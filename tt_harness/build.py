# SPDX-License-Identifier: Apache-2.0
"""Compare builds: bitstreams with openXC7 or Vivado, and a timing gate.

The tools are driven directly (as `tests/toolchain/test_blink.py` does) rather than through
LiteX's build backends, because both flows need things those backends do not give: openXC7
must constrain the BUFG-derived project clock per clock (LiteX constrains only the input clock
and passes `--timing-allow-fail`), and Vivado needs generated clocks and multicycle paths
written after synthesis, when the `pclk` register and any clocks the project makes exist. The
board layer supplies pins (from litex-boards), parts and the BUFG.

Clocks, with S = `SETTLE` system cycles and T the system period:

- `sys`: the board oscillator.
- `pclk`: the recorder's `pclk` register through a BUFG, constrained at its shortest real
  period, 2 * (S + 1) * T. This times the project's own logic at that rate, not at the
  project's real clock.
- project clocks: any other clock the tools find, made by the project's own logic (a register
  or gate in the pclk domain). Each is constrained at the pclk period, a safe bound: logic
  clocked by pclk cannot make a faster clock. Timing keys are `project:<source>`.
- sys <-> pclk and sys <-> project clocks: S system cycles from a sys-side change to the
  next project edge and from a project edge to the sample (`tt_harness.compare.recorder`).
  pclk <-> project clocks use the plain relationship of two clocks with pclk's waveform.

The gate raises `TimingError` when a clock in the design is missing from the report, a clock
in the report has no constraint (it was timed at a default), a clock was timed against a
different target than the one given, or any domain misses. On failure no bitstream is left
at `BuildResult.bitstream`'s path.

Project sources are given to the tools unmodified, as Verilog-2005 unless the design needs
SystemVerilog (a `.sv` file, or the plain parse fails), the rule the pre-screen uses. The
one exception is opt-in: Vivado rejects a non-ANSI port without a net type under
`default_nettype none` and offers no option to relax it; `vivado_nettype_rewrite=True`
gives it copies with `default_nettype wire` and records them in `source_rewrites`.

Files under `out_dir`: `build.log` (every tool's output), the generated top, constraints and
scripts, reports, and `<top>.bit` on success.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tt_harness import prescreen, tools, verilate
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
# insertion delay of the logic-driven clocks (pclk through its BUFG, a project clock behind
# a pclk register). One system period of each budget is set aside for it; Vivado measures
# those insertion delays at a few ns (see the build report).
CROSS_DOMAIN_ALLOWANCE_CYCLES = 1
VERILOG = "verilog-2005"
SYSTEMVERILOG = "systemverilog"

OPENXC7 = tools.TOOLCHAINS / "openxc7"
PRJXRAY_DB = OPENXC7 / "squashfs-root" / "opt" / "nextpnr-xilinx" / "external" / "prjxray-db"
CHIPDB = tools.TOOLCHAINS / "chipdb"

Kind = Literal["sys", "pclk", "project", "cross"]
PROJECT = "project:"


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
    # "cross" marks a clock pair (`a->b`), the others a clock.
    kind: Kind
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
    project_clocks: tuple[str, ...] = ()  # sources of clocks made by the project's logic
    language: str = VERILOG  # how the project sources were read
    prescreen_derived_clocks: int = 0  # `prescreen.check(sources).derived_clocks`
    inputs: dict[str, str] = field(default_factory=dict)  # project file -> SHA-256 given
    source_rewrites: tuple[str, ...] = ()  # project files given to a tool modified


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
    def pclk_period_ns(self) -> float:
        return 1000.0 / self.pclk_mhz

    @property
    def cross_requirement_ns(self) -> float:
        """Setup requirement between sys and a project-side clock: S system periods."""
        return self.settle * self.sys_period_ns

    @property
    def clocks(self) -> dict[str, float]:
        return {"sys": self.sys_mhz, "pclk": self.pclk_mhz}

    def mhz(self, kind: str) -> float:
        return self.sys_mhz if kind == "sys" else self.pclk_mhz

    def requirement_ns(self, src: str, dst: str, same_edge: bool = True) -> float:
        """Setup requirement of a path from clock kind `src` to clock kind `dst`."""
        if "sys" in (src, dst):
            return self.cross_requirement_ns
        return self.pclk_period_ns if same_edge else self.pclk_period_ns / 2


def clock_plan(board, settle: int = SETTLE) -> ClockPlan:
    sys_mhz = board.SYS_CLK_FREQ / 1e6
    return ClockPlan(sys_mhz, sys_mhz / (2 * (settle + 1)), settle)


def _kind(key: str) -> str:
    return "project" if key.startswith(PROJECT) else key


# Timing reports ----------------------------------------------------------------------------

_NEXTPNR_CLOCK_MAP = re.compile(
    r"^tt-harness clock: domain (\S+) net '(.*)' ([\d.]+) MHz\b.*$", re.MULTILINE
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


def _edge_clock(text: str) -> tuple[str, str] | None:
    """'posedge clk0' -> ('posedge', 'clk0'); '<async>' -> None."""
    text = text.strip()
    if text.startswith("<"):
        return None
    edge, _, name = text.partition(" ")
    if edge not in ("posedge", "negedge") or not name:
        raise ValueError(f"unexpected clock edge {text!r} in nextpnr report")
    return edge, name.strip()


def _nextpnr_domains(text: str) -> dict[str, str]:
    """Net name -> timing key, from the pre-pack script's `tt-harness clock:` lines."""
    nets = {}
    for m in _NEXTPNR_CLOCK_MAP.finditer(text):
        domain, net = m.group(1), m.group(2)
        nets[net] = f"{PROJECT}{net}" if domain == "project" else domain
    return nets


def parse_nextpnr(text: str, plan: ClockPlan, log: Path) -> dict[str, TimingDomain]:
    """Timing per domain from a nextpnr-xilinx log (the post-route report is used).

    Net names are mapped to domains by the `tt-harness clock:` lines that the pre-pack
    script prints. Raises `TimingError` for an unmapped clock, a mapped clock missing from
    the report, or a target other than the plan's.
    """
    nets = _nextpnr_domains(text)
    routed = text.rfind(_NEXTPNR_ROUTED)
    if routed < 0:
        raise TimingError("nextpnr log has no post-route timing report", log)
    report = text[routed:]

    timing: dict[str, TimingDomain] = {}
    for m in _NEXTPNR_FMAX.finditer(report):
        net = m.group("name")
        key = nets.get(net)
        if key is None:
            raise TimingError(
                f"unconstrained: clock net '{net}' is not a constrained clock of the design "
                f"(nextpnr timed it at its default {m.group('target')} MHz)",
                log,
            )
        kind = _kind(key)
        target = float(m.group("target"))
        if abs(target - plan.mhz(kind)) > 0.01:
            raise TimingError(
                f"unconstrained: clock {key} ('{net}') was timed at {target} MHz, "
                f"not {plan.mhz(kind):.3f} MHz",
                log,
            )
        achieved = float(m.group("achieved"))
        timing[key] = TimingDomain(
            constraint_mhz=plan.mhz(kind),
            achieved_mhz=achieved,
            met=m.group("verdict") == "PASS" and achieved >= target,
            kind=kind,
            setup_slack_ns=1000.0 / target - 1000.0 / achieved,
        )

    allowance = CROSS_DOMAIN_ALLOWANCE_CYCLES * plan.sys_period_ns
    worst: dict[str, tuple[float, float, float]] = {}  # key -> (slack, budget, delay)
    for m in _NEXTPNR_DELAY.finditer(report):
        ends = []
        for side in ("from", "to"):
            edge_net = _edge_clock(m.group(side))
            if edge_net is not None and edge_net[1] not in nets:
                raise TimingError(
                    f"unconstrained: cross-domain path {m.group('from')} -> {m.group('to')} "
                    f"involves clock net '{edge_net[1]}', which is not a constrained clock",
                    log,
                )
            ends.append(None if edge_net is None else (edge_net[0], nets[edge_net[1]]))
        if None in ends or ends[0][1] == ends[1][1]:
            continue  # I/O paths and half-cycle paths within one clock are not clock pairs
        (src_edge, src), (dst_edge, dst) = ends
        budget = plan.requirement_ns(_kind(src), _kind(dst), src_edge == dst_edge) - allowance
        delay = float(m.group("delay"))
        key = f"{src}->{dst}"
        if key not in worst or budget - delay < worst[key][0]:
            worst[key] = (budget - delay, budget, delay)
    for key, (slack, budget, delay) in worst.items():
        timing[key] = TimingDomain(
            constraint_mhz=1000.0 / budget,
            achieved_mhz=1000.0 / delay if delay > 0 else float("inf"),
            met=slack >= 0,
            kind="cross",
            setup_slack_ns=slack,
        )

    absent = sorted(set(nets.values()) - set(timing))
    # A project clock whose registers take data only from other clocks has no Fmax line of
    # its own; its paths are then all cross-domain, which must be reported.
    absent = [k for k in absent if not (k.startswith(PROJECT) and _in_cross(k, timing))]
    if absent:
        raise TimingError(
            f"unconstrained: clock domain(s) {', '.join(absent)} absent from the timing report",
            log,
        )
    return timing


def _in_cross(key: str, timing: dict[str, TimingDomain]) -> bool:
    return any(d.kind == "cross" and key in k.split("->") for k, d in timing.items())


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
_VIVADO_PROJECT_CLOCK = re.compile(
    r"^tt-harness project clock: (\S+) on (\S+) from \S+ \(master pin \S+\)$", re.MULTILINE
)


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

    Project clocks are named `project_clk<n>` in Vivado; the `tt-harness project clock:`
    lines (from the constraint script, prepended to the report) map them to their source
    pins. Raises `TimingError` if `check_timing` found untimed logic, the report lists a clock
    that is neither sys, pclk nor a mapped project clock, a clock's period differs from the
    plan's, or a mapped clock is absent.
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

    names = {"sys": "sys", "pclk": "pclk"}
    names |= {m.group(1): f"{PROJECT}{m.group(2)}" for m in _VIVADO_PROJECT_CLOCK.finditer(text)}
    periods = {}
    for row in _table_rows(text, "Clock Summary"):
        name, period = row[0], float(row[-2])
        if name not in names:
            raise TimingError(f"unconstrained: unexpected clock {name} in the design", log)
        expected = 1000.0 / plan.mhz(_kind(names[name]))
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
        key = names[name]
        timing[key] = TimingDomain(
            constraint_mhz=1000.0 / period,
            achieved_mhz=1000.0 / (period - wns) if wns is not None else float("inf"),
            met=all(v is None or v >= 0 for v in (wns, whs)) and wpws is not None and wpws >= 0,
            kind=_kind(key),
            setup_slack_ns=wns,
            hold_slack_ns=whs,
        )

    def pair(src: str, dst: str, wns: float | None, whs: float | None, suffix: str) -> None:
        if src not in periods or dst not in periods:
            raise TimingError(f"inter-clock path {src} -> {dst} involves an unknown clock", log)
        a, b = names[src], names[dst]
        requirement = plan.requirement_ns(_kind(a), _kind(b))
        timing[f"{a}->{b}{suffix}"] = TimingDomain(
            constraint_mhz=1000.0 / requirement,
            achieved_mhz=1000.0 / (requirement - wns) if wns is not None else float("inf"),
            met=all(v is None or v >= 0 for v in (wns, whs)),
            kind="cross",
            setup_slack_ns=wns,
            hold_slack_ns=whs,
        )

    for row in _table_rows(text, "Inter Clock Table"):
        pair(row[0], row[1], _ns(row[2]), _ns(row[6]), "")
    for row in _table_rows(text, "Other Path Groups Table"):
        # Recovery and removal of asynchronous resets, e.g. a project's `negedge rst_n`.
        pair(row[1], row[2], _ns(row[3]), _ns(row[7]), f" ({row[0].strip('*')})")

    absent = sorted(k for k in names.values() if k not in timing and not _in_cross(k, timing))
    if absent:
        raise TimingError(
            f"unconstrained: clock domain(s) {', '.join(absent)} absent from the timing report",
            log,
        )
    if "Timing constraints are not met." in text and all(d.met for d in timing.values()):
        raise TimingError("Vivado reports timing not met in a path group not parsed", log)
    return timing


def check(timing: dict[str, TimingDomain], plan: ClockPlan, log: Path) -> None:
    """Raise `TimingError` unless sys and pclk are reported and every domain met."""
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


def project_clocks(timing: dict[str, TimingDomain]) -> tuple[str, ...]:
    """The sources of the project clocks in a timing result."""
    keys = set()
    for key in timing:
        for part in key.split(" (")[0].split("->"):
            if part.startswith(PROJECT):
                keys.add(part.removeprefix(PROJECT))
    return tuple(sorted(keys))


# Flows ---------------------------------------------------------------------------------------

_PREPACK = """\
# Generated by tt_harness.build: constrain every clock net of the compare build and print
# which net is which domain for the timing gate. sys and pclk are found by their BUFGs; any
# other net that clocks a cell is made by the project's logic and is constrained at the
# pclk period.
INPUT_PORT = {input_port!r}
SYS_MHZ = {sys_mhz!r}
PCLK_BUFG = {pclk_bufg!r}
PCLK_MHZ = {pclk_mhz!r}
CLOCK_PORTS = ("C", "CLK", "WCLK", "CLKARDCLK", "CLKBWRCLK")
found = {{}}
known = set()


def constrain(domain, net, mhz, why):
    ctx.addClock(net.name, mhz)
    known.add(net.name)
    found.setdefault(domain, []).append(net.name)
    print(f"tt-harness clock: domain {{domain}} net '{{net.name}}' {{mhz}} MHz ({{why}})")


for name, cell in ctx.cells:
    if cell.type != "BUFG":
        continue
    out_net = cell.ports["O"].net
    in_net = cell.ports["I"].net
    if out_net is None:
        continue
    driver = in_net.driver.cell if in_net is not None and in_net.driver is not None else None
    source = None
    if driver is not None and driver.type == "IBUF" and driver.ports["I"].net is not None:
        source = driver.ports["I"].net.name
    if name == PCLK_BUFG:
        constrain("pclk", out_net, PCLK_MHZ, f"BUFG {{name}}")
    elif source == INPUT_PORT:
        constrain("sys", out_net, SYS_MHZ, f"BUFG {{name}} from {{INPUT_PORT}}")
    else:
        constrain("project", out_net, PCLK_MHZ, f"BUFG {{name}}")
for name, cell in ctx.cells:
    for port, info in cell.ports:
        if port in CLOCK_PORTS and info.net is not None and info.net.name not in known:
            constrain("project", info.net, PCLK_MHZ, f"clocks {{name}}.{{port}}")
for domain in ("sys", "pclk"):
    if len(found.get(domain, [])) != 1:
        raise RuntimeError(f"tt-harness: expected one {{domain}} clock, found {{found.get(domain)}}")
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
    language: str
    vivado_nettype_rewrite: bool = False
    # Filled in by the flow: project file given to the tool -> its SHA-256, and the files
    # given modified.
    inputs: dict[str, str] = field(default_factory=dict)
    rewrites: list[str] = field(default_factory=list)

    @property
    def top(self) -> str:
        return self.board.MODULE_NAME

    @property
    def unchecked_bit(self) -> Path:
        return self.out_dir / f"{self.top}.unchecked.bit"

    @property
    def sv(self) -> bool:
        return self.language == SYSTEMVERILOG


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _log_since(log: Path, start: int) -> str:
    with log.open("rb") as f:
        f.seek(start)
        return f.read().decode(errors="replace")


def _step(job: _Job, what: str, cmd: list[str], cwd: Path, **kwargs) -> None:
    start = job.log.stat().st_size
    rc = verilate.run(cmd, job.log, cwd, STEP_TIMEOUT_S, **kwargs)
    if rc != 0:
        first = re.search(r"^(?:ERROR|Error|error)\b.*$", _log_since(job.log, start), re.MULTILINE)
        detail = f": {first.group(0)}" if first else ""
        raise BuildError(f"{job.sources.project.id}: {what} failed (exit {rc}){detail}", job.log)


def _tool(name: str) -> Path:
    path = tools.find(name)
    if path is None:
        raise FileNotFoundError(f"{name} not found; run: uv run scripts/setup_toolchains.py")
    return path


def _openxc7_implement(job: _Job) -> str:
    """Synthesise, place and route; return the final nextpnr-xilinx log."""
    out = job.out_dir
    files, include_dirs = verilate.project_files(job.sources)
    job.inputs.update({str(f): _sha256(f) for f in files})
    incs = " ".join(f"-I{d}" for d in include_dirs)
    sv = "-sv " if job.sv else ""
    script = out / "synth.ys"
    script.write_text(
        "\n".join(
            [
                *(f"read_verilog {sv}{incs} {f}" for f in files),
                f"read_verilog {job.top_v}",
                # The board layer places both BUFGs; none are inserted for project clocks.
                f"synth_xilinx -flatten -abc9 -noclkbuf -arch xc7 -top {job.top}",
                # Cells with no hardware: scope records left by -flatten, and $display or
                # assertion cells from simulation-only code. nextpnr-xilinx has no bel for them.
                "delete t:$scopeinfo t:$print t:$check",
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
        text = _log_since(job.log, start)
        if rc == 0:
            break
        if not re.search(r"^ERROR: Max frequency for clock .*FAIL at", text, re.MULTILINE):
            first = re.search(r"^(?:ERROR|RuntimeError)\b.*$", text, re.MULTILINE)
            detail = f": {first.group(0)}" if first else ""
            raise BuildError(
                f"{job.sources.project.id}: place and route (nextpnr-xilinx) failed "
                f"(exit {rc}){detail}",
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

# Clocks made by the project's logic. Every clock pin of a sequential cell that no clock
# reaches yet is followed to the root of its net: a BUFG output, or the driving pin itself.
# Each root gets a clock with pclk's waveform (divide by 1 from the register that makes it,
# or from pclk when a gate makes it): logic clocked by pclk cannot toggle faster.
set roots {{}}
foreach pin [get_pins -quiet -filter {{IS_CLOCK}} \\
        -of_objects [get_cells -quiet -hier -filter {{IS_SEQUENTIAL}}]] {{
    if {{[llength [get_clocks -quiet -of_objects $pin]]}} {{ continue }}
    set drv [get_pins -quiet -leaf -filter {{DIRECTION == OUT}} -of_objects [get_nets -of_objects $pin]]
    if {{[llength $drv] != 1}} {{ error "tt-harness: clock pin $pin has driver(s) '$drv'" }}
    if {{[lsearch -exact $roots $drv] < 0}} {{ lappend roots $drv }}
}}
set n 0
foreach root $roots {{
    set cell [get_cells -of_objects $root]
    if {{[string match BUFG* [get_property REF_NAME $cell]]}} {{
        set src [get_pins -leaf -filter {{DIRECTION == OUT}} \\
            -of_objects [get_nets -of_objects [get_pins $cell/I]]]
    }} else {{
        set src $root
    }}
    set srccell [get_cells -of_objects $src]
    if {{[lsearch -exact {{FDRE FDSE FDCE FDPE}} [get_property REF_NAME $srccell]] >= 0}} {{
        set master [get_pins -of_objects $srccell -filter {{REF_PIN_NAME == C}}]
    }} else {{
        set master [get_pins {bufg}/O]
    }}
    set name project_clk$n
    incr n
    puts "tt-harness project clock: $name on $root from $src (master pin $master)"
    create_generated_clock -name $name -divide_by 1 -source $master $root
}}
puts "tt-harness: $n project clock(s)"

# One project clock is S = {settle} system cycles from any sys-side change to the next pclk
# edge, and S cycles from a pclk edge to the sample. Setup is checked over S sys cycles in
# both directions. Hold: sys->pclk data changes one sys cycle after a falling pclk edge at
# the earliest (requirement -1 sys period); pclk->sys keeps the default same-edge check
# (requirement 0), which is stricter than the recorder needs. Clocks the project makes are
# edges of pclk delayed further, so they get the same relationship to sys. Between pclk and
# project clocks the default (same waveform) relationship stands.
foreach c [get_clocks {{pclk project_clk*}}] {{
    set_multicycle_path -setup -start {settle} -from [get_clocks sys] -to $c
    set_multicycle_path -hold -start {settle} -from [get_clocks sys] -to $c
    set_multicycle_path -setup -end {settle} -from $c -to [get_clocks sys]
    set_multicycle_path -hold -end {settle_minus_1} -from $c -to [get_clocks sys]
}}
"""

_VIVADO_NETTYPE_ERROR = "[Synth 8-6735]"


def _vivado_rewritten_sources(job: _Job) -> tuple[list[Path], list[Path]]:
    """Copies of src/ with `default_nettype none` read as `wire` (opt-in, recorded)."""
    src = job.sources.root / "src"
    dest = job.out_dir / "src-nettype-wire"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)
    for path in sorted(dest.rglob("*")):
        if path.is_file() and path.suffix in (".v", ".sv", ".vh", ".svh"):
            text = path.read_text(errors="surrogateescape")
            fixed = re.sub(r"`default_nettype\s+none\b", "`default_nettype wire", text)
            if fixed != text:
                path.write_text(fixed, errors="surrogateescape")
                job.rewrites.append(str(src / path.relative_to(dest)))
    files, include_dirs = verilate.project_files(job.sources)
    return (
        [dest / f.relative_to(src) for f in files],
        [dest / d.relative_to(src) for d in include_dirs],
    )


def _vivado_script(job: _Job, files: list[Path], dirs: list[Path]) -> Path:
    out = job.out_dir
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

    def braced(paths: list[Path]) -> str:
        return " ".join("{" + str(p) + "}" for p in paths)

    # The worst paths of every ordered clock pair, setup and hold, with their requirements.
    pair_reports = [
        "foreach a [get_clocks] { foreach b [get_clocks] { if {$a ne $b} {",
        "    foreach {kind tag} {max setup min hold} {",
        "        report_timing -from $a -to $b -delay_type $kind -max_paths 3 \\",
        f'            -file "{out}/$a-to-$b-$tag.rpt"',
        "}}}}",
    ]
    script = out / "build.tcl"
    sv = "-sv " if job.sv else ""
    script.write_text(
        "\n".join(
            [
                f"set_param general.maxThreads {job.jobs}",
                f"read_verilog {sv}{braced(files)}",
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
                (
                    "report_timing_summary -max_paths 10 -report_unconstrained "
                    f"-file {{{out / 'timing.rpt'}}}"
                ),
                *pair_reports,
                f"write_checkpoint -force {{{out / (job.top + '.dcp')}}}",
                f"write_bitstream -force {{{job.unchecked_bit}}}",
            ]
        )
        + "\n"
    )
    return script


def _vivado_implement(job: _Job) -> str:
    """Synthesise, implement and write the bitstream; return the timing summary.

    The returned text starts with the `tt-harness project clock:` lines of the run.
    """
    settings = tools.vivado_settings()
    if settings is None:
        raise FileNotFoundError(f"Vivado not found at {tools.VIVADO_SETTINGS}")
    out = job.out_dir
    vivado_log = out / "vivado.log"
    files, dirs = verilate.project_files(job.sources)
    for attempt in ("unmodified", "nettype-rewrite"):
        script = _vivado_script(job, files, dirs)
        # Vivado's own environment stays in this subshell; its log goes in out_dir.
        shell = (
            f'source "{settings}" && exec vivado -mode batch -nojournal '
            f'-log "{vivado_log}" -source "{script}"'
        )
        start = job.log.stat().st_size
        rc = verilate.run(["bash", "-c", shell], job.log, out, STEP_TIMEOUT_S)
        text = _log_since(job.log, start)
        nettype = [ln for ln in text.splitlines() if _VIVADO_NETTYPE_ERROR in ln]
        if rc == 0 or attempt != "unmodified" or not nettype:
            break
        if not job.vivado_nettype_rewrite:
            raise BuildError(
                f"{job.sources.project.id}: Vivado rejects the unmodified sources: "
                f"{nettype[0].strip()}; Vivado cannot relax this (message 8-6735 cannot be "
                "downgraded); vivado_nettype_rewrite=True (--vivado-nettype-rewrite) builds "
                "from copies with `default_nettype wire`",
                job.log,
            )
        with job.log.open("a") as f:
            f.write("\ntt-harness: retrying Vivado with `default_nettype wire` copies\n")
        files, dirs = _vivado_rewritten_sources(job)
    job.inputs.update({str(f): _sha256(f) for f in files if f.suffix not in (".vh", ".svh")})
    if rc != 0:
        first = re.search(r"^ERROR\b.*$", text, re.MULTILINE)
        detail = f": {first.group(0)}" if first else ""
        raise BuildError(
            f"{job.sources.project.id}: Vivado (synthesis, implementation, bitstream) failed "
            f"(exit {rc}){detail}",
            job.log,
        )
    timing_rpt = out / "timing.rpt"
    if not timing_rpt.is_file():
        raise BuildError(f"{job.sources.project.id}: Vivado wrote no timing report", job.log)
    mapping = [ln for ln in text.splitlines() if ln.startswith("tt-harness project clock: ")]
    return "\n".join(mapping) + "\n" + timing_rpt.read_text()


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
    vivado_nettype_rewrite: bool = False,
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

    # The language rule of the pre-screen (and so of the reference): Verilog-2005 unless a
    # source is .sv or the plain parse fails.
    try:
        screen = prescreen.check(sources, out_dir / "prescreen")
    except prescreen.PrescreenError as e:
        raise BuildError(f"{project.id}: pre-screen (yosys) failed: {e}", log) from e
    language = SYSTEMVERILOG if sources.needs_sv or screen.sv_retried else VERILOG
    with log.open("a") as f:
        f.write(
            f"tt-harness: sources read as {language}; pre-screen derived_clocks "
            f"{screen.derived_clocks}, latches {screen.latches}, loops {screen.comb_loops}\n"
        )

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
        language=language,
        vivado_nettype_rewrite=vivado_nettype_rewrite,
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
    clocks_made = project_clocks(timing)
    with log.open("a") as f:
        for name, d in sorted(timing.items()):
            f.write(f"tt-harness timing: {name}: {_describe(d)}, met\n")
        if bool(clocks_made) != bool(screen.derived_clocks):
            f.write(
                f"tt-harness: warning: the tools found project clocks {clocks_made}, the "
                f"pre-screen {screen.derived_clocks} derived clock(s)\n"
            )
        for path in job.rewrites:
            f.write(f"tt-harness: warning: {path} was given to {toolchain} modified\n")
    return BuildResult(
        bitstream,
        toolchain,
        job.part,
        timing,
        log,
        project_clocks=clocks_made,
        language=language,
        prescreen_derived_clocks=screen.derived_clocks,
        inputs=dict(job.inputs),
        source_rewrites=tuple(job.rewrites),
    )
