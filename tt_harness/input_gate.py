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
asks (below).

Asynchronous clear and preset. A flip-flop with an asynchronous pin (`FDCE` `CLR`, `FDPE`
`PRE`) changes its output when that pin does, so a glitch there is a glitch on the project
input (the `rst_n` failure again, one cell further back). For every such flip-flop that drives
a project input the net on that pin must itself be a constant or the Q of a flip-flop;
anything else (a LUT, an unconnected or undriven pin, a report without the field) makes the
driver an offender that names the cell: `FDCE <cell>: CLR is driven by LUT2`.

Clock domain (added for the demo build, where the project has its own clock). For each
flip-flop that drives a project input the report also names the net on the flip-flop's clock
pin, and `clock_offenders` fails any whose clock is not the expected (project clock) net,
which the caller names. A register of the `sys` domain driving a project input is exactly
the crossing that causes metastability inside the design. The compare build has one clock and
does not ask for this check; wiring it into the demo builds is a later task.
The expected net is the one ON the flip-flop's clock pin (for the project clock: the output
of its `BUFGCE`, `ProjectClock.clk`). A name further back does not count: the net in front of
a `BUFGCE` is a different clock (ungated, and shared with whatever else it feeds), so
`expected = clkout_raw` is refused. `yosys_driver_clocks` still follows the clock back
through clock buffers (`BUFG`, `IBUF`, `BUFGCE`, ...) and records the cells walked and the
names seen further back (`ClockTrace.cells`, `.upstream`), so the CALLER can require exactly
the project clock's own chain (`allowed_trace`). The walk ends

- at a net with no driver (a port of the top);
- at a clock source the caller named (`clock_sources`, e.g. `("MMCME2_ADV", "CLKOUT0")`: the
  project clock is `MMCME2_ADV.CLKOUT0 -> BUFGCE -> C`), recorded in `ClockTrace.source`,
  which `allowed_source` can then require;
- at any other cell (a LUT gating a clock, an MMCM output nobody named, an inverter), which
  fails (`stopped_at`).

An inverted clock pin (`IS_C_INVERTED`) on a flip-flop driving a project input is an offender:
it is a register of the other clock edge. The check fails closed: see `clock_offenders`. The
Vivado report gives the net on the flip-flop's `C` pin as Vivado names it, so the expected
name is that one; it has no walk (`cells` empty, `source` None). The Vivado Tcl for the
clock, inversion, cell and clear fields has not been run (no Vivado in the sessions that wrote
it): it is checked against a hand-written sample report.

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
- Vivado: `vivado_tcl` writes a report from the routed design; `parse_vivado` reads it. It
  fails closed: a missing report or end line, a line with the report's prefix that cannot be
  read, an unknown port, a port with no line, or a port without either its one `missing`
  line or exactly one line per bit (`PORT_WIDTHS`: 8 / 8 / 1 / 1), is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLOCK_PORT = "clk"
INPUT_PORTS = ("ui_in", "uio_in", "ena", "rst_n")  # every project input but the clock
OUTPUT_PORTS = ("uo_out", "uio_out", "uio_oe")
FLIP_FLOPS = ("FDRE", "FDSE", "FDCE", "FDPE")
ASYNC_PINS = {"FDCE": "CLR", "FDPE": "PRE"}  # flip-flop type -> its asynchronous pin
PORT_WIDTHS = {"ui_in": 8, "uio_in": 8, "ena": 1, "rst_n": 1}
CONSTANT = "constant"
MISSING = "missing"
FOLDED = "constant (net removed)"  # a literal in the source; the tool folded it away
UNDRIVEN = "undriven"
REPORT_NAME = "project-inputs.rpt"
_LINE = "tt-harness project input: "
_END = "tt-harness project inputs: end"

_CONNECTION = re.compile(r"\.(\w+)\(\s*([^()\s]*)\s*\)")
_MARK = "tt-harness project input"  # any report line with this in it must be readable
_REPORT_LINE = re.compile(
    rf"{re.escape(_LINE)}(?P<port>\w+) net (?P<net>\S+) driver (?P<driver>\S+)"
    r"(?: cell (?P<cell>\S+))?(?: clock (?P<clock>\S+))?(?: inverted (?P<inverted>[01]))?"
    r"(?: clear (?P<clear>\S+))?"
)
UNCONNECTED = "unconnected"
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
    for cell_name, cell in module["cells"].items():
        for port, direction in cell.get("port_directions", {}).items():
            if direction == "output":
                for bit in cell["connections"][port]:
                    driver[bit] = (cell["type"], port, cell_name, cell)
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
                kind, pin, cell_name, cell = driver[bit]
                if kind in FLIP_FLOPS and pin != "Q":
                    kind = f"{kind}.{pin}"
                elif kind in ASYNC_PINS:
                    problem = _async_pin_problem(cell, driver)
                    if problem:
                        kind = f"{kind} {cell_name}: {problem}"
                kinds.append(kind)
        out[port] = tuple(kinds)
    return out


def _async_pin_problem(cell: dict, driver: dict) -> str | None:
    """Why the asynchronous clear/preset of flip-flop `cell` is not glitch-free, or None when
    it is a constant or the Q of a flip-flop."""
    pin = ASYNC_PINS[cell["type"]]
    bits = cell["connections"].get(pin, [])
    if len(bits) != 1:
        return f"{pin} is {UNCONNECTED}"
    bit = bits[0]
    if bit in ("0", "1"):
        return None
    if bit not in driver:
        return f"{pin} is {UNDRIVEN}"
    kind, out_pin = driver[bit][:2]
    if kind in FLIP_FLOPS:
        return None if out_pin == "Q" else f"{pin} is driven by {kind}.{out_pin}"
    return f"{pin} is driven by {kind}"


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
        # For a flip-flop: its name, the net on C, whether C is inverted, and what drives
        # its asynchronous pin (CLR or PRE; `none` for a flip-flop without one).
        '        set extra ""',
        "        if {[llength $drv] == 1} {",
        "            set cell [get_cells -of_objects $drv]",
        "            set ref [get_property REF_NAME $cell]",
        "            if {[lsearch {FDRE FDSE FDCE FDPE} $ref] >= 0} {",
        '                append extra " cell $cell"',
        "                set cpin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]",
        "                set cnet [get_nets -quiet -of_objects $cpin]",
        '                if {[llength $cnet] == 1} { append extra " clock $cnet" }',
        "                set inv [get_property -quiet IS_C_INVERTED $cell]",
        """                if {$inv eq "1'b0" || $inv eq "0"} { append extra " inverted 0" }""",
        """                if {$inv eq "1'b1" || $inv eq "1"} { append extra " inverted 1" }""",
        "                set clear none",
        '                if {$ref eq "FDCE" || $ref eq "FDPE"} {',
        f"                    set clear {UNCONNECTED}",
        "                    set apin [get_pins -quiet -of_objects $cell \\",
        "                        -filter {REF_PIN_NAME == CLR || REF_PIN_NAME == PRE}]",
        "                    set anet [get_nets -quiet -of_objects $apin]",
        "                    if {[llength $apin] == 1 && [llength $anet] == 1} {",
        "                        set atype [get_property -quiet TYPE $anet]",
        "                        set adrv [get_pins -quiet -leaf -filter {DIRECTION == OUT} \\",
        "                            -of_objects $anet]",
        f'                        if {{$atype eq "GROUND" || $atype eq "POWER"}} {{ set clear {CONSTANT} }} \\',
        f"                        elseif {{[llength $adrv] != 1}} {{ set clear {UNDRIVEN} }} \\",
        "                        else {",
        "                            set clear [get_property REF_NAME [get_cells -of_objects $adrv]]",
        '                            if {[get_property REF_PIN_NAME $adrv] ne "Q"} {',
        '                                set clear "$clear.[get_property REF_PIN_NAME $adrv]"',
        "                            }",
        "                        }",
        "                    }",
        "                }",
        '                append extra " clear $clear"',
        "            }",
        "        }",
        f'        puts $tt_inputs "{_LINE}$port net $net driver $kind$extra"',
        "    }",
        "}",
    ]
    lines += [f"tt_input {port} {name}" for port, name in nets.items()]
    lines += [f'puts $tt_inputs "{_END}"', "close $tt_inputs"]
    return "\n".join(lines) + "\n"


def _report_entries(text: str, nets: dict[str, str]) -> dict[str, list[re.Match]]:
    """The lines of a `vivado_tcl` report, per port, in bit order; ValueError unless the
    report is complete and every line is understood (see `parse_vivado`)."""
    lines = text.splitlines()
    if _END not in lines:
        raise ValueError("the project-inputs report is incomplete (no end line)")
    found: dict[str, list[re.Match]] = {port: [] for port in nets}
    for line in lines:
        if _MARK not in line or line == _END:
            continue
        m = _REPORT_LINE.fullmatch(line)
        if m is None:
            raise ValueError(f"the project-inputs report has a line that cannot be read: {line!r}")
        if m.group("port") not in found:
            raise ValueError(f"the project-inputs report names an unknown port {m.group('port')}")
        found[m.group("port")].append(m)
    for port, entries in found.items():
        name, width = nets[port], PORT_WIDTHS.get(port)
        if width is None:
            raise ValueError(f"the project-inputs report: no known width for port {port}")
        got = [m.group("net") for m in entries]
        if got == [name] and (width == 1 or entries[0].group("driver") == MISSING):
            continue  # a single-bit net, or the one line of a net that is not in the design
        wanted = [f"{name}[{bit}]" for bit in range(width)] if width > 1 else [name]
        if sorted(got) != sorted(wanted):
            raise ValueError(
                f"the project-inputs report has {len(got)} line(s) for {port} "
                f"({', '.join(got) or 'none'}); expected one per bit: {', '.join(wanted)}"
            )
        entries.sort(key=lambda m: wanted.index(m.group("net")))
    return found


def _vivado_kind(m: re.Match) -> str:
    """The driver of one report line; a flip-flop whose asynchronous pin is not driven by a
    constant or a flip-flop Q (or is not reported) is named as an offender."""
    driver = m.group("driver")
    if driver not in ASYNC_PINS:
        return driver
    pin, clear = ASYNC_PINS[driver], m.group("clear")
    if clear == CONSTANT or clear in FLIP_FLOPS:
        return driver
    cell = m.group("cell") or "(cell not reported)"
    if clear is None:
        return f"{driver} {cell}: no {pin} data"
    if clear in (UNCONNECTED, UNDRIVEN):
        return f"{driver} {cell}: {pin} is {clear}"
    return f"{driver} {cell}: {pin} is driven by {clear}"


def parse_vivado(text: str, nets: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Per port, what the report of `vivado_tcl` says drives each bit.

    Fails closed. Raises ValueError if the report is not complete (no end line), has a line
    with the report's prefix that cannot be read, names a port that was not asked about, or
    does not have, for every port asked about, either the single `missing` line of a net
    that is not in the design (which only `fold_constants` can then accept) or exactly one
    line per bit (`PORT_WIDTHS`), each bit once.
    """
    found = _report_entries(text, nets)
    return {port: tuple(_vivado_kind(m) for m in entries) for port, entries in found.items()}


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
    """What clocks a flip-flop: the net on its clock pin and what the walk back found.

    `names`: every name of the net ON the `C` pin (sorted); the expected clock must be one of
    them. `cells`: the types of the clock buffers walked through, nearest the flip-flop first
    (a `BUFGCE` in it means the clock is gated by an enable). `stopped_at`: the type of the
    first cell that is neither a clock buffer nor a named clock source (a LUT gating the
    clock, say), else None. `upstream`: the names of the nets further back (information: a
    name here is a DIFFERENT clock net from the one on the pin). `source`: (cell type, pin)
    of the clock source the walk ended at, when the caller named it; None when it ended at a
    net with no driver (a port). `inverted`: the `C` pin is inverted (`IS_C_INVERTED`); None
    when that could not be read. The Vivado report gives only the net on the `C` pin and the
    inversion: `cells` and `upstream` are empty and `source` is None.
    """

    names: tuple[str, ...]
    cells: tuple[str, ...] = ()
    stopped_at: str | None = None
    upstream: tuple[str, ...] = ()
    source: tuple[str, str] | None = None
    inverted: bool | None = False


def _inverted(cell: dict) -> bool | None:
    """`IS_C_INVERTED` of a Yosys netlist cell: absent is the primitive's default (0); a value
    that is not a binary string or an integer is None (unknown: an offender)."""
    value = cell.get("parameters", {}).get("IS_C_INVERTED")
    if value is None:
        return False
    if isinstance(value, int):
        return value != 0
    text = str(value).strip()
    if text and set(text) <= {"0", "1"}:
        return int(text, 2) != 0
    return None


def _bit_names(module: dict) -> dict:
    names: dict = {}
    for name, net in module["netnames"].items():
        for bit in net["bits"]:
            names.setdefault(bit, []).append(name)
    return names


def yosys_driver_clocks(
    netlist: dict,
    top: str,
    nets: dict[str, str],
    clock_sources: tuple[tuple[str, str], ...] = (),
) -> dict[str, tuple[ClockTrace | None, ...]]:
    """Per port and bit: the `ClockTrace` of the flip-flop driving it, or None.

    The clock pin is `C`. `names` are the names of the net on it. The net is then followed
    back through clock buffers (`CLOCK_BUFFERS`), recording the cells and the names further
    back (`upstream`). The walk ends at a net with no driver, or at an output pin listed in
    `clock_sources` as (cell type, pin), e.g. `("MMCME2_ADV", "CLKOUT0")` (`source`); any
    other cell stops it (`stopped_at`): a clock gated by logic, or made by a source nobody
    named, is not accepted. A bit not driven by a flip-flop, or a flip-flop with no `C`
    connection, gives None, as does a net that is not in the netlist; `None` is never
    acceptable to `clock_offenders`.
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
            at_pin = names.get(clock_bits[0], [])
            upstream: list[str] = []
            cells: list[str] = []
            stopped: str | None = None
            source: tuple[str, str] | None = None
            current = clock_bits[0]
            for _ in range(MAX_CLOCK_HOPS + 1):  # up to MAX_CLOCK_HOPS buffers, then the source
                if current != clock_bits[0]:
                    upstream += names.get(current, [])
                buf = driver.get(current)
                if buf is None:
                    break
                if (buf[0], buf[1]) in clock_sources:
                    source = (buf[0], buf[1])
                    break
                if buf[0] not in CLOCK_BUFFERS:
                    stopped = buf[0]
                    break
                if buf[1] != "O":  # a clock buffer, but not from its clock output
                    stopped = f"{buf[0]}.{buf[1]}"
                    break
                cells.append(buf[0])
                inputs = buf[2]["connections"].get("I", [])
                if not inputs:
                    break
                current = inputs[0]
            else:  # not a plain buffer chain: too long (or a loop)
                stopped = f"more than {MAX_CLOCK_HOPS} clock buffers"
            traces.append(
                ClockTrace(
                    tuple(sorted(set(at_pin))),
                    tuple(cells),
                    stopped,
                    tuple(sorted(set(upstream) - set(at_pin))),
                    source,
                    _inverted(found[2]),
                )
            )
        out[port] = tuple(traces) if traces else (None,)
    return out


def parse_vivado_clocks(
    text: str, nets: dict[str, str]
) -> dict[str, tuple[ClockTrace | None, ...]]:
    """Per port and bit: the clock net a flip-flop driver reports, as a `ClockTrace`, or None.

    Raises ValueError exactly as `parse_vivado` does. A line with no clock field gives None;
    a clock field without an inversion field gives `inverted=None` (both offenders).
    """

    def trace(m: re.Match) -> ClockTrace | None:
        if not m.group("clock"):
            return None
        inverted = None if m.group("inverted") is None else m.group("inverted") == "1"
        return ClockTrace((m.group("clock"),), inverted=inverted)

    found = _report_entries(text, nets)
    return {port: tuple(trace(m) for m in entries) for port, entries in found.items()}


def clock_offenders(
    drivers: dict[str, tuple[str, ...]],
    clocks: dict[str, tuple[ClockTrace | None, ...]],
    expected: str,
    allowed_trace: tuple[str, ...] | None = None,
    allowed_source: tuple[str, str] | None = None,
) -> list[str]:
    """One line per bit that cannot be shown to be a flip-flop on the `expected` clock net.

    Fails closed. Every bit of every port in `drivers` must be a constant (`CONSTANT`,
    `FOLDED`: no clock needed) or a flip-flop whose clock pin is not inverted and is ON the
    net `expected` (a name further back, in front of a clock buffer, is another net and is
    refused) and whose walk did not stop at a cell that is neither a clock buffer nor a named
    source. A missing port in `clocks`, a different number of entries than bits, a `None`
    entry, a wrong net, a clock gated by logic, an inverted or unreadable clock polarity, or
    a driver that is neither constant nor flip-flop is an offender, named `port[bit]`.

    `allowed_trace`, if given, is the exact tuple of clock buffer types the clock may pass
    through, and `allowed_source` the (cell type, pin) the walk must have ended at (the later
    build task pins both to the project clock's own chain: `("BUFGCE",)` from
    `("MMCME2_ADV", "CLKOUT0")`); otherwise any chain of buffers from a port or a named source
    is accepted. Both apply to the Yosys walk; a Vivado trace has neither.
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
                    further = (
                        f" ({expected} is further back, in front of {trace.cells[0]}: another net)"
                        if expected in trace.upstream and trace.cells
                        else ""
                    )
                    out.append(
                        f"{where}: clocked by {'/'.join(trace.names) or 'an unnamed net'}, "
                        f"not {expected}{further}"
                    )
                elif trace.inverted is None:
                    out.append(f"{where}: no clock polarity data (IS_C_INVERTED)")
                elif trace.inverted:
                    out.append(f"{where}: the clock pin is inverted (IS_C_INVERTED)")
                elif allowed_trace is not None and trace.cells != tuple(allowed_trace):
                    out.append(
                        f"{where}: clock passes through {'/'.join(trace.cells) or 'nothing'}, "
                        f"not {'/'.join(allowed_trace) or 'nothing'}"
                    )
                elif allowed_source is not None and trace.source != tuple(allowed_source):
                    got = ".".join(trace.source) if trace.source else "a port"
                    out.append(f"{where}: clock comes from {got}, not {'.'.join(allowed_source)}")
    return out
