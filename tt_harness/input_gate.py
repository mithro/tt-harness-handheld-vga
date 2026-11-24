# SPDX-License-Identifier: Apache-2.0
"""Gate on the implemented netlist: what drives each net into the project.

A project may use any input asynchronously (`rst_n` as an asynchronous reset), so the net on
every project input other than the clock must be a constant or be driven directly by a
flip-flop: the output of a LUT, a carry chain or an inverter can glitch. The source is checked
by `tt_harness.compare.inputs`; this checks what the tools made of it, because synthesis may
insert or duplicate logic (a LUT used as a buffer, an inverter folded into a LUT, a register
whose polarity was changed and an inverter added behind it).

Finding the nets. The project is flattened into the top by Yosys and its hierarchy is rebuilt
by Vivado, so "the project's `rst_n` port" is not a reliable name in either netlist. The nets
are found from the harness side instead: the generated top connects every project port to
one named signal (`.rst_n(stimulus_rst_n)`), which `instance_connections` reads from the
Verilog, and the compare top marks the one that is not a constant (`rst_n`) `keep`, so both
tools preserve it as a net with that name and cannot fold it into other logic. A net that
cannot be found fails the gate, with one exception: both tools propagate a constant into
the project and drop its net. A net that the generated top itself assigns a literal
(`assign stimulus_ena = 1'd1;`, found by `source_constants`) may therefore be absent, and is
then recorded as `FOLDED`; it may not have any other driver. The constant nets are not
marked `keep`: that gains nothing (Vivado removes them anyway) and it stops Vivado
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
    rf"^{re.escape(_LINE)}(?P<port>\w+) net (?P<net>\S+) driver (?P<driver>\S+)\s*$", re.MULTILINE
)


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
        f'        puts $tt_inputs "{_LINE}$port net $net driver $kind"',
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
