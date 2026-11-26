# SPDX-License-Identifier: Apache-2.0
"""Real builds of the project-clock module (spike B): one small design per board, both flows.

The design is `ClockTop`: the board oscillator, a `ProjectClock` at 25.175 MHz enabled by a
button, a 24-bit counter in the project-clock domain and a 24-bit counter in the input-clock
domain, each driving output pins. On the Game Bub every one of the twelve buttons reaches
logic, so the platform's `PULLTYPE PULLUP` on all of them is visible in the results.

This is a separate, small driver and not `tt_harness.build.build_compare`: that function is
built around a project in a slot with clocks named sys/pclk/project (a recorder's pclk
register through a BUFG, project clocks made by project logic). Here the clocks are "the input
clock" and "the MMCM output", both made by clock primitives. It reuses `verilate.run` (the
logged tool runner), `tools`, the `BuildError`/`TimingError` and `TimingDomain` types and the
nextpnr "Max frequency" parser of `tt_harness.build`.

The design files (Verilog and XDC) come from the board's LiteX platform (`Platform.build`
with `run=False`), so the platform's pins, IO standards and `PULLTYPE` reach the tools
exactly as LiteX writes them. The tools are then driven directly, as `build.py` does:

- openXC7: yosys (`synth_xilinx -flatten -abc9 -noclkbuf`, so only the design's own BUFGs
  exist), nextpnr-xilinx with a pre-pack script that constrains each clock net (see `PREPACK`),
  `fasm2frames`, `xc7frames2bit`.
- Vivado: synthesis, implementation and bitstream in one batch run, reports and
  `tt-harness` lines written by the script (see `vivado_script`).

Feedback gate. Every build decodes its own bitstream and fails unless the MMCM's CLKFBIN is
fed by its own CLKFBOUT through the tile's dedicated connection (`feedback_gate`): an open-flow
MMCM whose feedback nextpnr-xilinx has tied off builds, meets timing and never locks.

Timing gate. A build passes only when both clock domains have a constraint, each domain's
worst setup slack is not negative, and (Vivado) `check_timing` finds nothing unconstrained.
Inputs (buttons) and outputs (LEDs, test points) have no clock relationship to the board and
are declared false paths in the Vivado constraints; nextpnr-xilinx times neither.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from operator import xor
from pathlib import Path

from litex_boards.platforms import digilent_arty
from migen import Cat, ClockDomain, Instance, Module, Signal, reduce
from migen.genlib.cdc import MultiReg

from tt_harness import mmcm_fasm, tools, verilate
from tt_harness.boards.gamebub import platform as gamebub_platform
from tt_harness.build import (
    CHIPDB,
    NEXTPNR_FMAX,
    PRJXRAY_DB,
    VIVADO_CHECKS,
    BuildError,
    TimingDomain,
    TimingError,
)
from tt_harness.core.clocking import MmcmSetting, ProjectClock, solve

CLOCK_HZ = 25_175_000
COUNTER_BITS = 24
TOP = "top"
SYS_BUFG = "sys_bufg"
STEP_TIMEOUT_S = 3600
JOBS = 16


@dataclass(frozen=True)
class Board:
    name: str
    clk_resource: str
    clk_hz: int
    parts: dict[str, str]  # toolchain -> part name for that flow
    chipdb_part: str
    # (resource name, number) of the button record or single button, the outputs, in order.
    enable: str  # "<resource>[.<subsignal>]"

    def platform(self, toolchain: str):
        if self.name == "gamebub":
            return gamebub_platform.Platform(toolchain=toolchain)
        return digilent_arty.Platform(variant="a7-35", toolchain=toolchain)


ARTY = Board(
    name="arty",
    clk_resource="clk100",
    clk_hz=100_000_000,
    parts={"vivado": "xc7a35ticsg324-1L", "openxc7": "xc7a35tcsg324-1"},
    chipdb_part="xc7a35tcsg324-1",
    enable="user_btn",
)
GAMEBUB = Board(
    name="gamebub",
    clk_resource="clk50",
    clk_hz=50_000_000,
    parts={"vivado": "xc7a100tcsg324-1", "openxc7": "xc7a100tcsg324-1"},
    chipdb_part="xc7a100tcsg324-1",
    enable="buttons.a",
)
BOARDS = {b.name: b for b in (ARTY, GAMEBUB)}


class ClockTop(Module):
    """Oscillator -> ProjectClock; one counter on each of the two clocks."""

    def __init__(self, platform, board: Board):
        clk_in = platform.request(board.clk_resource)
        self.clock_domains.cd_sys = ClockDomain("sys", reset_less=True)
        self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
        # An explicit BUFG for the input clock's logic: synthesis adds no buffers itself.
        self.specials += Instance("BUFG", name=SYS_BUFG, i_I=clk_in, o_O=self.cd_sys.clk)

        self.submodules.pc = pc = ProjectClock(platform, clk_in, board.clk_hz, CLOCK_HZ)
        self.comb += self.cd_project.clk.eq(pc.clk)

        project_count = Signal(COUNTER_BITS)
        free_count = Signal(COUNTER_BITS)
        self.sync.project += project_count.eq(project_count + 1)
        self.sync.sys += free_count.eq(free_count + 1)

        if board is ARTY:
            self.comb += pc.enable.eq(platform.request("user_btn", 0))
            led0, led1 = platform.request("user_led", 0), platform.request("user_led", 1)
            self.comb += [led0.eq(project_count[-1]), led1.eq(free_count[-1])]
        else:
            buttons = platform.request("buttons")
            others = Signal(11)
            sync_others = Signal(11)
            self.comb += [
                pc.enable.eq(buttons.a),
                others.eq(
                    Cat(
                        *(
                            getattr(buttons, n)
                            for n in ("b", "x", "y", "up", "down", "left", "right")
                        ),
                        buttons.l,
                        buttons.r,
                        buttons.start,
                        buttons.select,
                    )
                ),
            ]
            self.specials += MultiReg(others, sync_others, odomain="sys")
            tp = platform.request("tp", 0)
            self.comb += [
                tp[0].eq(project_count[-1]),
                tp[1].eq(free_count[-1] ^ reduce(xor, [sync_others[i] for i in range(11)])),
                tp[2].eq(project_count[-2]),
                tp[3].eq(free_count[-2]),
            ]


@dataclass
class Build:
    board: Board
    toolchain: str
    out_dir: Path
    setting: MmcmSetting
    log: Path
    bitstream: Path | None = None
    facts: dict = field(default_factory=dict)


def generate(
    board: Board, toolchain: str, out_dir: Path, top=None
) -> tuple[Path, Path, MmcmSetting]:
    """Write `top.v` and `top.xdc` for `board` with the LiteX platform; return them.

    `top(platform, board)` makes the design, `ClockTop` unless given. It must have the same
    clock structure (the input clock through a BUFG, one `ProjectClock` as `pc`) for the
    constraints and the timing gate here to apply.
    """
    out_dir = out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    platform = board.platform(toolchain)
    top = (top or ClockTop)(platform, board)
    platform.build(top, run=False, build_dir=str(out_dir), build_name=TOP)
    return out_dir / f"{TOP}.v", out_dir / f"{TOP}.xdc", top.pc.setting


# openXC7 ----------------------------------------------------------------------------------------

PREPACK = """\
# Generated by tt_harness.clock_build. nextpnr-xilinx 0.8.2 does not carry an input clock
# constraint through the IBUF, the MMCM or a clock buffer, so every global clock buffer's
# output net is constrained here from what drives the buffer: the input port (the board's
# clock) or the MMCM's CLKOUT0 (the achieved project clock).
INPUT_MHZ = {input_mhz!r}
PROJECT_MHZ = {project_mhz!r}
BUFFERS = ("BUFG", "BUFGCTRL", "BUFGCE")
for name, cell in ctx.cells:
    if cell.type not in BUFFERS:
        continue
    out_port = "O"
    in_port = "I" if "I" in cell.ports else "I0"
    out_net = cell.ports[out_port].net
    in_net = cell.ports[in_port].net
    driver = in_net.driver.cell if in_net is not None and in_net.driver is not None else None
    if out_net is None or driver is None:
        raise RuntimeError(f"tt-harness: clock buffer {{name}} is not driven or drives nothing")
    if driver.type == "IBUF":
        domain, mhz = "input", INPUT_MHZ
    elif driver.type == "MMCME2_ADV":
        domain, mhz = "project:" + name, PROJECT_MHZ
    else:
        raise RuntimeError(f"tt-harness: clock buffer {{name}} is driven by a {{driver.type}}")
    ctx.addClock(out_net.name, mhz)
    print(f"tt-harness clock: domain {{domain}} net '{{out_net.name}}' {{mhz}} MHz "
          f"(buffer {{name}} type {{cell.type}} driven by {{driver.type}})")
"""


def _run(job: Build, what: str, cmd: list[str], cwd: Path, **kwargs) -> str:
    start = job.log.stat().st_size if job.log.exists() else 0
    rc = verilate.run(cmd, job.log, cwd, STEP_TIMEOUT_S, **kwargs)
    with job.log.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    if rc != 0:
        first = re.search(r"^(?:ERROR|Error|error)\b.*$", text, re.MULTILINE)
        detail = f": {first.group(0)}" if first else ""
        raise BuildError(
            f"{job.board.name} {job.toolchain}: {what} failed (exit {rc}){detail}", job.log
        )
    return text


def tool(name: str) -> Path:
    path = tools.find(name)
    if path is None:
        raise FileNotFoundError(f"{name} not found; run: uv run scripts/setup_toolchains.py")
    return path


def openxc7_build(board: Board, out_dir: Path, top=None, prepack_extra: str = "") -> Build:
    """Yosys, nextpnr-xilinx, fasm2frames, xc7frames2bit. Raises `BuildError` on a tool failure.

    `prepack_extra` is Python appended to the nextpnr pre-pack script (`PREPACK`).
    """
    out_dir = out_dir.resolve()
    top_v, xdc, setting = generate(board, "openxc7", out_dir, top)
    job = Build(board, "openxc7", out_dir, setting, out_dir / "build.log")
    job.log.write_text("")
    part = board.parts["openxc7"]
    ys = out_dir / "synth.ys"
    ys.write_text(
        "\n".join(
            [
                f"read_verilog {top_v}",
                f"synth_xilinx -flatten -abc9 -noclkbuf -arch xc7 -top {TOP}",
                "delete t:$scopeinfo t:$print t:$check",
                f"tee -o {out_dir / 'utilization-yosys.txt'} stat",
                f"write_json {out_dir / (TOP + '.json')}",
            ]
        )
        + "\n"
    )
    _run(job, "synthesis (yosys)", [str(tool("yosys")), "-s", str(ys)], out_dir)
    prepack = out_dir / "clocks.py"
    prepack.write_text(
        PREPACK.format(input_mhz=board.clk_hz / 1e6, project_mhz=setting.achieved_hz / 1e6)
        + prepack_extra
    )
    chipdb = CHIPDB / f"{board.chipdb_part}.bin"
    if not chipdb.is_file():
        raise FileNotFoundError(f"no nextpnr-xilinx chip database {chipdb}")
    text = _run(
        job,
        "place and route (nextpnr-xilinx)",
        [
            str(tool("nextpnr-xilinx")),
            "--chipdb",
            str(chipdb),
            "--xdc",
            str(xdc),
            "--json",
            str(out_dir / f"{TOP}.json"),
            "--pre-pack",
            str(prepack),
            "--fasm",
            str(out_dir / f"{TOP}.fasm"),
            "--seed",
            "1",
        ],
        out_dir,
    )
    (out_dir / "nextpnr.log").write_text(text)
    db = PRJXRAY_DB / "artix7"
    frames = out_dir / f"{TOP}.frames"
    _run(
        job,
        "fasm2frames",
        [
            str(tool("fasm2frames")),
            "--part",
            part,
            "--db-root",
            str(db),
            str(out_dir / f"{TOP}.fasm"),
        ],
        out_dir,
        stdout=frames,
    )
    bit = out_dir / f"{TOP}.bit"
    _run(
        job,
        "xc7frames2bit",
        [
            str(tool("xc7frames2bit")),
            "--part_file",
            str(db / part / "part.yaml"),
            "--part_name",
            part,
            "--frm_file",
            str(frames),
            "--output_file",
            str(bit),
        ],
        out_dir,
    )
    job.bitstream = bit
    job.facts["nextpnr"] = text
    job.facts["feedback"] = feedback_gate(job)
    return job


def parse_nextpnr(
    text: str, board: Board, setting: MmcmSetting, log: Path
) -> dict[str, TimingDomain]:
    """Per-domain timing from the final nextpnr report, keyed `input` and `project:<buffer>`.

    The domains come from the pre-pack's `tt-harness clock:` lines, and each must appear in
    the final report with the target the pre-pack set; anything else is a `TimingError`.
    """
    mapping = {
        m["net"]: (m["domain"], float(m["mhz"]))
        for m in re.finditer(
            r"^tt-harness clock: domain (?P<domain>\S+) net '(?P<net>.*)' (?P<mhz>[\d.]+) MHz",
            text,
            re.MULTILINE,
        )
    }
    if not any(d == "input" for d, _ in mapping.values()):
        raise TimingError("nextpnr: no input clock was constrained", log)
    if not any(d.startswith("project:") for d, _ in mapping.values()):
        raise TimingError("nextpnr: no project clock was constrained", log)
    final = text[text.rindex("Info: Routing complete.") :]
    reported = {m["name"]: m for m in NEXTPNR_FMAX.finditer(final)}
    domains: dict[str, TimingDomain] = {}
    for net, (domain, mhz) in mapping.items():
        m = reported.get(net)
        if m is None:
            raise TimingError(
                f"nextpnr: constrained clock {domain} ({net}) has no timing line", log
            )
        target = float(m["target"])
        if abs(target - mhz) > 1e-3 * mhz:
            raise TimingError(
                f"nextpnr: clock {domain} was timed against {target} MHz, not {mhz}", log
            )
        domains[domain] = TimingDomain(
            constraint_mhz=target,
            achieved_mhz=float(m["achieved"]),
            met=m["verdict"] == "PASS",
            kind="sys" if domain == "input" else "project",
        )
    unconstrained = set(reported) - set(mapping)
    if unconstrained:
        raise TimingError(f"nextpnr: clocks timed at a default: {sorted(unconstrained)}", log)
    return domains


# Vivado -----------------------------------------------------------------------------------------

# The MMCM attributes read back from the implemented design.
MMCM_PROPERTIES = (
    "COMPENSATION",
    "DIVCLK_DIVIDE",
    "CLKFBOUT_MULT_F",
    "CLKOUT0_DIVIDE_F",
    "CLKIN1_PERIOD",
)

VIVADO_TCL = """\
set_param general.maxThreads {jobs}
read_verilog {{{top_v}}}
read_xdc {{{xdc}}}
synth_design -top {top} -part {part}
opt_design
place_design
phys_opt_design
route_design
write_checkpoint -force {{{out}/{top}.dcp}}
report_clocks -file {{{out}/clocks.rpt}}
report_clock_interaction -file {{{out}/clock_interaction.rpt}}
report_utilization -file {{{out}/utilization.rpt}}
report_io -file {{{out}/io.rpt}}
report_drc -file {{{out}/drc.rpt}}
check_timing -verbose -file {{{out}/check_timing.rpt}}
report_timing_summary -max_paths 10 -report_unconstrained -file {{{out}/timing.rpt}}
foreach c [get_clocks] {{
    puts "tt-harness clock: [get_property NAME $c] period [get_property PERIOD $c] generated [get_property IS_GENERATED $c]"
}}
foreach c [get_clocks] {{
    foreach {{kind flag}} {{setup -setup hold -hold}} {{
        set p [get_timing_paths -from $c -to $c -max_paths 1 $flag]
        if {{[llength $p]}} {{
            puts "tt-harness slack: [get_property NAME $c] $kind [get_property SLACK $p]"
        }} else {{
            puts "tt-harness slack: [get_property NAME $c] $kind none"
        }}
    }}
}}
set mmcm [get_cells -hier -filter {{REF_NAME == MMCME2_ADV}}]
foreach prop {{{props}}} {{
    puts "tt-harness mmcm: $prop [get_property $prop $mmcm]"
}}
foreach p [get_ports] {{
    puts "tt-harness port: [get_property NAME $p] LOC [get_property LOC $p] IOSTANDARD [get_property IOSTANDARD $p] PULLTYPE [get_property PULLTYPE $p]"
}}
# Vivado implements the BUFGCE as a BUFGCTRL whose CE0 pin carries the enable.
set gate [get_cells -hier -filter {{REF_NAME == BUFGCTRL}}]
report_property $gate -file {{{out}/bufgce_properties.rpt}}
report_property $mmcm -file {{{out}/mmcm_properties.rpt}}
set ce [get_pins -quiet -of_objects $gate -filter {{REF_PIN_NAME == CE0}}]
puts "tt-harness ce pin: $ce"
set ce_paths [get_timing_paths -quiet -to $ce -max_paths 1 -setup]
if {{[llength $ce_paths]}} {{
    puts "tt-harness ce path: slack [get_property SLACK $ce_paths] from [get_property STARTPOINT_PIN $ce_paths] to [get_property ENDPOINT_PIN $ce_paths] clock [get_property ENDPOINT_CLOCK $ce_paths] requirement [get_property REQUIREMENT $ce_paths]"
}} else {{
    puts "tt-harness ce path: none"
}}
if {{[llength $ce]}} {{
    report_timing -to $ce -delay_type max -file {{{out}/ce_path.rpt}}
    report_timing -to $ce -delay_type min -file {{{out}/ce_path_hold.rpt}}
}}
write_bitstream -force {{{out}/{top}.bit}}
"""

_CLOCK_LINE = re.compile(
    r"^tt-harness clock: (?P<name>\S+) period (?P<period>[\d.]+) generated (?P<gen>\d)$",
    re.MULTILINE,
)
_SLACK_LINE = re.compile(
    r"^tt-harness slack: (?P<name>\S+) (?P<kind>setup|hold) (?P<slack>-?[\d.]+|none)$",
    re.MULTILINE,
)
_VIVADO_CHECK = re.compile(r"^\d+\. checking (\w+) \((\d+)\)\s*$", re.MULTILINE)


def vivado_script(board: Board, out_dir: Path, top_v: Path, xdc: Path) -> Path:
    """The Vivado batch script: implement, write reports and the `tt-harness` fact lines."""
    script = out_dir / "build.tcl"
    script.write_text(
        VIVADO_TCL.format(
            jobs=JOBS,
            top_v=top_v,
            xdc=xdc,
            top=TOP,
            part=board.parts["vivado"],
            out=out_dir,
            props=" ".join(MMCM_PROPERTIES),
        )
    )
    return script


def vivado_build(board: Board, out_dir: Path, top=None) -> Build:
    """Implement with Vivado and write a bitstream. Raises `BuildError` on a tool failure."""
    out_dir = out_dir.resolve()
    top_v, xdc, setting = generate(board, "vivado", out_dir, top)
    job = Build(board, "vivado", out_dir, setting, out_dir / "build.log")
    job.log.write_text("")
    settings = tools.vivado_settings()
    if settings is None:
        raise FileNotFoundError(f"Vivado not found at {tools.VIVADO_SETTINGS}")
    script = vivado_script(board, out_dir, top_v, xdc)
    shell = (
        f'source "{settings}" && exec vivado -mode batch -nojournal '
        f'-log "{out_dir / "vivado.log"}" -source "{script}"'
    )
    text = _run(
        job, "Vivado (synthesis, implementation, bitstream)", ["bash", "-c", shell], out_dir
    )
    job.bitstream = out_dir / f"{TOP}.bit"
    job.facts["vivado"] = text + "\n" + (out_dir / "timing.rpt").read_text()
    job.facts["feedback"] = feedback_gate(job)
    return job


def parse_vivado(
    text: str, board: Board, setting: MmcmSetting, log: Path
) -> dict[str, TimingDomain]:
    """Per-clock timing from the Vivado run's `tt-harness` lines and timing summary.

    Raises `TimingError` unless `check_timing` is clean, the design has exactly the input
    clock (at the board's period) and one generated clock whose period is that of the
    achieved frequency, and both clocks have setup and hold slack figures that are not
    negative (and the summary reports no failing endpoints).
    """
    checks = {m.group(1): int(m.group(2)) for m in _VIVADO_CHECK.finditer(text)}
    absent = [c for c in VIVADO_CHECKS if c not in checks]
    if absent:
        raise TimingError(f"Vivado timing report lacks check_timing {absent}", log)
    bad = {c: checks[c] for c in VIVADO_CHECKS if checks[c]}
    if bad:
        raise TimingError(f"unconstrained: Vivado check_timing reports {bad}", log)
    clocks = {m["name"]: (float(m["period"]), m["gen"] == "1") for m in _CLOCK_LINE.finditer(text)}
    slacks: dict[tuple[str, str], float | None] = {}
    for m in _SLACK_LINE.finditer(text):
        slacks[(m["name"], m["kind"])] = None if m["slack"] == "none" else float(m["slack"])
    input_ns = 1e9 / board.clk_hz
    project_ns = 1e9 / setting.achieved_hz
    feedback_ns = input_ns * setting.divclk
    domains: dict[str, TimingDomain] = {}
    for name, (period, generated) in clocks.items():
        wns, whs = slacks.get((name, "setup")), slacks.get((name, "hold"))
        if generated and abs(period - project_ns) > 1e-3:
            # Vivado also derives a clock for CLKFBOUT, at the phase detector's period (the
            # input period times DIVCLK_DIVIDE): it clocks only the feedback pin, so it must
            # have no timing paths.
            if abs(period - feedback_ns) > 1e-3 or wns is not None or whs is not None:
                raise TimingError(f"unexpected generated clock {name} ({period} ns)", log)
            continue
        key = "project" if generated else "input"
        want = project_ns if generated else input_ns
        if abs(period - want) > 1e-3:
            raise TimingError(f"clock {name} has period {period} ns, not {want:.4f}", log)
        if key in domains:
            raise TimingError(f"two {key} clocks: {name}", log)
        if wns is None or whs is None:
            raise TimingError(f"clock {name} has no setup or hold paths of its own", log)
        domains[key] = TimingDomain(
            constraint_mhz=1000.0 / period,
            achieved_mhz=1000.0 / (period - wns),
            met=wns >= 0 and whs >= 0,
            kind="project" if generated else "sys",
            setup_slack_ns=wns,
            hold_slack_ns=whs,
        )
    if sorted(domains) != ["input", "project"]:
        raise TimingError(f"expected an input and a project clock, found {clocks}", log)
    if "Timing constraints are not met." in text:
        raise TimingError("Vivado reports timing constraints not met", log)
    return domains


# What the bitstream says ---------------------------------------------------------------------


def decode_bitstream(bit: Path, part: str, out: Path, log: Path) -> Path:
    """Decode a `.bit` back to FASM with `bit2fasm` (openXC7); the FASM is written to `out`.

    Run through `verilate.run`: TMPDIR is `<repo>/tmp`, the command line and its standard
    error are appended to `log`. A non-zero exit raises `BuildError`.
    """
    cmd = [
        str(tool("bit2fasm")),
        "--db-root",
        str(PRJXRAY_DB / "artix7"),
        "--part",
        part,
        "--bitread",
        str(tool("bitread")),
        str(bit),
    ]
    rc = verilate.run(cmd, log, out.parent, STEP_TIMEOUT_S, stdout=out)
    if rc != 0:
        raise BuildError(f"decoding {bit}: bit2fasm failed (exit {rc})", log)
    return out


def decoded_path(job: Build) -> Path:
    """Where `feedback_gate` leaves the build's bitstream decoded back to FASM."""
    return job.out_dir / f"{TOP}.decoded.fasm"


def feedback_gate(job: Build) -> str:
    """Decode the build's bitstream and require the MMCM's dedicated feedback.

    Every build here ends with this, whichever the toolchain and board: the bitstream is
    decoded with `bit2fasm` to `decoded_path(job)` and the source of the MMCM's CLKFBIN must
    be its own CLKFBOUT (`mmcm_fasm.check_feedback`). Returns that source's name; raises
    `BuildError` otherwise, because such a bitstream does not lock and no tool says so.
    """
    # The Vivado part of the Arty (-1L) is not in the prjxray database; it is the same die.
    decode_bitstream(job.bitstream, job.board.parts["openxc7"], decoded_path(job), job.log)
    try:
        return mmcm_fasm.check_feedback(decoded_path(job).read_text())
    except mmcm_fasm.FeedbackError as e:
        raise BuildError(f"{job.board.name} {job.toolchain}: {e}", job.log) from e


def generate_setting(board: Board) -> MmcmSetting:
    """The MMCM setting the design uses on `board`."""
    return solve(board.clk_hz, CLOCK_HZ)


def mmcm_values(fasm: str) -> dict[str, int]:
    """MMCME2_ADV features as integers: the bit slices of one field are summed
    (`FIELD[3:0] = ..`, `FIELD[2]` and `FIELD` all add into `FIELD`), so differently sliced
    encodings of the same value compare equal."""
    out: dict[str, int] = {}
    for line in fasm.splitlines():
        if ".MMCME2_ADV." not in line:
            continue
        name = line.split(".MMCME2_ADV.")[1].strip()
        m = re.match(r"([\w.]+?)(?:\[(\d+)(?::(\d+))?\])?(?: = \d+'b(\w+))?$", name)
        if m is None:
            raise ValueError(f"cannot read FASM line {line!r}")
        base, hi, lo, val = m.groups()
        if val is not None:
            v = int(val, 2) << int(lo)
        elif hi is not None:
            v = 1 << int(hi)
        else:
            v = 1
        out[base] = out.get(base, 0) + v
    return out
