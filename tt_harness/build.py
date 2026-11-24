# SPDX-License-Identifier: Apache-2.0
"""Compare builds: bitstreams with openXC7 or Vivado, and a timing gate.

The tools are driven directly (as `tests/toolchain/test_blink.py` does) rather than through
LiteX's build backends, because both flows need things those backends do not give: openXC7
must constrain the BUFG-derived project clock per clock (LiteX constrains only the input clock
and passes `--timing-allow-fail`), and Vivado needs generated clocks and multicycle paths
written after synthesis, when the `pclk` register and any clocks the project makes exist. The
board layer supplies pins (from litex-boards), parts and the BUFGs.

Clocks, with S = `SETTLE` system cycles and T the system period:

- `sys`: the board oscillator.
- `pclk`: the recorder's `pclk` register through a BUFG, constrained at its shortest real
  period, 2 * (S + 1) * T. This times the project's own logic at that rate, not at the
  project's real clock.
- project clocks: any other clock the tools find, made by the project's own logic. Timing
  keys are `project:<source>`. Vivado propagates pclk through combinational gates itself,
  so every root found is made by sequential cells; `tt_harness.project_clocks` sorts each
  root by where its edges can fall (`TimingDomain.root_kind`), from the cells both flows
  print for it.
- sys <-> pclk and sys <-> project clocks: S system cycles from a sys-side change to the
  next project edge and from a project edge to the sample (`tt_harness.compare.recorder`).

Project clocks in Vivado. P is the pclk period; pclk rises at 0 and falls at P/2. Vivado's
constraints for each root class, and why, edge by edge:

- `register` (edges only on pclk rising edges): `create_generated_clock -edges {1 3 5}`
  from the pclk BUFG output, so the clock rises at 0 and falls at P, both on pclk rising
  edges, with pclk's insertion delay and the delay to the root in its latency.
  - pclk rise -> either edge of the clock: setup P, hold 0 (the edge may be the very pclk
    edge that launched the data). The waveforms give exactly this for both edges.
  - pclk fall -> either edge: setup P/2, hold -P/2. Either edge -> pclk rise: P and 0;
    -> pclk fall: P/2 and -P/2. All from the waveforms.
  - within the clock: same edge 2P and 0, opposite edges P and -P; two edges of one such
    clock are never closer than P.
  - between two different `register` clocks, A -> B: on hardware any edge of B can be one
    pclk period after any edge of A, or on the same pclk edge. The 2P waveforms would give
    2P for same-edge paths and a hold of -P for opposite edges, so every A -> B path gets
    `set_max_delay P` and `set_min_delay 0` (without `-datapath_only`: both clocks'
    insertion delays stay in the check, and hold stays analysed). A multicycle path cannot
    express this: it moves a requirement by whole periods of these 2P clocks.
- `derived` (edges on pclk rising or falling edges): `create_generated_clock -divide_by 1`
  from the pclk BUFG output, pclk's own waveform in whichever sense Vivado traces. That
  waveform alone would be wrong between clocks (a falling edge of the clock that really
  lies on a pclk rising edge would get a hold requirement of -P/2), and a clock at twice
  pclk's frequency is no better (-P/4). So every path between the clock and pclk or any
  other project clock, both ways, gets `set_max_delay P/2` and `set_min_delay 0`:
  - setup P/2: two different pclk edges are at least P/2 apart, whichever edges launch and
    capture;
  - hold 0: the capturing edge may be the pclk edge that launched the data.
  - within the clock the waveform stands: same edge P, opposite edges P/2 (hold -P/2);
    its edges are pclk edges, so two of them are never closer than P/2.
  - with sys, the sys <-> pclk multicycles apply to both of its edges: setup S system
    periods either way (the least it can be), hold -1 system period from sys (a sys-side
    change comes one system cycle after a pclk falling edge, later after a rising one) and
    0 to sys.
- `unclassified` (a latch, an asynchronous set or clear, pclk through logic, a cell not
  clocked by pclk, ...): the `derived` constraints, as an assumption: the build completes,
  the domain has `signed_off` False and the result lists it in `limitations` with what was
  assumed. Wherever pclk reaches the root (a flip-flop with an asynchronous reset on pclk,
  say) it is still a clock generated from pclk, so pclk's insertion delay is in its
  latency. Only if pclk does not reach the root at all is it a primary clock with pclk's
  waveform; it then has no insertion delay relative to pclk and sys, and its cross-domain
  figures are indicative only (a root made by sys-domain logic also has its edges at
  sys-side times, not on pclk edges).

openXC7 constrains every project clock at the pclk frequency. nextpnr-xilinx 0.8.2 times
cells on a clock's falling edge as if on its rising edge, so a clock with users on both
edges is constrained at twice its frequency (every path, half-cycle ones included, is
checked against half the period; the domain says `half_cycle`), and a path between pclk and
a project clock, or between two project clocks, is budgeted at half a pclk period when
either clock has any falling-edge user or is not of the `register` class.

The gate raises `TimingError` when a clock in the design is missing from the report, a clock
in the report has no constraint (it was timed at a default), a clock was timed against a
different target than the one given, a sys<->pclk pair is missing, or any domain or pair
misses. On failure no bitstream is left at `BuildResult.bitstream`'s path.

Sources. Project sources are read as Verilog-2005 unless the design needs SystemVerilog (a
`.sv` file, or the plain Yosys parse fails), the rule the pre-screen uses; Vivado follows
the mode Yosys needed ("verilog-2005" is Vivado's Verilog-2001 reader, `read_verilog`;
"systemverilog" is `read_verilog -sv`). openXC7 builds always read the fetched files
unmodified. Vivado rejects a non-ANSI port without a separate net declaration (`input clk;`)
under `` `default_nettype none `` (Synth 8-6735, an error that cannot be downgraded), which
most Tiny Tapeout VGA designs contain; only when such a message names a project file does
the Vivado flow retry from copies in which each `` `default_nettype none `` directive, and
nothing else, reads `` `default_nettype wire ``. Yosys has already parsed the unmodified
sources with `none` in force and rejects any genuinely undeclared net, so the rewrite can
only relax Vivado's port-declaration strictness. On the retry Vivado logs Synth 8-11241
("undeclared symbol ..., assumed default net type 'wire'") for names used before their
declaration and then uses their full declared width; the retry fails if such a message
names a symbol that no project source declares. The copies are listed in
`BuildResult.source_rewrites`; `vivado_nettype_rewrite=False` forbids the retry.

Files under `out_dir`: `build.log` (every tool's output), the generated top, constraints and
scripts, reports, and `<top>.bit` on success.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tt_harness import prescreen, tools, verilate
from tt_harness import project_clocks as pc
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

# Standing facts of every openXC7 build, in fixed wording for quoting.
LIMIT_NO_HOLD = "openXC7: nextpnr-xilinx performs no hold analysis"
LIMIT_CROSS_BUDGET = (
    "openXC7: cross-domain checks are harness budgets on nextpnr's data-path delays, "
    "assuming clock-insertion skew under 10 ns (one system period)"
)
LIMIT_LOCAL_CLOCKS = "openXC7: project clocks are on local routing with unmeasured skew"
# Per-build entries, with the toolchain named as in the standing facts.
TOOL_NAMES = {"openxc7": "openXC7", "vivado": "Vivado"}
LIMIT_NOT_SIGNED_OFF = "{tool}: project clock {clock} is not signed off: {reason}; {assumption}"
LIMIT_GATE = (
    "{tool}: project clock {clock} is a gate of {n} flip-flops; a glitch at the gate is not timed"
)
LIMIT_NO_PATHS = "{tool}: {key} has no paths within its own clock"


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
    # A clock: its constraint. A clock pair: 1000 / the setup requirement in ns.
    constraint_mhz: float
    # The frequency the clock could run at with its worst setup path just closing, so it
    # compares with `constraint_mhz`: `constraint_mhz * requirement / (requirement - slack)`
    # with that path's own requirement (which is not the period for a half-cycle path).
    # None where the requirement is not known (nextpnr cross-domain entries, Vivado entries
    # without a pair report) or there are no paths.
    achieved_mhz: float | None
    met: bool
    # "cross" marks a clock pair (`a->b`), the others a clock.
    kind: Kind
    # Worst setup and hold slack in ns, where the tool reports them.
    setup_slack_ns: float | None = None
    hold_slack_ns: float | None = None
    # The clock has users on both edges and openXC7 checked every path at half its period
    # (constraint_mhz is then twice the clock's frequency).
    half_cycle: bool = False
    # False when the clock has no paths within its own domain (only cross-domain ones).
    intra_paths: bool = True
    # The setup requirement in ns of the worst path (Vivado, from the pair reports), or the
    # budget its data-path delay was checked against (nextpnr clock pairs); None if unknown.
    requirement_ns: float | None = None
    # Project clocks only: where the edges of the clock's root can fall ("register": only on
    # pclk rising edges; "derived": on either pclk edge; "unclassified": not known), why,
    # and how many flip-flops the root is a gate of (`tt_harness.project_clocks`).
    root_kind: pc.RootKind | None = None
    root_reason: str | None = None
    root_assumption: str | None = None  # what its timing rests on when not signed off
    root_gate_of: int | None = None
    # False when the clock's timing model is an assumption rather than derived from the
    # netlist (an "unclassified" root). Such a domain can be `met` and still not be signed
    # off; `BuildResult.limitations` names it.
    signed_off: bool = True


@dataclass(frozen=True)
class BuildResult:
    bitstream: Path
    toolchain: str
    part: str
    timing: dict[str, TimingDomain]
    log: Path
    project_clocks: tuple[str, ...] = ()  # sources of clocks made by the project's logic
    language: str = VERILOG  # the mode the flow read the project sources in
    prescreen_derived_clocks: int = 0  # `prescreen.check(sources).derived_clocks`
    inputs: dict[str, str] = field(default_factory=dict)  # project file -> SHA-256 given
    # Project files given to the tool modified, with the number of `default_nettype none`
    # directives changed in each (Vivado only, and only when it needed it).
    source_rewrites: tuple[tuple[str, int], ...] = ()
    # What this build's timing result does not cover, in fixed wording.
    limitations: tuple[str, ...] = ()


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
        if src == "sys" and dst == "sys":
            return self.sys_period_ns if same_edge else self.sys_period_ns / 2
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
    r"^tt-harness clock: domain (?P<domain>\S+) net '(?P<net>.*)' (?P<mhz>[\d.]+) MHz"
    r"(?: \(cells (?P<cells>\d+) rising (?P<rising>\d+) falling (?P<falling>\d+)"
    r" latches (?P<latches>\d+)(?: intra (?P<intra>yes|no))?(?P<half> half-cycle)?;.*\))?.*$",
    re.MULTILINE,
)
# The clock-name column is padded to the longest name, inside the quotes or after them.
NEXTPNR_FMAX = re.compile(
    r"^(?:Info|ERROR|Warning): Max frequency for clock\s+'\s*(?P<name>.+?)\s*':\s+"
    r"(?P<achieved>[\d.]+) MHz \((?P<verdict>PASS|FAIL) at (?P<target>[\d.]+) MHz\)\s*$",
    re.MULTILINE,
)
_NEXTPNR_DELAY = re.compile(
    r"^Info: Max delay (?P<from>.+?)\s+->\s+(?P<to>.+?)\s*:\s+(?P<delay>[\d.]+) ns\s*$",
    re.MULTILINE,
)
_NEXTPNR_ROUTED = "Info: Routing complete."


@dataclass(frozen=True)
class _NextpnrClock:
    key: str
    cells: int | None  # clocked cells on the net; None in logs without the counts
    half_cycle: bool
    # Whether data can pass from one of its cells to another (None: not reported).
    intra: bool | None = None
    falling: int | None = None  # cells on its falling edge; None in logs without the counts
    # Project clocks: the class of the net's root.
    root: pc.Classified | None = None

    @property
    def half_budget(self) -> bool:
        """Whether a path between this clock and another may have only half a pclk period:
        it has (or may have) a falling-edge user, which nextpnr prints as `posedge`, or its
        own edges may lie on pclk falling edges."""
        if self.falling is None or self.falling > 0:
            return True
        return self.root is not None and self.root.kind != pc.REGISTER


def _edge_clock(text: str) -> tuple[str, str] | None:
    """'posedge clk0' -> ('posedge', 'clk0'); '<async>' -> None."""
    text = text.strip()
    if text.startswith("<"):
        return None
    edge, _, name = text.partition(" ")
    if edge not in ("posedge", "negedge") or not name:
        raise ValueError(f"unexpected clock edge {text!r} in nextpnr report")
    return edge, name.strip()


_NO_ROOT_FACTS = pc.Classified(
    pc.UNCLASSIFIED, "the build did not report what drives it", False, 0, pc.ASSUME_EDGES
)


def _root_fields(root: pc.Classified) -> dict:
    """The `TimingDomain` fields of a project clock whose root is `root`."""
    return {
        "root_kind": root.kind,
        "root_reason": root.reason,
        "root_assumption": root.assumption,
        "root_gate_of": root.gate_of,
        "signed_off": root.signed_off,
    }


def _nextpnr_clocks(text: str, log: Path) -> dict[str, _NextpnrClock]:
    """Net name -> clock, from the pre-pack script's `tt-harness clock` lines."""
    try:
        roots = pc.classify(pc.parse_roots(text))
    except ValueError as e:
        raise TimingError(str(e), log) from e
    nets = {}
    for m in _NEXTPNR_CLOCK_MAP.finditer(text):
        domain, net = m.group("domain"), m.group("net")
        project = domain == "project"
        nets[net] = _NextpnrClock(
            key=f"{PROJECT}{net}" if project else domain,
            cells=int(m.group("cells")) if m.group("cells") is not None else None,
            half_cycle=bool(m.group("half")),
            intra=None if m.group("intra") is None else m.group("intra") == "yes",
            falling=int(m.group("falling")) if m.group("falling") is not None else None,
            root=roots.get(net, _NO_ROOT_FACTS) if project else None,
        )
    return nets


def parse_nextpnr(text: str, plan: ClockPlan, log: Path) -> dict[str, TimingDomain]:
    """Timing per domain from a nextpnr-xilinx log (the post-route report is used).

    Net names are mapped to domains by the `tt-harness clock:` lines that the pre-pack
    script prints, and project clocks are classified from its `tt-harness clock root:`
    lines. Raises `TimingError` for an unmapped clock, a mapped clock missing from the
    report (unless the script found no path between its cells, or it clocks a single cell
    and the script did not say), or a target other than the plan's (twice the plan's for a
    half-cycle clock).
    """
    nets = _nextpnr_clocks(text, log)
    keys = {c.key: c for c in nets.values()}
    routed = text.rfind(_NEXTPNR_ROUTED)
    if routed < 0:
        raise TimingError("nextpnr log has no post-route timing report", log)
    report = text[routed:]

    timing: dict[str, TimingDomain] = {}
    for m in NEXTPNR_FMAX.finditer(report):
        net = m.group("name")
        clock = nets.get(net)
        if clock is None:
            raise TimingError(
                f"unconstrained: clock net '{net}' is not a constrained clock of the design "
                f"(nextpnr timed it at its default {m.group('target')} MHz)",
                log,
            )
        kind = _kind(clock.key)
        expected = plan.mhz(kind) * (2 if clock.half_cycle else 1)
        target = float(m.group("target"))
        if abs(target - expected) > 0.01:
            raise TimingError(
                f"unconstrained: clock {clock.key} ('{net}') was timed at {target} MHz, "
                f"not {expected:.3f} MHz",
                log,
            )
        achieved = float(m.group("achieved"))
        timing[clock.key] = TimingDomain(
            constraint_mhz=expected,
            achieved_mhz=achieved,
            met=m.group("verdict") == "PASS" and achieved >= target,
            kind=kind,
            setup_slack_ns=1000.0 / target - 1000.0 / achieved,
            half_cycle=clock.half_cycle,
            requirement_ns=1000.0 / expected,
            **(_root_fields(clock.root) if clock.root is not None else {}),
        )

    allowance = CROSS_DOMAIN_ALLOWANCE_CYCLES * plan.sys_period_ns
    worst: dict[str, tuple[float, float]] = {}  # key -> (slack, budget)
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
        if None in ends:
            continue  # I/O paths are not clock pairs
        (src_edge, src), (dst_edge, dst) = ends
        if src.key == dst.key and src_edge == dst_edge:
            continue  # within one clock: covered by its Fmax line
        # A whole period only when both ends are known to be on rising edges that are a
        # whole period apart (the printed edges are no evidence: a falling-edge capture is
        # printed as `posedge`).
        same_edge = src_edge == dst_edge
        if src.key != dst.key and "sys" not in (src.key, dst.key):
            same_edge = same_edge and not (src.half_budget or dst.half_budget)
        budget = plan.requirement_ns(_kind(src.key), _kind(dst.key), same_edge) - allowance
        key = f"{src.key}->{dst.key}" + ("" if src.key != dst.key else " (opposite edges)")
        slack = budget - float(m.group("delay"))
        if key not in worst or slack < worst[key][0]:
            worst[key] = (slack, budget)
    for key, (slack, budget) in worst.items():
        timing[key] = TimingDomain(
            constraint_mhz=1000.0 / budget if budget > 0 else float("inf"),
            achieved_mhz=None,  # only a data-path delay against a harness budget
            met=slack >= 0,
            kind="cross",
            setup_slack_ns=slack,
            requirement_ns=budget,
        )

    for key, clock in sorted(keys.items()):
        if key in timing:
            continue
        single = clock.cells is not None and clock.cells <= 1
        if clock.root is not None and (clock.intra is False or (clock.intra is None and single)):
            # No cell on this clock can take data from another one on it, or from itself
            # (the pre-pack script walked the netlist), so it has no paths of its own to
            # report. Without the script's word, only a single cell is taken to have none.
            timing[key] = TimingDomain(
                constraint_mhz=plan.pclk_mhz,
                achieved_mhz=None,
                met=True,
                kind="project",
                intra_paths=False,
                **_root_fields(clock.root),
            )
            continue
        raise TimingError(
            f"unconstrained: clock domain {key} ({clock.cells} clocked cells, intra-clock paths "
            f"{'unknown' if clock.intra is None else 'possible'}) absent from "
            "the timing report",
            log,
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
_VIVADO_PROJECT_CLOCK_PREFIX = "tt-harness project clock: "
_VIVADO_PROJECT_CLOCK = re.compile(
    r"^tt-harness project clock: (?P<name>\S+) on (?P<root>\S+) "
    r"kind (?P<kind>register|derived|unclassified)$",
    re.MULTILINE,
)
_VIVADO_REQUIREMENT = re.compile(
    r"^tt-harness requirement: (?P<a>\S+) -> (?P<b>\S+) setup (?P<ns>-?[\d.]+) ns$",
    re.MULTILINE,
)
_VIVADO_NO_PATHS = re.compile(r"^tt-harness no paths: (?P<a>\S+) -> (?P<b>\S+)$", re.MULTILINE)


def table_rows(text: str, title: str) -> list[list[str]]:
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


def ns_value(text: str) -> float | None:
    return None if text == "NA" else float(text)


def _achieved(constraint_mhz: float, requirement: float | None, wns: float | None) -> float | None:
    """The frequency a clock constrained at `constraint_mhz` could run at, from a path with
    setup requirement `requirement` and slack `wns` (see `TimingDomain.achieved_mhz`)."""
    if requirement is None or wns is None or requirement <= 0 or requirement - wns <= 0:
        return None
    return constraint_mhz * requirement / (requirement - wns)


def parse_vivado(text: str, plan: ClockPlan, log: Path) -> dict[str, TimingDomain]:
    """Timing per clock and per clock pair from a Vivado `report_timing_summary`.

    Project clocks are named `project_clk<n>` in Vivado. The lines the flow prepends to the
    report say the rest: `tt-harness project clock:` maps each to its root pin and the class
    it was constrained as, the `tt-harness clock root:` lines give what drives each root
    (the class is derived again here and must agree), `tt-harness requirement:` gives the
    requirement of a pair's worst setup path and `tt-harness no paths:` a pair whose report
    has no paths. Raises `TimingError` if `check_timing` found untimed logic, the report
    lists a clock that is neither sys, pclk nor a mapped project clock, a clock's period
    differs from what its class calls for, a mapped clock is absent, a clock reports setup
    or hold figures for only one of the two, or a clock has neither (unless it is a project
    clock whose own pair report says it has no paths).
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

    try:
        classes = pc.classify(pc.parse_roots(text))
    except ValueError as e:
        raise TimingError(str(e), log) from e
    names = {"sys": "sys", "pclk": "pclk"}
    expected_period = {"sys": plan.sys_period_ns, "pclk": plan.pclk_period_ns}
    roots: dict[str, pc.Classified] = {}
    for line in text.splitlines():
        if not line.startswith(_VIVADO_PROJECT_CLOCK_PREFIX):
            continue
        m = _VIVADO_PROJECT_CLOCK.match(line)
        if m is None:
            raise TimingError(f"cannot read the project clock line {line!r}", log)
        name, root, kind = m.group("name"), m.group("root"), m.group("kind")
        if root not in classes:
            raise TimingError(f"no clock root facts for project clock {name} on {root}", log)
        if classes[root].kind != kind:
            raise TimingError(
                f"project clock {name} on {root} was constrained as {kind}, but its root is "
                f"{classes[root].kind} ({classes[root].reason})",
                log,
            )
        names[name] = f"{PROJECT}{root}"
        roots[name] = classes[root]
        # A register clock has both edges on pclk rising edges: twice pclk's period.
        factor = 2 if kind == pc.REGISTER else 1
        expected_period[name] = factor * plan.pclk_period_ns
    requirements = {(m["a"], m["b"]): float(m["ns"]) for m in _VIVADO_REQUIREMENT.finditer(text)}
    no_paths = {(m["a"], m["b"]) for m in _VIVADO_NO_PATHS.finditer(text)}
    periods = {}
    for row in table_rows(text, "Clock Summary"):
        name, period = row[0], float(row[-2])
        if name not in names:
            raise TimingError(f"unconstrained: unexpected clock {name} in the design", log)
        if abs(period - expected_period[name]) > 0.001:
            raise TimingError(
                f"clock {name} has period {period} ns, not {expected_period[name]:.3f}", log
            )
        periods[name] = period

    timing: dict[str, TimingDomain] = {}
    for row in table_rows(text, "Intra Clock Table"):
        name = row[0]
        if name not in periods:
            raise TimingError(f"clock {name} in the intra-clock table has no period", log)
        if len(row) == 13:
            wns, whs, wpws = ns_value(row[1]), ns_value(row[5]), ns_value(row[9])
        elif len(row) == 5:
            # Vivado leaves the setup and hold columns empty for a clock with no paths of
            # its own and fills in only its pulse width.
            wns, whs, wpws = None, None, ns_value(row[1])
        else:
            raise TimingError(f"cannot read the intra-clock row for {name}: {row}", log)
        if (wns is None) != (whs is None):
            raise TimingError(f"clock {name} reports only one of setup and hold slack", log)
        if wns is None and not (name in roots and (name, name) in no_paths):
            # sys and pclk always have paths of their own; a project clock may have none,
            # but only on the word of its own pair report.
            raise TimingError(
                f"clock {name} has no setup or hold figures"
                + (" and no pair report saying it has no paths" if name in roots else ""),
                log,
            )
        key = names[name]
        constraint = 1000.0 / periods[name]
        requirement = requirements.get((name, name))
        timing[key] = TimingDomain(
            constraint_mhz=constraint,
            achieved_mhz=_achieved(constraint, requirement, wns),
            met=(wns is None or (wns >= 0 and whs >= 0)) and wpws is not None and wpws >= 0,
            kind=_kind(key),
            setup_slack_ns=wns,
            hold_slack_ns=whs,
            intra_paths=wns is not None,
            requirement_ns=requirement,
            **(_root_fields(roots[name]) if name in roots else {}),
        )

    def pair(row_src: str, row_dst: str, wns, whs, suffix: str, req_known: bool) -> None:
        if row_src not in periods or row_dst not in periods:
            raise TimingError(
                f"inter-clock path {row_src} -> {row_dst} involves an unknown clock", log
            )
        if wns is None or whs is None:
            raise TimingError(f"clock pair {row_src} -> {row_dst} has no slack figures", log)
        a, b = names[row_src], names[row_dst]
        # The pair's constraint is the requirement of its worst setup path where the pair
        # report gave it, else the plan's requirement between clocks of these kinds.
        requirement = requirements.get((row_src, row_dst)) if req_known else None
        constraint = 1000.0 / (requirement or plan.requirement_ns(_kind(a), _kind(b)))
        timing[f"{a}->{b}{suffix}"] = TimingDomain(
            constraint_mhz=constraint,
            achieved_mhz=_achieved(constraint, requirement, wns),
            met=wns >= 0 and whs >= 0,
            kind="cross",
            setup_slack_ns=wns,
            hold_slack_ns=whs,
            requirement_ns=requirement,
        )

    for row in table_rows(text, "Inter Clock Table"):
        pair(row[0], row[1], ns_value(row[2]), ns_value(row[6]), "", True)
    for row in table_rows(text, "Other Path Groups Table"):
        # Recovery and removal of asynchronous resets, e.g. a project's `negedge rst_n`.
        pair(row[1], row[2], ns_value(row[3]), ns_value(row[7]), f" ({row[0].strip('*')})", False)

    absent = sorted(k for k in names.values() if k not in timing)
    if absent:
        raise TimingError(
            f"unconstrained: clock domain(s) {', '.join(absent)} absent from the timing report",
            log,
        )
    if "Timing constraints are not met." in text and all(d.met for d in timing.values()):
        raise TimingError("Vivado reports timing not met in a path group not parsed", log)
    return timing


# Every compare build has paths both ways between sys and pclk: rst_n into the project,
# uo_out into the sampler. A report without them has left them untimed.
REQUIRED_PAIRS = ("sys->pclk", "pclk->sys")


def check(timing: dict[str, TimingDomain], plan: ClockPlan, log: Path) -> None:
    """Raise `TimingError` unless sys, pclk and both pairs between them are reported,
    and every clock and clock pair met timing."""
    missing = sorted((set(plan.clocks) | set(REQUIRED_PAIRS)) - set(timing))
    if missing:
        raise TimingError(
            f"unconstrained: clock domain(s) {', '.join(missing)} absent from the timing report",
            log,
        )
    failed = [f"{k} ({_describe(d)})" for k, d in sorted(timing.items()) if not d.met]
    if failed:
        raise TimingError(f"timing not met in {', '.join(failed)}", log)


def _describe(d: TimingDomain) -> str:
    if d.kind == "cross":
        # A clock pair is a requirement, not a frequency.
        requirement = d.requirement_ns
        if requirement is None:
            requirement = 1000.0 / d.constraint_mhz
        parts = [f"requirement {requirement:.3f} ns"]
    else:
        achieved = "n/a" if d.achieved_mhz is None else f"{d.achieved_mhz:.2f} MHz"
        parts = [f"{achieved} achieved against {d.constraint_mhz:.2f} MHz"]
        period = 1000.0 / d.constraint_mhz
        if d.requirement_ns is not None and abs(d.requirement_ns - period) > 0.001:
            parts.append(
                f"worst setup path has a {d.requirement_ns:.3f} ns requirement "
                f"(frequency scaled to the {period:.3f} ns period)"
            )
    if d.root_kind is not None:
        parts.append(f"{d.root_kind} clock" + ("" if d.signed_off else ", NOT SIGNED OFF"))
    if d.half_cycle:
        parts.append("constraint doubled: clock used on both edges")
    if not d.intra_paths:
        parts.append("no paths within the clock")
    if d.setup_slack_ns is not None:
        parts.append(f"setup slack {d.setup_slack_ns:.3f} ns")
    if d.hold_slack_ns is not None:
        parts.append(f"hold slack {d.hold_slack_ns:.3f} ns")
    return ", ".join(parts)


def describe(d: TimingDomain) -> str:
    """One line for a timing domain, as the build log and the CLI print it."""
    return _describe(d)


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
# other net on a clock or latch-enable pin is made by the project's logic and is constrained
# at the pclk frequency. nextpnr-xilinx times cells on a clock's falling edge as on its
# rising edge, so a clock with users on both edges is constrained at twice its frequency.
INPUT_PORT = {input_port!r}
SYS_MHZ = {sys_mhz!r}
PCLK_BUFG = {pclk_bufg!r}
PCLK_MHZ = {pclk_mhz!r}
# Clock (and latch-enable) pins, each with the parameter that inverts it.
CLOCK_PORTS = {{
    "C": "IS_C_INVERTED",
    "CLK": "IS_CLK_INVERTED",
    "WCLK": "IS_WCLK_INVERTED",
    "CLKARDCLK": "IS_CLKARDCLK_INVERTED",
    "CLKBWRCLK": "IS_CLKBWRCLK_INVERTED",
    "G": "IS_G_INVERTED",
}}
LATCHES = ("LDCE", "LDPE")


def param_set(cell, key):
    for k, v in cell.params:
        if k == key:
            return "1" in str(v)
    return False


def falling(cell, port):
    # FDRE_1 and friends are the negative-edge flip-flops; a latch is transparent while
    # its enable is high and closes on the falling edge.
    inverted = cell.type.endswith("_1") or param_set(cell, CLOCK_PORTS[port])
    if cell.type in LATCHES:
        return not inverted
    return inverted


users = {{}}  # net -> [cells, rising, falling, latches]
for name, cell in ctx.cells:
    for port, info in cell.ports:
        if port in CLOCK_PORTS and info.net is not None:
            u = users.setdefault(info.net.name, [0, 0, 0, 0])
            u[0] += 1
            u[2 if falling(cell, port) else 1] += 1
            if cell.type in LATCHES:
                u[3] += 1
clock_of = {{}}  # clocked cell name -> its clock net
clock_port_of = {{}}
for name, cell in ctx.cells:
    for port, info in cell.ports:
        if port in CLOCK_PORTS and info.net is not None:
            clock_of[name] = info.net.name
            clock_port_of[name] = port


def own_output(cell, port, info):
    drv = info.net.driver
    return drv is not None and drv.cell is not None and drv.cell.name == cell.name and drv.port == port


def has_intra_paths(net_name):
    # Can data reach a cell on this clock from another cell on it? Walk back from every
    # such cell's other pins through cells that are not clocked (gates, carries, muxes).
    members = {{n for n, c in clock_of.items() if c == net_name}}
    for start in members:
        cell = ctx.cells[start]
        todo = []
        for port, info in cell.ports:
            if port in CLOCK_PORTS or info.net is None or info.net.driver is None:
                continue
            if own_output(cell, port, info):
                continue
            drv = info.net.driver.cell
            if drv is None:
                continue
            if drv.name == start:
                return True  # its own output on one of its inputs (Q to D or CE)
            todo.append(drv)
        seen = set()
        while todo and len(seen) < 20000:
            c = todo.pop()
            if c.name in seen:
                continue
            seen.add(c.name)
            if c.name in clock_of:
                if c.name in members:
                    return True
                continue
            for port, info in c.ports:
                if info.net is None or info.net.driver is None:
                    continue
                drv = info.net.driver.cell
                if drv is not None and drv.name != c.name:
                    todo.append(drv)
        if len(seen) >= 20000:
            return True  # too large to tell: assume there are
    return False


found = {{}}
known = set()
project_nets = []


def constrain(domain, net, mhz, why):
    cells, rise, fall, latches = users.get(net.name, [0, 0, 0, 0])
    intra = "yes" if has_intra_paths(net.name) else "no"
    half = rise > 0 and fall > 0
    effective = mhz * 2 if half else mhz
    ctx.addClock(net.name, effective)
    known.add(net.name)
    found.setdefault(domain, []).append(net.name)
    if domain == "project":
        project_nets.append(net)
    print(
        f"tt-harness clock: domain {{domain}} net '{{net.name}}' {{effective}} MHz "
        f"(cells {{cells}} rising {{rise}} falling {{fall}} latches {{latches}} intra {{intra}}"
        f"{{' half-cycle' if half else ''}}; {{why}})"
    )


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

# What drives each project clock, for tt_harness.project_clocks: walk back from the net
# through cells that are not clocked to the clocked cells (and inputs) its value comes from.
CONSTANTS = ("GND", "VCC")
ASYNC = {{"FDCE": "CLR", "FDPE": "PRE", "FDCE_1": "CLR", "FDPE_1": "PRE", "LDCE": "CLR", "LDPE": "PRE"}}
SYS_NET = found["sys"][0]
PCLK_NET = found["pclk"][0]


def clock_ref(net_name):
    if net_name == PCLK_NET:
        return "pclk"
    if net_name == SYS_NET:
        return "sys"
    return "root:" + net_name


def has_async(cell):
    wanted = ASYNC.get(cell.type)
    for port, info in cell.ports:
        if port != wanted:
            continue
        drv = info.net.driver.cell if info.net is not None and info.net.driver is not None else None
        return drv is None or drv.type not in CONSTANTS
    return False


def contributors(net):
    out = []
    seen_nets = set()
    seen_cells = set()
    todo = [net]
    while todo:
        n = todo.pop()
        if n.name in seen_nets:
            continue
        seen_nets.add(n.name)
        if n.name != net.name and n.name in (PCLK_NET, SYS_NET):
            # A clock used as data: both of its edges pass through.
            out.append(f"clock {{clock_ref(n.name)}} rising through-logic no-async")
            continue
        drv = n.driver.cell if n.driver is not None else None
        if drv is None or drv.type == "IBUF" or drv.type.startswith("$nextpnr_"):
            out.append("port none rising direct no-async")
            continue
        if drv.type in CONSTANTS or drv.name in seen_cells:
            continue
        seen_cells.add(drv.name)
        if len(seen_cells) > 20000:
            out.append("unknown none rising direct no-async")  # too large to tell
            break
        if drv.name in clock_of:
            edge = "falling" if falling(drv, clock_port_of[drv.name]) else "rising"
            asynchronous = "async" if has_async(drv) else "no-async"
            out.append(f"{{drv.type}} {{clock_ref(clock_of[drv.name])}} {{edge}} direct {{asynchronous}}")
            continue
        for port, info in drv.ports:
            if info.net is not None and not own_output(drv, port, info):
                todo.append(info.net)
    return out


for net in project_nets:
    print(f"tt-harness clock root: {{net.name}}")
    for line in contributors(net):
        print(f"tt-harness clock contributor: {{net.name}} {{line}}")
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
    vivado_nettype_rewrite: bool = True
    # Yosys (the pre-screen) parsed the unmodified sources, `default_nettype none` in force.
    yosys_accepted_unmodified: bool = False
    # Filled in by the flow: project file given to the tool -> its SHA-256, and the files
    # given modified.
    inputs: dict[str, str] = field(default_factory=dict)
    rewrites: list[tuple[str, int]] = field(default_factory=list)

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
                # The generated top first, so no project directive is in force for it.
                f"read_verilog {job.top_v}",
                *(f"read_verilog {sv}{incs} {f}" for f in files),
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
# reaches yet is followed to the root of its net, the pin that drives it. (Vivado carries
# pclk through combinational gates itself, so a root is made by sequential cells.)
set tt_roots {{}}
foreach pin [get_pins -quiet -filter {{IS_CLOCK}} \\
        -of_objects [get_cells -quiet -hier -filter {{IS_SEQUENTIAL}}]] {{
    if {{[llength [get_clocks -quiet -of_objects $pin]]}} {{ continue }}
    set drv [get_pins -quiet -leaf -filter {{DIRECTION == OUT}} -of_objects [get_nets -of_objects $pin]]
    if {{[llength $drv] != 1}} {{ error "tt-harness: clock pin $pin has driver(s) '$drv'" }}
    if {{[lsearch -exact $tt_roots $drv] < 0}} {{ lappend tt_roots $drv }}
}}

# What drives each root, for tt_harness.project_clocks: every sequential cell in its fan-in
# cone, with the clock on its clock pin (pclk, sys, another root or none), the edge it is
# clocked on, whether that clock arrives straight from its source, and whether the cell has
# an asynchronous set or clear that is not tied off; and every top-level input in the cone.
set tt_facts [open {{{roots_file}}} w]
proc tt_fact {{line}} {{
    global tt_facts
    puts $tt_facts $line
    puts $line
}}
set tt_pclk_pin [get_pins {bufg}/O]
set tt_n 0
foreach root $tt_roots {{
    set tt_root($tt_n) $root
    incr tt_n
    tt_fact "tt-harness clock root: $root"
    foreach cell [filter [all_fanin -quiet -flat -only_cells $root] {{IS_SEQUENTIAL}}] {{
        set ref [get_property REF_NAME $cell]
        set async no-async
        foreach p [get_pins -quiet -of_objects $cell \\
                -filter {{REF_PIN_NAME == CLR || REF_PIN_NAME == PRE}}] {{
            set type [get_property -quiet TYPE [get_nets -quiet -of_objects $p]]
            if {{$type ne "GROUND" && $type ne "POWER"}} {{ set async async }}
        }}
        set edge rising
        foreach prop {{IS_C_INVERTED IS_G_INVERTED IS_CLK_INVERTED}} {{
            # The value reads 1'b1 or 1'b0.
            if {{[string match "*1" [get_property -quiet $prop $cell]]}} {{ set edge falling }}
        }}
        set cpins [get_pins -quiet -of_objects $cell -filter {{IS_CLOCK}}]
        if {{[llength $cpins] == 0}} {{
            tt_fact "tt-harness clock contributor: $root $ref none $edge direct $async"
        }}
        foreach cpin $cpins {{
            set drv [get_pins -quiet -leaf -filter {{DIRECTION == OUT}} \\
                -of_objects [get_nets -quiet -of_objects $cpin]]
            set clocks [get_clocks -quiet -of_objects $cpin]
            set path direct
            if {{[lsearch -exact $clocks pclk] >= 0}} {{
                set clock pclk
                if {{[llength $drv] != 1 || $drv ne $tt_pclk_pin}} {{ set path through-logic }}
            }} elseif {{[lsearch -exact $clocks sys] >= 0}} {{
                set clock sys
            }} elseif {{[llength $drv] == 1}} {{
                set clock root:$drv
            }} else {{
                set clock none
            }}
            tt_fact "tt-harness clock contributor: $root $ref $clock $edge $path $async"
        }}
    }}
    foreach start [all_fanin -quiet -flat -startpoints_only $root] {{
        if {{[get_property CLASS $start] eq "port"}} {{
            tt_fact "tt-harness clock contributor: $root port none rising direct no-async"
        }}
    }}
}}
close $tt_facts

# tt_harness.project_clocks classifies the roots and writes the clock on each one, and the
# requirements between project clocks and pclk that their waveforms do not give.
exec /usr/bin/env -i {{{python}}} -I {{{script}}} {{{roots_file}}} {{{constraints}}} {bufg}/O {pclk_period:.3f}
source {{{constraints}}}

# One project clock is S = {settle} system cycles from any sys-side change to the next pclk
# edge, and S cycles from a pclk edge to the sample. Setup is checked over S sys cycles in
# both directions. Hold: sys->pclk data changes one sys cycle after a falling pclk edge at
# the earliest (requirement -1 sys period); pclk->sys keeps the default same-edge check
# (requirement 0), which is stricter than the recorder needs. Clocks the project makes are
# edges of pclk delayed further, so they get the same relationship to sys.
foreach c [get_clocks {{pclk project_clk*}}] {{
    set_multicycle_path -setup -start {settle} -from [get_clocks sys] -to $c
    set_multicycle_path -hold -start {settle} -from [get_clocks sys] -to $c
    set_multicycle_path -setup -end {settle} -from $c -to [get_clocks sys]
    set_multicycle_path -hold -end {settle_minus_1} -from $c -to [get_clocks sys]
}}
"""


def vivado_clocks_tcl(plan: ClockPlan, board, out: Path) -> str:
    """The Tcl that constrains a compare build's clocks, run after synthesis.

    It defines sys and pclk, writes what drives every project-clock root to
    `out/clock-roots.txt`, runs `tt_harness/project_clocks.py` (as a program, with this
    interpreter and an empty environment, so Vivado's own cannot affect it) to write
    `out/project-clocks.tcl` from that, and sources the result.
    """
    return _VIVADO_CLOCKS.format(
        sys_period=plan.sys_period_ns,
        pclk_period=plan.pclk_period_ns,
        input_port=board.SYS_CLK_PORT,
        bufg=board.BUFG_NAME,
        divide=2 * (plan.settle + 1),
        settle=plan.settle,
        settle_minus_1=plan.settle - 1,
        roots_file=out / "clock-roots.txt",
        constraints=out / "project-clocks.tcl",
        python=sys.executable,
        script=Path(pc.__file__),
    )


_VIVADO_NETTYPE_ERROR = "[Synth 8-6735]"
_VIVADO_MESSAGE_FILE = re.compile(r"\[(?P<path>[^\[\]]+?):\d+\]\s*$")
_VIVADO_UNDECLARED = re.compile(r"\[Synth 8-11241\] undeclared symbol '(?P<name>[^']+)'")


def nettype_retry(text: str, project_files: list[Path]) -> tuple[str, list[str]]:
    """Decide what a failed Vivado run's output calls for.

    Returns ("retry", files) when at least one Synth 8-6735 message names a project source
    (`files` are those named), ("harness", files) when 8-6735 names only other files (the
    harness's own, a harness bug), and ("none", []) when there is no 8-6735 at all.
    """
    project = {str(Path(f)) for f in project_files}
    named_project, named_other = [], []
    for line in text.splitlines():
        if _VIVADO_NETTYPE_ERROR not in line:
            continue
        m = _VIVADO_MESSAGE_FILE.search(line)
        path = m.group("path") if m else "?"
        (named_project if path in project else named_other).append(path)
    if named_project:
        return "retry", sorted(set(named_project))
    if named_other:
        return "harness", sorted(set(named_other))
    return "none", []


_NETTYPE_NONE = re.compile(r"`default_nettype([ \t]+)none\b")


def rewrite_nettype_none(text: str) -> tuple[str, int]:
    """Replace each `` `default_nettype none `` directive by `` `default_nettype wire ``.

    Only directives outside comments and string literals are changed, and nothing else in
    the text (the whitespace inside the directive is kept). Returns the new text and the
    number of directives changed.
    """
    out: list[str] = []
    count = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
        elif c == "`" and (m := _NETTYPE_NONE.match(text, i)):
            out.append(f"`default_nettype{m.group(1)}wire")
            count += 1
            i = m.end()
            continue
        else:
            j = i + 1
        out.append(text[i:j])
        i = j
    return "".join(out), count


_DECLARATION_WORDS = (
    "input|output|inout|wire|reg|logic|tri|tri0|tri1|wand|wor|supply0|supply1|integer|"
    "genvar|bit|byte|int|shortint|longint|parameter|localparam|var"
)
_DECLARATION = re.compile(rf"\b(?:{_DECLARATION_WORDS})\b")
_NOT_NAMES = frozenset(_DECLARATION_WORDS.split("|")) | {"signed", "unsigned"}
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_BRACKETS = re.compile(r"\[[^\[\]]*\]")


def _declared_names(text: str) -> set[str]:
    """The identifiers that declarations in Verilog `text` declare (a textual reading).

    From each declaration keyword to the end of its statement (or of an ANSI port list),
    each comma-separated item declares the last identifier before any `=`: names used in
    ranges or initialisers are not declared by it.
    """
    text = _COMMENT.sub(" ", text)
    names: set[str] = set()
    for m in _DECLARATION.finditer(text):
        items, item, depth, i = [], [], 0, m.end()
        while i < len(text):
            c = text[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    break  # the end of an ANSI port list
            elif c == ";" and depth == 0:
                break
            if c == "," and depth == 0:
                items.append("".join(item))
                item = []
            else:
                item.append(c)
            i += 1
        items.append("".join(item))
        for piece in items:
            while _BRACKETS.search(piece):
                piece = _BRACKETS.sub(" ", piece)
            head = piece.split("=", 1)[0]
            found = [w for w in _IDENTIFIER.findall(head) if w not in _NOT_NAMES]
            if found:
                names.add(found[-1])
    return names


def undeclared_symbols(text: str, sources: list[Path]) -> list[str]:
    """Names in Synth 8-11241 messages that no project source declares (a textual check)."""
    names = sorted({m.group("name") for m in _VIVADO_UNDECLARED.finditer(text)})
    if not names:
        return []
    declared: set[str] = set()
    for path in sources:
        declared |= _declared_names(path.read_bytes().decode("utf-8", errors="replace"))
    return [name for name in names if name not in declared]


def _vivado_rewritten_sources(job: _Job) -> tuple[list[Path], list[Path]]:
    """Copies of src/ with each `default_nettype none` directive read as `wire` (recorded)."""
    src = job.sources.root / "src"
    files, include_dirs = verilate.project_files(job.sources)
    outside = [str(p) for p in [*files, *include_dirs] if not p.is_relative_to(src)]
    if outside:
        raise BuildError(
            f"{job.sources.project.id}: cannot make `default_nettype wire` copies: "
            f"{', '.join(outside)} not under {src}",
            job.log,
        )
    dest = job.out_dir / "src-nettype-wire"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)
    for path in sorted(dest.rglob("*")):
        if path.is_file() and path.suffix in (".v", ".sv", ".vh", ".svh"):
            # Bytes in and out, so line endings and encoding are kept exactly.
            text = path.read_bytes().decode("utf-8", errors="surrogateescape")
            fixed, count = rewrite_nettype_none(text)
            if count:
                path.write_bytes(fixed.encode("utf-8", errors="surrogateescape"))
                job.rewrites.append((str(src / path.relative_to(dest)), count))
    return (
        [dest / f.relative_to(src) for f in files],
        [dest / d.relative_to(src) for d in include_dirs],
    )


def _vivado_script(job: _Job, files: list[Path], dirs: list[Path]) -> Path:
    out = job.out_dir
    clocks_tcl = out / "clocks.tcl"
    clocks_tcl.write_text(vivado_clocks_tcl(job.plan, job.board, out))

    def braced(paths: list[Path]) -> str:
        return " ".join("{" + str(p) + "}" for p in paths)

    # The worst paths of every ordered clock pair (a clock with itself included), setup and
    # hold, with their requirements.
    pair_reports = [
        "foreach a [get_clocks] { foreach b [get_clocks] {",
        "    foreach {kind tag} {max setup min hold} {",
        "        report_timing -from $a -to $b -delay_type $kind -max_paths 3 \\",
        f'            -file "{out}/$a-to-$b-$tag.rpt"',
        "}}}",
    ]
    script = out / "build.tcl"
    sv = "-sv " if job.sv else ""
    script.write_text(
        "\n".join(
            [
                f"set_param general.maxThreads {job.jobs}",
                # The generated top first, so no project directive is in force for it.
                f"read_verilog {{{job.top_v}}}",
                f"read_verilog {sv}{braced(files)}",
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


_PAIR_REPORT = re.compile(r"^(?P<a>.+)-to-(?P<b>.+)-setup\.rpt$")
_REQUIREMENT_LINE = re.compile(r"^\s*Requirement:\s+(-?[\d.]+)ns", re.MULTILINE)
_NO_PATHS_LINE = re.compile(r"^No timing paths found\.\s*$", re.MULTILINE)


def _pair_report_lines(out: Path) -> list[str]:
    """What each pair's setup report says, as lines for `parse_vivado`.

    `tt-harness requirement:` gives the requirement of the first (worst) path;
    `tt-harness no paths:` a report that says there are none.
    """
    lines = []
    for report in sorted(out.glob("*-to-*-setup.rpt")):
        m = _PAIR_REPORT.match(report.name)
        if m is None:
            continue
        text = report.read_text(errors="replace")
        req = _REQUIREMENT_LINE.search(text)
        if req:
            lines.append(f"tt-harness requirement: {m['a']} -> {m['b']} setup {req.group(1)} ns")
        elif _NO_PATHS_LINE.search(text):
            lines.append(f"tt-harness no paths: {m['a']} -> {m['b']}")
    return lines


def _vivado_implement(job: _Job) -> str:
    """Synthesise, implement and write the bitstream; return the timing summary.

    The returned text starts with the run's `tt-harness clock root:`, `tt-harness clock
    contributor:` and `tt-harness project clock:` lines and the lines of
    `_pair_report_lines`.
    """
    settings = tools.vivado_settings()
    if settings is None:
        raise FileNotFoundError(f"Vivado not found at {tools.VIVADO_SETTINGS}")
    out = job.out_dir
    vivado_log = out / "vivado.log"
    files, dirs = verilate.project_files(job.sources)
    project_id = job.sources.project.id
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
        if rc == 0 or attempt != "unmodified":
            break
        decision, named = nettype_retry(text, files)
        if decision == "none":
            break
        if decision == "harness":
            raise BuildError(
                f"{project_id}: Vivado reports Synth 8-6735 only in harness files "
                f"({', '.join(named)}): a harness bug, not rewritten",
                job.log,
            )
        if not job.vivado_nettype_rewrite:
            raise BuildError(
                f"{project_id}: Vivado rejects the unmodified sources with Synth 8-6735 in "
                f"{', '.join(named)}; Vivado cannot relax this (the message cannot be "
                "downgraded), and the `default_nettype wire` retry is forbidden "
                "(vivado_nettype_rewrite=False, --no-vivado-nettype-rewrite)",
                job.log,
            )
        if not job.yosys_accepted_unmodified:
            # Without Yosys's acceptance under `none`, the rewrite could hide a real
            # undeclared net. (build_compare fails before this when the pre-screen fails.)
            raise BuildError(
                f"{project_id}: Vivado rejects the unmodified sources (Synth 8-6735 in "
                f"{', '.join(named)}) and Yosys has not accepted them, so they are not "
                "rewritten",
                job.log,
            )
        with job.log.open("a") as f:
            f.write(
                "\ntt-harness: retrying Vivado with `default_nettype wire` copies "
                f"(Synth 8-6735 in {', '.join(named)})\n"
            )
        files, dirs = _vivado_rewritten_sources(job)
    job.inputs.update({str(f): _sha256(f) for f in files})
    if job.rewrites:
        missing = undeclared_symbols(text, files)
        if missing:
            raise BuildError(
                f"{project_id}: on the `default_nettype wire` retry Vivado assumed a wire for "
                f"{', '.join(missing)}, which no project source declares: a genuine implicit "
                "net",
                job.log,
            )
    if rc != 0:
        first = re.search(r"^ERROR\b.*$", text, re.MULTILINE)
        detail = f": {first.group(0)}" if first else ""
        raise BuildError(
            f"{project_id}: Vivado (synthesis, implementation, bitstream) failed "
            f"(exit {rc}){detail}",
            job.log,
        )
    timing_rpt = out / "timing.rpt"
    if not timing_rpt.is_file():
        raise BuildError(f"{project_id}: Vivado wrote no timing report", job.log)
    prefixes = (
        pc.ROOT_PREFIX,
        pc.CONTRIBUTOR_PREFIX,
        _VIVADO_PROJECT_CLOCK_PREFIX,
    )
    mapping = [ln for ln in text.splitlines() if ln.startswith(prefixes)]
    return "\n".join([*mapping, *_pair_report_lines(out)]) + "\n" + timing_rpt.read_text()


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

# Outputs of an earlier build in the same directory that must not be mistaken for this one's.
_STALE = (
    "timing.rpt",
    "clocks.rpt",
    "utilization.rpt",
    "nextpnr.log",
    "vivado.log",
    "clock-roots.txt",
    "project-clocks.tcl",
)


def _remove_stale(out_dir: Path, names: tuple[str, ...]) -> None:
    for name in (*_STALE, *names):
        (out_dir / name).unlink(missing_ok=True)
    for pattern in ("*-to-*-*.rpt", "vivado_*.backup.log"):
        for stale in out_dir.glob(pattern):
            stale.unlink()
    shutil.rmtree(out_dir / "src-nettype-wire", ignore_errors=True)


def _warn(log: Path, message: str) -> None:
    print(f"tt-harness build-compare: warning: {message}", file=sys.stderr)
    with log.open("a") as f:
        f.write(f"tt-harness: warning: {message}\n")


def build_compare(
    project: Project,
    sources: Sources,
    board: str,
    toolchain: str,
    out_dir: Path,
    clocks: int,
    baud: int = 1_000_000,
    jobs: int = 16,
    vivado_nettype_rewrite: bool = True,
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
    unchecked = out_dir / f"{board_mod.MODULE_NAME}.unchecked.bit"
    _remove_stale(out_dir, (LOG_NAME, bitstream.name, unchecked.name))
    log.write_text(f"tt-harness build-compare {project.id} {board} {toolchain} clocks={clocks}\n")

    # The language rule of the pre-screen (and so of the reference): Verilog-2005 unless a
    # source is .sv or the plain parse fails.
    try:
        screen = prescreen.check(sources, out_dir / "prescreen")
    except prescreen.PrescreenError as e:
        with log.open("a") as f:
            f.write(f"tt-harness: pre-screen failed, nothing built: {e}\n")
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
        # prescreen.check raised otherwise, so Yosys read them as fetched, `none` in force.
        yosys_accepted_unmodified=True,
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

    tool = TOOL_NAMES[toolchain]
    limitations = []
    if toolchain == "openxc7":
        limitations += [LIMIT_NO_HOLD, LIMIT_CROSS_BUDGET]
        if clocks_made:
            limitations.append(LIMIT_LOCAL_CLOCKS)
        for key, d in sorted(timing.items()):
            if d.half_cycle:
                limitations.append(
                    f"openXC7: {key} is used on both edges and nextpnr times falling-edge "
                    "cells as rising, so it was constrained at twice its frequency"
                )
    for key, d in sorted(timing.items()):
        if d.kind != "project":
            continue
        clock = key.removeprefix(PROJECT)
        if not d.signed_off:
            limitations.append(
                LIMIT_NOT_SIGNED_OFF.format(
                    tool=tool, clock=clock, reason=d.root_reason, assumption=d.root_assumption
                )
            )
        elif d.root_kind == pc.REGISTER and (d.root_gate_of or 0) > 1:
            limitations.append(LIMIT_GATE.format(tool=tool, clock=clock, n=d.root_gate_of))
    for key, d in sorted(timing.items()):
        if d.kind != "cross" and not d.intra_paths:
            limitations.append(LIMIT_NO_PATHS.format(tool=tool, key=key))
    if screen.latches:
        limitations.append(
            f"{screen.latches} latch(es) in the design, timed with their enable as a clock"
        )
    if bool(clocks_made) != bool(screen.derived_clocks):
        limitations.append(
            f"pre-screen and {tool} disagree on clocks made in logic: pre-screen "
            f"{screen.derived_clocks} derived clock(s), {tool} {list(clocks_made)}"
        )
        _warn(log, limitations[-1])
    with log.open("a") as f:
        for name, d in sorted(timing.items()):
            f.write(f"tt-harness timing: {name}: {_describe(d)}, met\n")
        for item in limitations:
            f.write(f"tt-harness limitation: {item}\n")
    if job.rewrites:
        files = ", ".join(f"{Path(p).name} ({n})" for p, n in job.rewrites)
        _warn(
            log,
            f"{toolchain} read copies with `default_nettype none` as `wire` "
            f"(Synth 8-6735 on the unmodified sources): {files}",
        )
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
        limitations=tuple(limitations),
    )
