# SPDX-License-Identifier: Apache-2.0
"""Gate on the implemented netlist: what drives each net into the project.

A project may use any input asynchronously (`rst_n` as an asynchronous reset), so the net on
every project input other than the clock must be a constant or be driven directly by a
flip-flop: the output of a LUT, a carry chain or an inverter can glitch. The source is checked
by `tt_harness.compare.inputs`; this checks what the tools made of it, because synthesis may
insert or duplicate logic (a LUT used as a buffer, an inverter folded into a LUT, a register
whose polarity was changed and an inverter added behind it).

What is verified in the netlist. In the builds so far only `rst_n` is found there and
classified (its driver is the Q pin of a flip-flop). The three constant inputs (`ena`,
`ui_in`, `uio_in`) are not found in the netlist: synthesis propagated the constants and
removed the nets. They are accepted (`FOLDED`) because the generated top assigns each net
only a literal, which is read from that Verilog source and not from the netlist. A tool fault
that left a constant input driven by logic would show up as a missing net and pass. The gate
also does not check which clock domain the flip-flop driving `rst_n` is in, unless the caller
asks (next paragraph).

Clock domain (added for the demo build, where the project has its own clock). For each
flip-flop that drives a project input the report also names the net on the flip-flop's clock
pin, and `clock_offenders` fails any whose clock is not the expected (project clock) net,
which the caller names. A register of the `sys` domain driving a project input is exactly
the crossing that causes metastability inside the design. The compare build has one clock and
does not ask for this check; wiring it into the demo builds is a later task.
`yosys_driver_clocks` follows the clock net back through clock buffers (`BUFG`, `IBUF`,
`BUFGCE`, ...) and reports every net name on the way, since a buffer's output net is often
renamed by the tools, and the cells walked (`ClockTrace`). A `BUFGCE` is a clock GATE, so
walking through it reports a gated clock under the name of the clock it was made from: the
check passes on the expected name, and it is the CALLER that decides which traces are
acceptable (`allowed_trace`; the later build task will require exactly the project clock's
own buffer chain). The walk stops at any other cell (a LUT gating a clock), which fails. The
check fails closed: see `clock_offenders`. The Vivado report
gives the net on the flip-flop's `C` pin as Vivado names it (the clock buffer's output net),
so the expected name is that one. The Vivado Tcl for this field has not been run
(no Vivado in the session that wrote it): it is checked against a hand-written sample report.

Finding the nets. The project is flattened into the top by Yosys and its hierarchy is rebuilt
by Vivado, so "the project's `rst_n` port" is not a reliable name in either netlist. The nets
are found from the harness side instead: the generated top connects every project port to
one named signal (`.rst_n(stimulus_rst_n)`), which `instance_connections` reads from the
Verilog, and the compare top marks the one that is not a constant (`rst_n`) `keep`, so both
tools preserve it as a net with that name and cannot fold it into other logic. A net that
cannot be found fails the gate, with one exception: both tools propagate a constant into
the project and drop its net. A net that the generated top itself assigns a literal
(`assign stimulus_ena = 1'd1;`, found by `source_constants`) may therefore be absent, and is
then recorded as `FOLDED` on the strength of that source literal alone. The constant nets are
not marked `keep`: that gains nothing (Vivado removes them anyway) and it stops Vivado
propagating the constants, which changes what is built (2048: 986 LUTs instead of 207).
`keep` on the register's output costs nothing (the same 207 LUTs and 215 flip-flops with
and without it) and puts no logic on a path.

- openXC7: `yosys_drivers` reads the netlist Yosys wrote, which is the netlist nextpnr-xilinx
  reads before packing.
- Vivado: `vivado_tcl` writes a report from the routed design; `parse_vivado` reads it. A
  missing report, a missing end line or a missing port fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLOCK_PORT = "clk"
INPUT_PORTS = ("ui_in", "uio_in", "ena", "rst_n")  # every project input but the clock
OUTPUT_PORTS = ("uo_out", "uio_out", "uio_oe")
FLIP_FLOPS = ("FDRE", "FDSE", "FDCE", "FDPE")
CONSTANT = "constant"
MISSING = "missing"
FOLDED = "constant (net removed)"  # a literal in the source; the tool folded it away
UNDRIVEN = "undriven"
REPORT_NAME = "project-inputs.rpt"
_LINE = "tt-harness project input: "
_END = "tt-harness project inputs: end"

_CONNECTION = re.compile(r"\.(\w+)\(\s*([^()\s]*)\s*\)")
_REPORT_LINE = re.compile(
    rf"^{re.escape(_LINE)}(?P<port>\w+) net (?P<net>\S+) driver (?P<driver>\S+)"
    r"(?: clock (?P<clock>\S+))?\s*$",
    re.MULTILINE,
)
CLOCK_BUFFERS = ("BUFG", "IBUF", "BUFGCE", "BUFH", "BUFHCE")  # one clock input, pin I
MAX_CLOCK_HOPS = 16  # a longer chain is not a plain buffer chain: it fails the check


def instance_connections(top_verilog: str, instance: str) -> dict[str, str]:
    """Port -> the signal connected to it, for the instance called `instance`.

    Raises ValueError unless there is exactly one such instance and every connection is a
    plain identifier (an expression on a port would be logic the gate cannot name).
    """
    blocks = re.findall(
        rf"^\w+\s+{re.escape(instance)}\s*\((.*?)^\);", top_verilog, re.MULTILINE | re.DOTALL
    )
    if len(blocks) != 1:
        raise ValueError(f"expected one instance {instance!r} in the top, found {len(blocks)}")
    out = {}
    for line in blocks[0].splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        m = _CONNECTION.fullmatch(line)
        if not m or not re.fullmatch(r"[A-Za-z_]\w*", m.group(2)):
            raise ValueError(f"instance {instance!r}: cannot read the connection {line!r}")
        out[m.group(1)] = m.group(2)
    return out


def input_nets(top_verilog: str, instance: str) -> dict[str, str]:
    """The net on each project input port other than the clock.

    Raises ValueError for a port that is not a known project port: a new input must be added
    to `INPUT_PORTS` (and then pass the gate), not slip through.
    """
    connections = instance_connections(top_verilog, instance)
    known = {CLOCK_PORT, *INPUT_PORTS, *OUTPUT_PORTS}
    unknown = sorted(set(connections) - known)
    absent = sorted(set(INPUT_PORTS) - set(connections))
    if unknown or absent:
        raise ValueError(
            f"instance {instance!r}: unknown port(s) {unknown}, unconnected input(s) {absent}"
        )
    return {port: connections[port] for port in INPUT_PORTS}


def source_constants(top_verilog: str, nets: dict[str, str]) -> frozenset[str]:
    """The ports whose net the generated top assigns a literal, and nothing else."""
    out = set()
    for port, name in nets.items():
        assigns = re.findall(
            rf"^assign\s+{re.escape(name)}\s*=\s*(.+?);\s*$", top_verilog, re.MULTILINE
        )
        written = re.search(rf"^\s*{re.escape(name)}\s*<=", top_verilog, re.MULTILINE)
        if len(assigns) == 1 and re.fullmatch(r"\d+'d\d+", assigns[0]) and not written:
            out.add(port)
    return frozenset(out)


def fold_constants(
    drivers: dict[str, tuple[str, ...]], constants: frozenset[str]
) -> dict[str, tuple[str, ...]]:
    """`drivers` with `MISSING` turned into `FOLDED` for the ports in `constants`."""
    return {
        port: tuple(FOLDED if k == MISSING and port in constants else k for k in kinds)
        for port, kinds in drivers.items()
    }


def yosys_drivers(netlist: dict, top: str, nets: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """What drives each bit of each net in a Yosys JSON netlist, per port.

    Each entry is `CONSTANT`, `MISSING` (no such net), `UNDRIVEN`, or the type of the driving
    cell (with `.<port>` appended when a flip-flop drives it from a pin other than Q).
    """
    module = netlist["modules"][top]
    driver = {}
    for cell in module["cells"].values():
        for port, direction in cell.get("port_directions", {}).items():
            if direction == "output":
                for bit in cell["connections"][port]:
                    driver[bit] = (cell["type"], port)
    out = {}
    for port, name in nets.items():
        net = module["netnames"].get(name)
        if net is None:
            out[port] = (MISSING,)
            continue
        kinds = []
        for bit in net["bits"]:
            if bit in ("0", "1"):
                kinds.append(CONSTANT)
            elif bit not in driver:
                kinds.append(UNDRIVEN)
            else:
                kind, pin = driver[bit]
                kinds.append(kind if pin == "Q" or kind not in FLIP_FLOPS else f"{kind}.{pin}")
        out[port] = tuple(kinds)
    return out


def vivado_tcl(nets: dict[str, str], report: str) -> str:
    """Tcl, for the routed design, that writes what drives each bit of each net to `report`."""
    lines = [
        "# What drives each net into the project (tt_harness.input_gate).",
        f"set tt_inputs [open {{{report}}} w]",
        "proc tt_input {port name} {",
        "    global tt_inputs",
        '    set nets [get_nets -quiet [list $name "$name\\[*\\]"]]',
        f'    if {{[llength $nets] == 0}} {{ puts $tt_inputs "{_LINE}$port net $name driver {MISSING}" }}',
        "    foreach net $nets {",
        "        set type [get_property -quiet TYPE $net]",
        "        set drv [get_pins -quiet -leaf -filter {DIRECTION == OUT} -of_objects $net]",
        f'        if {{$type eq "GROUND" || $type eq "POWER"}} {{ set kind {CONSTANT} }} \\',
        f"        elseif {{[llength $drv] != 1}} {{ set kind {UNDRIVEN} }} \\",
        "        else {",
        "            set kind [get_property REF_NAME [get_cells -of_objects $drv]]",
        '            if {[get_property REF_PIN_NAME $drv] ne "Q"} {',
        '                set kind "$kind.[get_property REF_PIN_NAME $drv]"',
        "            }",
        "        }",
        '        set clock ""',
        "        if {[llength $drv] == 1} {",
        "            set cell [get_cells -of_objects $drv]",
        "            if {[lsearch {FDRE FDSE FDCE FDPE} [get_property REF_NAME $cell]] >= 0} {",
        "                set cpin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]",
        "                set cnet [get_nets -quiet -of_objects $cpin]",
        '                if {[llength $cnet] == 1} { set clock " clock $cnet" }',
        "            }",
        "        }",
        f'        puts $tt_inputs "{_LINE}$port net $net driver $kind$clock"',
        "    }",
        "}",
    ]
    lines += [f"tt_input {port} {name}" for port, name in nets.items()]
    lines += [f'puts $tt_inputs "{_END}"', "close $tt_inputs"]
    return "\n".join(lines) + "\n"


def parse_vivado(text: str, nets: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Per port, what the report of `vivado_tcl` says drives each bit.

    Raises ValueError if the report is not complete (no end line) or names a port that was
    not asked about; a port without a line is `MISSING`.
    """
    if _END not in text.splitlines():
        raise ValueError("the project-inputs report is incomplete (no end line)")
    found: dict[str, list[str]] = {port: [] for port in nets}
    for m in _REPORT_LINE.finditer(text):
        if m.group("port") not in found:
            raise ValueError(f"the project-inputs report names an unknown port {m.group('port')}")
        found[m.group("port")].append(m.group("driver"))
    return {port: tuple(kinds) or (MISSING,) for port, kinds in found.items()}


def offenders(drivers: dict[str, tuple[str, ...]]) -> list[str]:
    """One line per input that is not a constant or driven directly by a flip-flop."""
    out = []
    for port, kinds in sorted(drivers.items()):
        bad = sorted({k for k in kinds if k not in (CONSTANT, FOLDED) and k not in FLIP_FLOPS})
        if bad:
            out.append(f"{port} is driven by {', '.join(bad)}")
    return out


def summary(drivers: dict[str, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
    """(port, what drives it) for a result: one word per port when all its bits agree."""
    return tuple((port, "/".join(sorted(set(kinds)))) for port, kinds in sorted(drivers.items()))


@dataclass(frozen=True)
class ClockTrace:
    """What clocks a flip-flop: the net names found on its clock pin and what was walked.

    `names`: every net name on the way back from the `C` pin (sorted). `cells`: the types of
    the clock buffers walked through, nearest the flip-flop first (a `BUFGCE` in it means the
    clock is gated by an enable). `stopped_at`: the type of the first cell that is not a
    clock buffer (a LUT gating the clock, say) or None when the walk ended at a net with no
    driver (a port). The Vivado report gives only the net on the `C` pin: `cells` is empty.
    """

    names: tuple[str, ...]
    cells: tuple[str, ...] = ()
    stopped_at: str | None = None


def _bit_names(module: dict) -> dict:
    names: dict = {}
    for name, net in module["netnames"].items():
        for bit in net["bits"]:
            names.setdefault(bit, []).append(name)
    return names


def yosys_driver_clocks(
    netlist: dict, top: str, nets: dict[str, str]
) -> dict[str, tuple[ClockTrace | None, ...]]:
    """Per port and bit: the `ClockTrace` of the flip-flop driving it, or None.

    The clock pin is `C`; the net is followed back through clock buffers (`CLOCK_BUFFERS`),
    collecting the names of every net on the way, so a renamed buffer output does not hide
    the net it came from. The walk stops at any other cell (`stopped_at`): a clock gated by
    logic is not the clock it was made from. A bit not driven by a flip-flop, or a flip-flop
    with no `C` connection, gives None, as does a net that is not in the netlist; `None` is
    never acceptable to `clock_offenders`.
    """
    module = netlist["modules"][top]
    driver = {}
    for cell in module["cells"].values():
        for port, direction in cell.get("port_directions", {}).items():
            if direction == "output":
                for bit in cell["connections"].get(port, []):
                    driver[bit] = (cell["type"], port, cell)
    names = _bit_names(module)
    out = {}
    for port, name in nets.items():
        net = module["netnames"].get(name)
        traces: list[ClockTrace | None] = []
        for bit in net["bits"] if net else []:
            found = driver.get(bit)
            clock_bits = found[2]["connections"].get("C", []) if found else []
            if found is None or found[0] not in FLIP_FLOPS or found[1] != "Q" or not clock_bits:
                traces.append(None)
                continue
            seen: list[str] = []
            cells: list[str] = []
            stopped: str | None = None
            current = clock_bits[0]
            for _ in range(MAX_CLOCK_HOPS + 1):  # up to MAX_CLOCK_HOPS buffers, then the source
                seen += names.get(current, [])
                buf = driver.get(current)
                if buf is None:
                    break
                if buf[0] not in CLOCK_BUFFERS:
                    stopped = buf[0]
                    break
                cells.append(buf[0])
                inputs = buf[2]["connections"].get("I", [])
                if not inputs:
                    break
                current = inputs[0]
            else:  # not a plain buffer chain: too long (or a loop)
                stopped = f"more than {MAX_CLOCK_HOPS} clock buffers"
            traces.append(ClockTrace(tuple(sorted(set(seen))), tuple(cells), stopped))
        out[port] = tuple(traces) if traces else (None,)
    return out


def parse_vivado_clocks(
    text: str, nets: dict[str, str]
) -> dict[str, tuple[ClockTrace | None, ...]]:
    """Per port and bit: the clock net a flip-flop driver reports, as a `ClockTrace`, or None.

    Raises ValueError as `parse_vivado` does for an incomplete report or an unknown port. A
    port with no line gives (None,), like `parse_vivado`'s `MISSING`.
    """
    parse_vivado(text, nets)  # the same validity rules
    found: dict[str, list] = {port: [] for port in nets}
    for m in _REPORT_LINE.finditer(text):
        found[m.group("port")].append(ClockTrace((m.group("clock"),)) if m.group("clock") else None)
    return {port: tuple(v) or (None,) for port, v in found.items()}


def clock_offenders(
    drivers: dict[str, tuple[str, ...]],
    clocks: dict[str, tuple[ClockTrace | None, ...]],
    expected: str,
    allowed_trace: tuple[str, ...] | None = None,
) -> list[str]:
    """One line per bit that cannot be shown to be a flip-flop on the `expected` clock net.

    Fails closed. Every bit of every port in `drivers` must be a constant (`CONSTANT`,
    `FOLDED`: no clock needed) or a flip-flop with a clock entry that names `expected` and
    whose walk did not stop at a non-buffer cell. A missing port in `clocks`, a different
    number of entries than bits, a `None` entry, a wrong net, a gated clock, or a driver
    that is neither constant nor flip-flop is an offender, named `port[bit]`.

    `allowed_trace`, if given, is the exact tuple of clock buffer types the clock may pass
    through (the later build task pins it to the project clock's own chain); otherwise any
    chain of buffers is accepted and it is the caller's business to look at `ClockTrace.cells`.
    """
    out = []
    for port in sorted(drivers):
        kinds = drivers[port]
        entries = clocks.get(port)
        mismatch = entries is not None and len(entries) != len(kinds)
        if mismatch and any(k in FLIP_FLOPS for k in kinds):
            out.append(f"{port}: {len(kinds)} bits but {len(entries)} clock entries")
        for bit, kind in enumerate(kinds):
            where = f"{port}[{bit}]"
            if kind in (CONSTANT, FOLDED):
                continue
            if kind not in FLIP_FLOPS:
                out.append(f"{where}: driven by {kind}, not a flip-flop")
            elif mismatch:
                out.append(
                    f"{where}: unchecked ({len(entries)} clock entries for {len(kinds)} bits)"
                )
            elif entries is None:
                out.append(f"{where}: no clock data")
            elif entries[bit] is None:
                out.append(f"{where}: no clock net")
            else:
                trace = entries[bit]
                if trace.stopped_at is not None:
                    out.append(f"{where}: clock not a plain buffer chain: {trace.stopped_at}")
                elif expected not in trace.names:
                    out.append(f"{where}: clocked by {'/'.join(trace.names)}, not {expected}")
                elif allowed_trace is not None and trace.cells != tuple(allowed_trace):
                    out.append(
                        f"{where}: clock passes through {'/'.join(trace.cells) or 'nothing'}, "
                        f"not {'/'.join(allowed_trace) or 'nothing'}"
                    )
    return out
