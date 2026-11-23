# SPDX-License-Identifier: Apache-2.0
"""Digilent Arty A7-35T board layer for the compare build.

Pins come from litex-boards' `digilent_arty` platform (clk100 on E3, serial TX on D10, LD4 on
H5). The system clock is the 100 MHz oscillator. The recorder's `pclk` drives the project
through a BUFG, so the project's flip-flops sit on a global clock network.

The A7-35T is the `xc7a35ticsg324-1L` that litex-boards (and Vivado) use; the openXC7 chip
database is built for `xc7a35tcsg324-1`, the same die and package, as LiteX's own openXC7
backend maps it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from litex.build.generic_platform import IOStandard, Pins, Subsignal
from litex_boards.platforms import digilent_arty
from migen import Instance, Module, Signal
from migen.fhdl import verilog

from tt_harness.compare.top import CompareTop
from tt_harness.manifest import Project

NAME = "arty"
SYS_CLK_FREQ = 100_000_000
PARTS = {"vivado": "xc7a35ticsg324-1L", "openxc7": "xc7a35tcsg324-1"}
MODULE_NAME = "arty_compare"
BUFG_NAME = "pclk_bufg"
SYS_CLK_PORT = "clk100"
SYS_BUFG_NAME = "sys_bufg"


@dataclass(frozen=True)
class Pin:
    port: str
    site: str
    iostandard: str


def _litex_pin(port: str, resource: str, number: int = 0, subsignal: str | None = None) -> Pin:
    """Look up one single-pin resource in litex-boards' Arty IO table."""
    for name, num, *items in digilent_arty._io:
        if (name, num) != (resource, number):
            continue
        iostandard = next((i.name for i in items if isinstance(i, IOStandard)), None)
        if subsignal is not None:
            sub = next(i for i in items if isinstance(i, Subsignal) and i.name == subsignal)
            items = sub.constraints
            iostandard = next((i.name for i in items if isinstance(i, IOStandard)), iostandard)
        (pins,) = (i for i in items if isinstance(i, Pins))
        (site,) = pins.identifiers
        if iostandard is None:
            raise ValueError(f"litex-boards Arty {resource}:{number} has no IO standard")
        return Pin(port, site, iostandard)
    raise KeyError(f"litex-boards Arty platform has no {resource}:{number}")


PINS = (
    _litex_pin("clk100", "clk100"),
    _litex_pin("serial_tx", "serial", 0, "tx"),
    _litex_pin("user_led0", "user_led", 0),
)


def bufg(i: Signal, o: Signal) -> Instance:
    """A global clock buffer from `i` to `o`."""
    return Instance("BUFG", name=BUFG_NAME, i_I=i, o_O=o)


class ArtyCompare(Module):
    """`CompareTop` on the Arty: clk100 in, UART TX out, `done` on LD4; `pclk` via a BUFG."""

    def __init__(self, project: Project, baud: int, clocks: int, settle: int = 2) -> None:
        self.clk100 = Signal(name_override="clk100")
        self.serial_tx = Signal(name_override="serial_tx")
        self.user_led0 = Signal(name_override="user_led0")
        self.submodules.compare = compare = CompareTop(
            project, SYS_CLK_FREQ, baud, clocks, settle=settle, clock_buffer=bufg
        )
        # The system clock's BUFG is explicit too, so that synthesis need not insert clock
        # buffers itself: Yosys's automatic one would also buffer a clock the project makes
        # from a register that drives data (dottee's vsync, which is uo_out[3]), and
        # nextpnr-xilinx cannot route a global clock net to data pins.
        sys_clk = Signal(name_override="sys_clk_buffered")
        self.specials += Instance("BUFG", name=SYS_BUFG_NAME, i_I=self.clk100, o_O=sys_clk)
        self.comb += [
            compare.clk.eq(sys_clk),
            compare.rst.eq(0),
            self.serial_tx.eq(compare.tx),
            self.user_led0.eq(compare.done),
        ]


Top = ArtyCompare  # the board's compare-build top, as tt_harness.build uses it


def to_verilog(board_top: ArtyCompare, path: Path) -> Path:
    """Write the board top as the Verilog module `arty_compare` to `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ios = {board_top.clk100, board_top.serial_tx, board_top.user_led0}
    text = str(verilog.convert(board_top, ios=ios, name=MODULE_NAME))
    # Harness glue, not project source: Migen's ports carry no net type, so this file must
    # not be read under a project's `default_nettype none`. The flows also read it first.
    path.write_text("`default_nettype wire\n" + text)
    return path


def pin_xdc() -> str:
    """XDC placing and setting the IO standard of every top-level port."""
    lines = []
    for pin in PINS:
        lines.append(f"set_property LOC {pin.site} [get_ports {pin.port}]")
        lines.append(f"set_property IOSTANDARD {pin.iostandard} [get_ports {pin.port}]")
    return "\n".join(lines) + "\n"
