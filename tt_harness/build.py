# SPDX-License-Identifier: Apache-2.0
"""Compare builds: bitstreams with openXC7 or Vivado, and a timing gate.

The tools are driven directly (as `tests/toolchain/test_blink.py` does) rather than through
LiteX's build backends, because both flows need things those backends do not give: openXC7
must constrain the BUFG-derived project clock per clock (LiteX constrains only the input clock
and passes `--timing-allow-fail`), and Vivado needs a generated clock and multicycle paths
written after synthesis, when the `pclk` register exists. The board layer supplies pins (from
litex-boards), parts and the BUFG.

Clocks, with S = `SETTLE` system cycles and T the system period:

- `sys`: the board oscillator.
- `pclk`: the recorder's `pclk` register through a BUFG, constrained at its shortest real
  period, 2 * (S + 1) * T. This times the project's own logic at that rate, not at the
  project's real clock.
- `sys->pclk` and `pclk->sys`: every sys-to-project path has S system cycles from a change
  to the next project clock edge, and every project-to-sys path S cycles from a project edge
  to the sample (see `tt_harness.compare.recorder`).

The gate raises `TimingError` when a clock the design has is missing from the report, a
clock in the report is not one of the design's constrained clocks (it was timed at a default),
a clock was timed against a different target than the one given, or any domain misses. On
failure no bitstream is left at `BuildResult.bitstream`'s path.

Files under `out_dir`: `build.log` (every tool's output), the generated top, constraints and
scripts, reports, and `<top>.bit` on success.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tt_harness import tools, verilate
from tt_harness.boards import arty
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

SETTLE = 2
BOARDS = {arty.NAME: arty}
TOOLCHAINS = ("openxc7", "vivado")
MAX_JOBS = 16
LOG_NAME = "build.log"
STEP_TIMEOUT_S = 3 * 3600
NEXTPNR_SEEDS = 3
# nextpnr-xilinx reports cross-domain paths as data-path delays only: it does not add the
# insertion delay of the BUFG that `pclk` goes through. One system period of the S-period
# budget is set aside for it (Vivado measures that insertion delay; see the build report).
CROSS_DOMAIN_ALLOWANCE_CYCLES = 1

OPENXC7 = tools.TOOLCHAINS / "openxc7"
PRJXRAY_DB = OPENXC7 / "squashfs-root" / "opt" / "nextpnr-xilinx" / "external" / "prjxray-db"
CHIPDB = tools.TOOLCHAINS / "chipdb"
_SV_SUFFIXES = (".sv",)


class _LoggedError(RuntimeError):
    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.message = message
        self.log = log


class BuildError(_LoggedError):
    """Synthesis, place and route, or bitstream generation failed."""


class TimingError(_LoggedError):
    """A clock domain missed timing or was unconstrained."""


@dataclass(frozen=True)
class TimingDomain:
    constraint_mhz: float
    achieved_mhz: float
    met: bool
    # Worst setup and hold slack in ns, where the tool reports them.
    setup_slack_ns: float | None = None
    hold_slack_ns: float | None = None


@dataclass(frozen=True)
class BuildResult:
    bitstream: Path
    toolchain: str
    part: str
    timing: dict[str, TimingDomain]
    log: Path


@dataclass(frozen=True)
class ClockPlan:
    """What the design's clocks must be timed against."""

    sys_mhz: float
    pclk_mhz: float
    settle: int

    @property
    def sys_period_ns(self) -> float:
        return 1000.0 / self.sys_mhz

    @property
    def cross_requirement_ns(self) -> float:
        """Setup requirement of sys->pclk and pclk->sys paths: S system periods."""
        return self.settle * self.sys_period_ns

    @property
    def clocks(self) -> dict[str, float]:
        return {"sys": self.sys_mhz, "pclk": self.pclk_mhz}


def clock_plan(board, settle: int = SETTLE) -> ClockPlan:
    sys_mhz = board.SYS_CLK_FREQ / 1e6
    return ClockPlan(sys_mhz, sys_mhz / (2 * (settle + 1)), settle)


# Timing reports ----------------------------------------------------------------------------

_NEXTPNR_CLOCK_MAP = re.compile(
    r"^tt-harness clock: domain (\S+) net '(.*)' ([\d.]+) MHz$", re.MULTILINE
)
# The clock-name column is padded to the longest name, inside the quotes or after them.
_NEXTPNR_FMAX = re.compile(
    r"^(?:Info|ERROR|Warning): Max frequency for clock\s+'\s*(?P<name>.+?)\s*':\s+"
    r"(?P<achieved>[\d.]+) MHz \((?P<verdict>PASS|FAIL) at (?P<target>[\d.]+) MHz\)\s*$",
    re.MULTILINE,
)
_NEXTPNR_DELAY = re.compile(
    r"^Info: Max delay (?P<from>.+?)\s+->\s+(?P<to>.+?)\s*:\s+(?P<delay>[\d.]+) ns\s*$",
    re.MULTILINE,
)
_NEXTPNR_ROUTED = "Info: Routing complete."


def _edge_clock(text: str) -> str | None:
    """'posedge clk0' -> 'clk0'; '<async>' -> None."""
    text = text.strip()
    if text.startswith("<"):
        return None
    edge, _, name = text.partition(" ")
    if edge not in ("posedge", "negedge") or not name:
        raise ValueError(f"unexpected clock edge {text!r} in nextpnr report")
    return name.strip()


def parse_nextpnr(text: str, plan: ClockPlan, log: Path) -> dict[str, TimingDomain]:
    """Timing per domain from a nextpnr-xilinx log (the post-route report is used).

    Net names are mapped to domains by the `tt-harness clock:` lines that the pre-pack
    script prints. Raises `TimingError` for an unmapped clock, a mapped clock missing from
    the report, or a target other than the plan's.
    """
    nets = {m.group(2): m.group(1) for m in _NEXTPNR_CLOCK_MAP.finditer(text)}
    routed = text.rfind(_NEXTPNR_ROUTED)
    if routed < 0:
        raise TimingError("nextpnr log has no post-route timing report", log)
    report = text[routed:]

    timing: dict[str, TimingDomain] = {}
    for m in _NEXTPNR_FMAX.finditer(report):
        net = m.group("name")
        domain = nets.get(net)
        if domain is None or domain not in plan.clocks:
            raise TimingError(
                f"unconstrained: clock net '{net}' is not a constrained clock of the design "
                f"(nextpnr timed it at its default {m.group('target')} MHz)",
                log,
            )
        target = float(m.group("target"))
        if abs(target - plan.clocks[domain]) > 0.01:
            raise TimingError(
                f"unconstrained: clock {domain} ('{net}') was timed at {target} MHz, "
                f"not {plan.clocks[domain]:.3f} MHz",
                log,
            )
        achieved = float(m.group("achieved"))
        timing[domain] = TimingDomain(
            constraint_mhz=plan.clocks[domain],
            achieved_mhz=achieved,
            met=m.group("verdict") == "PASS" and achieved >= target,
            setup_slack_ns=1000.0 / target - 1000.0 / achieved,
        )

    budget = plan.cross_requirement_ns - CROSS_DOMAIN_ALLOWANCE_CYCLES * plan.sys_period_ns
    worst: dict[str, float] = {}
    for m in _NEXTPNR_DELAY.finditer(report):
        ends = []
        for side in ("from", "to"):
            net = _edge_clock(m.group(side))
            if net is not None and nets.get(net) is None:
                raise TimingError(
                    f"unconstrained: cross-domain path {m.group('from')} -> {m.group('to')} "
                    f"involves clock net '{net}', which is not a constrained clock",
                    log,
                )
            ends.append(nets.get(net) if net is not None else None)
        if None in ends or ends[0] == ends[1]:
            continue  # I/O paths are not clock domains
        key = f"{ends[0]}->{ends[1]}"
        worst[key] = max(worst.get(key, 0.0), float(m.group("delay")))
    for key, delay in worst.items():
        timing[key] = TimingDomain(
            constraint_mhz=1000.0 / budget,
            achieved_mhz=1000.0 / delay if delay > 0 else float("inf"),
            met=delay <= budget,
            setup_slack_ns=budget - delay,
        )
    return timing


# check_timing items that leave part of the design untimed. no_input_delay and
# no_output_delay concern the asynchronous UART and LED pins, not a clock domain.
VIVADO_CHECKS = (
    "no_clock",
    "constant_clock",
    "pulse_width_clock",
    "unconstrained_internal_endpoints",
    "multiple_clock",
    "generated_clocks",
    "loops",
    "partial_input_delay",
    "partial_output_delay",
    "latch_loops",
)
_VIVADO_CHECK = re.compile(r"^\d+\. checking (\w+) \((\d+)\)\s*$", re.MULTILINE)


def _table_rows(text: str, title: str) -> list[list[str]]:
    """The rows of a `report_timing_summary` table, split on whitespace."""
    start = text.find(f"| {title}\n")
    if start < 0:
        return []
    lines = text[start:].splitlines()[1:]
    rows: list[list[str]] = []
    in_body = False
    for line in lines:
        if not in_body:
            if re.match(r"^\s*-{3,}(\s+-{3,})+\s*$", line):
                in_body = True
            continue
        if not line.strip():
            break
        rows.append(line.split())
    return rows


def _ns(text: str) -> float | None:
    return None if text == "NA" else float(text)


def parse_vivado(text: str, plan: ClockPlan, log: Path) -> dict[str, TimingDomain]:
    """Timing per clock and per clock pair from a Vivado `report_timing_summary`.

    Raises `TimingError` if `check_timing` found untimed logic, the report lists a clock the
    plan does not have, or a clock's period differs from the plan's.
    """
    checks = {}
    for m in _VIVADO_CHECK.finditer(text):
        checks.setdefault(m.group(1), int(m.group(2)))
    missing_checks = [c for c in VIVADO_CHECKS if c not in checks]
    if missing_checks:
        raise TimingError(f"Vivado timing report lacks check_timing {missing_checks}", log)
    bad = {c: checks[c] for c in VIVADO_CHECKS if checks[c]}
    if bad:
        detail = ", ".join(f"{c} ({n})" for c, n in bad.items())
        raise TimingError(f"unconstrained: Vivado check_timing reports {detail}", log)

    periods = {}
    for row in _table_rows(text, "Clock Summary"):
        name, period = row[0], float(row[-2])
        if name not in plan.clocks:
            raise TimingError(f"unconstrained: unexpected clock {name} in the design", log)
        expected = 1000.0 / plan.clocks[name]
        if abs(period - expected) > 0.001:
            raise TimingError(f"clock {name} has period {period} ns, not {expected:.3f}", log)
        periods[name] = period

    timing: dict[str, TimingDomain] = {}
    for row in _table_rows(text, "Intra Clock Table"):
        name = row[0]
        if name not in periods:
            raise TimingError(f"clock {name} in the intra-clock table has no period", log)
        wns, whs, wpws = _ns(row[1]), _ns(row[5]), _ns(row[9])
        period = periods[name]
        timing[name] = TimingDomain(
            constraint_mhz=1000.0 / period,
            achieved_mhz=1000.0 / (period - wns) if wns is not None else float("inf"),
            met=all(v is None or v >= 0 for v in (wns, whs)) and wpws is not None and wpws >= 0,
            setup_slack_ns=wns,
            hold_slack_ns=whs,
        )
    requirement = plan.cross_requirement_ns
    for row in _table_rows(text, "Inter Clock Table"):
        src, dst = row[0], row[1]
        if src not in periods or dst not in periods:
            raise TimingError(f"inter-clock path {src} -> {dst} involves an unknown clock", log)
        wns, whs = _ns(row[2]), _ns(row[6])
        timing[f"{src}->{dst}"] = TimingDomain(
            constraint_mhz=1000.0 / requirement,
            achieved_mhz=1000.0 / (requirement - wns) if wns is not None else float("inf"),
            met=all(v is None or v >= 0 for v in (wns, whs)),
            setup_slack_ns=wns,
            hold_slack_ns=whs,
        )
    if "Timing constraints are not met." in text and all(d.met for d in timing.values()):
        raise TimingError("Vivado reports timing not met in a path group not parsed", log)
    return timing


def check(timing: dict[str, TimingDomain], plan: ClockPlan, log: Path) -> None:
    """Raise `TimingError` unless every clock of the plan is reported and every domain met."""
    missing = sorted(set(plan.clocks) - set(timing))
    if missing:
        raise TimingError(
            f"unconstrained: clock domain(s) {', '.join(missing)} absent from the timing report",
            log,
        )
    failed = [f"{k} ({_describe(d)})" for k, d in sorted(timing.items()) if not d.met]
    if failed:
        raise TimingError(f"timing not met in {', '.join(failed)}", log)


def _describe(d: TimingDomain) -> str:
    parts = [f"{d.achieved_mhz:.2f} MHz achieved against {d.constraint_mhz:.2f} MHz"]
    if d.setup_slack_ns is not None:
        parts.append(f"setup slack {d.setup_slack_ns:.3f} ns")
    if d.hold_slack_ns is not None:
        parts.append(f"hold slack {d.hold_slack_ns:.3f} ns")
    return ", ".join(parts)


# Flows ---------------------------------------------------------------------------------------

_PREPACK = """\
# Generated by tt_harness.build: constrain each clock net of the compare build, found by
# structure, and print which net is which domain for the timing gate.
INPUT_PORT = {input_port!r}
SYS_MHZ = {sys_mhz!r}
PCLK_BUFG = {pclk_bufg!r}
PCLK_MHZ = {pclk_mhz!r}
found = {{}}
for name, cell in ctx.cells:
    if cell.type != "BUFG":
        continue
    out_net = cell.ports["O"].net
    in_net = cell.ports["I"].net
    if name == PCLK_BUFG:
        domain, mhz = "pclk", PCLK_MHZ
    else:
        driver = in_net.driver.cell if in_net is not None and in_net.driver is not None else None
        source = None
        if driver is not None and driver.type == "IBUF" and driver.ports["I"].net is not None:
            source = driver.ports["I"].net.name
        if source != INPUT_PORT:
            raise RuntimeError(f"tt-harness: BUFG {{name}} is not fed from {{INPUT_PORT}}")
        domain, mhz = "sys", SYS_MHZ
    if domain in found:
        raise RuntimeError(f"tt-harness: two BUFGs for domain {{domain}}")
    ctx.addClock(out_net.name, mhz)
    found[domain] = out_net.name
    print(f"tt-harness clock: domain {{domain}} net '{{out_net.name}}' {{mhz}} MHz")
missing = sorted({{"sys", "pclk"}} - set(found))
if missing:
    raise RuntimeError(f"tt-harness: no BUFG found for {{missing}}")
"""


@dataclass(frozen=True)
class _Job:
    sources: Sources
    board: object
    toolchain: str
    part: str
    out_dir: Path
    top_v: Path
    pins_xdc: Path
    plan: ClockPlan
    jobs: int
    log: Path

    @property
    def top(self) -> str:
        return self.board.MODULE_NAME

    @property
    def unchecked_bit(self) -> Path:
        return self.out_dir / f"{self.top}.unchecked.bit"


def _step(job: _Job, what: str, cmd: list[str], cwd: Path, **kwargs) -> None:
    rc = verilate.run(cmd, job.log, cwd, STEP_TIMEOUT_S, **kwargs)
    if rc != 0:
        raise BuildError(f"{job.sources.project.id}: {what} failed (exit {rc})", job.log)


def _tool(name: str) -> Path:
    path = tools.find(name)
    if path is None:
        raise FileNotFoundError(f"{name} not found; run: uv run scripts/setup_toolchains.py")
    return path


def _openxc7_implement(job: _Job) -> str:
    """Synthesise, place and route; return the final nextpnr-xilinx log."""
    out = job.out_dir
    files, include_dirs = verilate.project_files(job.sources)
    incs = " ".join(f"-I{d}" for d in include_dirs)
    # Every project file is read as SystemVerilog, as Verilator (the reference) reads them.
    script = out / "synth.ys"
    script.write_text(
        "\n".join(
            [
                *(f"read_verilog -sv {incs} {f}" for f in files),
                f"read_verilog {job.top_v}",
                f"synth_xilinx -flatten -abc9 -arch xc7 -top {job.top}",
                # Scope records left by -flatten; nextpnr-xilinx has no bel for them.
                "delete t:$scopeinfo",
                f"tee -o {out / 'utilization-yosys.txt'} stat",
                f"write_json {out / (job.top + '.json')}",
            ]
        )
        + "\n"
    )
    _step(job, "synthesis (yosys)", [str(_tool("yosys")), "-s", str(script)], out)

    prepack = out / "clocks.py"
    prepack.write_text(
        _PREPACK.format(
            input_port=job.board.SYS_CLK_PORT,
            sys_mhz=job.plan.sys_mhz,
            pclk_bufg=job.board.BUFG_NAME,
            pclk_mhz=job.plan.pclk_mhz,
        )
    )
    chipdb = CHIPDB / f"{job.part}.bin"
    if not chipdb.is_file():
        raise FileNotFoundError(f"no nextpnr-xilinx chip database {chipdb}")
    fasm = out / f"{job.top}.fasm"
    text = ""
    for seed in range(1, NEXTPNR_SEEDS + 1):
        start = job.log.stat().st_size
        cmd = [
            str(_tool("nextpnr-xilinx")),
            "--chipdb",
            str(chipdb),
            "--xdc",
            str(job.pins_xdc),
            "--json",
            str(out / (job.top + ".json")),
            "--pre-pack",
            str(prepack),
            "--seed",
            str(seed),
            "--fasm",
            str(fasm),
        ]
        rc = verilate.run(cmd, job.log, out, STEP_TIMEOUT_S)
        with job.log.open("rb") as f:
            f.seek(start)
            text = f.read().decode(errors="replace")
        if rc == 0:
            break
        if not re.search(r"^ERROR: Max frequency for clock .*FAIL at", text, re.MULTILINE):
            raise BuildError(
                f"{job.sources.project.id}: place and route (nextpnr-xilinx) failed (exit {rc})",
                job.log,
            )
        with job.log.open("a") as f:
            f.write(f"\ntt-harness: timing not met with nextpnr seed {seed}\n")
    (out / "nextpnr.log").write_text(text)
    return text


def _openxc7_bitstream(job: _Job) -> None:
    out = job.out_dir
    fasm = out / f"{job.top}.fasm"
    db = PRJXRAY_DB / "artix7"
    frames = out / f"{job.top}.frames"
    _step(
        job,
        "fasm2frames",
        [str(_tool("fasm2frames")), "--part", job.part, "--db-root", str(db), str(fasm)],
        out,
        stdout=frames,
    )
    _step(
        job,
        "xc7frames2bit",
        [
            str(_tool("xc7frames2bit")),
            "--part_file",
            str(db / job.part / "part.yaml"),
            "--part_name",
            job.part,
            "--frm_file",
            str(frames),
            "--output_file",
            str(job.unchecked_bit),
        ],
        out,
    )


_VIVADO_CLOCKS = """\
# Generated by tt_harness.build. sys is the board oscillator. pclk is the recorder's pclk
# register through the BUFG; it is defined on the BUFG output, so the register's own (sys)
# data fanout stays in sys, with the register's clock-to-out and the BUFG in its latency.
create_clock -name sys -period {sys_period:.3f} [get_ports {input_port}]
set bufg [get_cells {bufg}]
if {{[llength $bufg] != 1}} {{ error "tt-harness: no BUFG {bufg}" }}
set drivers [get_cells -of_objects [get_pins -leaf -filter {{DIRECTION == OUT}} \\
    -of_objects [get_nets -of_objects [get_pins {bufg}/I]]]]
if {{[llength $drivers] != 1 || [lsearch -exact {{FDRE FDSE FDCE FDPE}} \\
        [get_property REF_NAME $drivers]] < 0}} {{
    error "tt-harness: {bufg}/I is not driven by one flip-flop: $drivers"
}}
puts "tt-harness: pclk register $drivers ([get_property REF_NAME $drivers])"
create_generated_clock -name pclk -divide_by {divide} \\
    -source [get_pins -of_objects $drivers -filter {{REF_PIN_NAME == C}}] [get_pins {bufg}/O]
# One project clock is S = {settle} system cycles from any sys-side change to the next pclk
# edge, and S cycles from a pclk edge to the sample. Setup is checked over S sys cycles in
# both directions. Hold: sys->pclk data changes one sys cycle after a falling pclk edge at
# the earliest (requirement -1 sys period); pclk->sys keeps the default same-edge check
# (requirement 0), which is stricter than the recorder needs.
set_multicycle_path -setup -start {settle} -from [get_clocks sys] -to [get_clocks pclk]
set_multicycle_path -hold -start {settle} -from [get_clocks sys] -to [get_clocks pclk]
set_multicycle_path -setup -end {settle} -from [get_clocks pclk] -to [get_clocks sys]
set_multicycle_path -hold -end {settle_minus_1} -from [get_clocks pclk] -to [get_clocks sys]
"""


def _vivado_sources(job: _Job) -> tuple[list[Path], list[Path], list[Path]]:
    """Copy src/ for Vivado and return (Verilog files, SystemVerilog files, include dirs).

    Vivado rejects a non-ANSI port without a net type (`input clk;`) under
    `default_nettype none`, which Verilator and Yosys accept; the copy says
    `default_nettype wire` instead. Nothing else changes, and a design with a real implicit
    net would already have failed the reference build.
    """
    src = job.sources.root / "src"
    dest = job.out_dir / "src"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)
    for path in dest.rglob("*"):
        if path.is_file() and path.suffix in (".v", ".sv", ".vh", ".svh"):
            text = path.read_text(errors="surrogateescape")
            fixed = re.sub(r"`default_nettype\s+none\b", "`default_nettype wire", text)
            if fixed != text:
                path.write_text(fixed, errors="surrogateescape")
    files, include_dirs = verilate.project_files(job.sources)
    moved = [dest / f.relative_to(src) for f in files]
    dirs = [dest / d.relative_to(src) for d in include_dirs]
    plain = [f for f in moved if f.suffix not in _SV_SUFFIXES]
    sv = [f for f in moved if f.suffix in _SV_SUFFIXES]
    return plain, sv, dirs


def _vivado_implement(job: _Job) -> str:
    """Synthesise, implement and write the bitstream; return the timing summary."""
    settings = tools.vivado_settings()
    if settings is None:
        raise FileNotFoundError(f"Vivado not found at {tools.VIVADO_SETTINGS}")
    out = job.out_dir
    plain, sv, dirs = _vivado_sources(job)
    clocks_tcl = out / "clocks.tcl"
    clocks_tcl.write_text(
        _VIVADO_CLOCKS.format(
            sys_period=job.plan.sys_period_ns,
            input_port=job.board.SYS_CLK_PORT,
            bufg=job.board.BUFG_NAME,
            divide=2 * (job.plan.settle + 1),
            settle=job.plan.settle,
            settle_minus_1=job.plan.settle - 1,
        )
    )
    timing_rpt = out / "timing.rpt"

    def braced(paths: list[Path]) -> str:
        return " ".join("{" + str(p) + "}" for p in paths)

    pair_reports = []
    for src, dst in (("sys", "pclk"), ("pclk", "sys")):
        for kind in ("max", "min"):
            name = out / f"{src}-to-{dst}-{'setup' if kind == 'max' else 'hold'}.rpt"
            pair_reports.append(
                f"report_timing -from [get_clocks {src}] -to [get_clocks {dst}] "
                f"-delay_type {kind} -max_paths 3 -file {{{name}}}"
            )
    script = out / "build.tcl"
    script.write_text(
        "\n".join(
            [
                f"set_param general.maxThreads {job.jobs}",
                *([f"read_verilog {braced(plain)}"] if plain else []),
                *([f"read_verilog -sv {braced(sv)}"] if sv else []),
                f"read_verilog {{{job.top_v}}}",
                f"read_xdc {{{job.pins_xdc}}}",
                f"synth_design -top {job.top} -part {job.part} -include_dirs [list {braced(dirs)}]",
                f"source {{{clocks_tcl}}}",
                "opt_design",
                "place_design",
                "phys_opt_design",
                "route_design",
                f"report_utilization -file {{{out / 'utilization.rpt'}}}",
                f"report_clocks -file {{{out / 'clocks.rpt'}}}",
                f"report_timing_summary -max_paths 10 -report_unconstrained -file {{{timing_rpt}}}",
                *pair_reports,
                f"write_checkpoint -force {{{out / (job.top + '.dcp')}}}",
                f"write_bitstream -force {{{job.unchecked_bit}}}",
            ]
        )
        + "\n"
    )
    # Vivado's own environment stays in this subshell; its log and journal go in out_dir.
    shell = (
        f'source "{settings}" && exec vivado -mode batch -nojournal '
        f'-log "{out / "vivado.log"}" -source "{script}"'
    )
    _step(job, "Vivado (synthesis, implementation, bitstream)", ["bash", "-c", shell], out)
    if not timing_rpt.is_file():
        raise BuildError(f"{job.sources.project.id}: Vivado wrote no timing report", job.log)
    return timing_rpt.read_text()


def _vivado_bitstream(job: _Job) -> None:
    """Nothing to do: the Vivado run already wrote the (unchecked) bitstream."""


@dataclass(frozen=True)
class _Flow:
    implement: Callable[[_Job], str]  # returns the timing report text
    parse: Callable[[str, ClockPlan, Path], dict[str, TimingDomain]]
    bitstream: Callable[[_Job], None]  # writes job.unchecked_bit if implement did not


_FLOWS = {
    "openxc7": _Flow(_openxc7_implement, parse_nextpnr, _openxc7_bitstream),
    "vivado": _Flow(_vivado_implement, parse_vivado, _vivado_bitstream),
}


def build_compare(
    project: Project,
    sources: Sources,
    board: str,
    toolchain: str,
    out_dir: Path,
    clocks: int,
    baud: int = 1_000_000,
    jobs: int = 16,
) -> BuildResult:
    """Build the compare bitstream for `project` and gate it on timing.

    Raises `BuildError` if a tool fails and `TimingError` if a clock domain is missing from
    the timing report, unconstrained or misses timing. On any failure no bitstream is left at
    the result path, `out_dir/<top>.bit`.
    """
    if board not in BOARDS:
        raise ValueError(f"unknown board {board!r}; known: {', '.join(BOARDS)}")
    if toolchain not in _FLOWS:
        raise ValueError(f"unknown toolchain {toolchain!r}; known: {', '.join(TOOLCHAINS)}")
    if not 1 <= jobs <= MAX_JOBS:
        raise ValueError(f"jobs must be 1..{MAX_JOBS}, not {jobs}")
    if sources.project != project:
        raise ValueError(f"sources are for {sources.project.id}, not {project.id}")
    board_mod = BOARDS[board]
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / LOG_NAME
    bitstream = out_dir / f"{board_mod.MODULE_NAME}.bit"
    for stale in (log, bitstream, out_dir / f"{board_mod.MODULE_NAME}.unchecked.bit"):
        stale.unlink(missing_ok=True)
    log.write_text(f"tt-harness build-compare {project.id} {board} {toolchain} clocks={clocks}\n")

    plan = clock_plan(board_mod)
    top = board_mod.Top(project, baud, clocks, settle=plan.settle)
    top_v = board_mod.to_verilog(top, out_dir / f"{board_mod.MODULE_NAME}.v")
    pins = out_dir / "pins.xdc"
    pins.write_text(board_mod.pin_xdc())
    job = _Job(
        sources=sources,
        board=board_mod,
        toolchain=toolchain,
        part=board_mod.PARTS[toolchain],
        out_dir=out_dir,
        top_v=top_v,
        pins_xdc=pins,
        plan=plan,
        jobs=jobs,
        log=log,
    )
    flow = _FLOWS[toolchain]
    try:
        timing = flow.parse(flow.implement(job), plan, log)
        check(timing, plan, log)
        flow.bitstream(job)
        if not job.unchecked_bit.is_file():
            raise BuildError(f"{project.id}: no bitstream was written", log)
    except BaseException:
        job.unchecked_bit.unlink(missing_ok=True)
        raise
    job.unchecked_bit.rename(bitstream)
    with log.open("a") as f:
        for name, d in sorted(timing.items()):
            f.write(f"tt-harness timing: {name}: {_describe(d)}, met\n")
    return BuildResult(bitstream, toolchain, job.part, timing, log)
