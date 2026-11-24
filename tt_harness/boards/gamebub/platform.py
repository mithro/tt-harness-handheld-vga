# SPDX-License-Identifier: Apache-2.0
"""LiteX platform for the Game Bub handheld (board revision 4.1).

Pins are from the table in docs/gamebub-hardware.md, which records them from the upstream
constraints file (`framework/verilog/handheld/rev_4.xdc`). Only the resources the harness
uses are listed; cartridge, link port, SDRAM, SRAM, HDMI and rumble are left out.
"""

from litex.build.generic_platform import IOStandard, Misc, Pins, Subsignal
from litex.build.xilinx import Xilinx7SeriesPlatform

DEVICE = "xc7a100tcsg324-1"
TOOLCHAINS = ("vivado", "openxc7")

_io = [
    # 50 MHz oscillator.
    ("clk50", 0, Pins("P17"), IOStandard("LVCMOS33")),
    # ILI9806E panel, 24-bit parallel RGB driven directly by the FPGA.
    (
        "lcd",
        0,
        Subsignal("dotclk", Pins("T18")),
        Subsignal("hsync", Pins("U18")),
        Subsignal("vsync", Pins("T16")),
        Subsignal("de", Pins("R18")),
        Subsignal("r", Pins("E18 E16 D18 D17 C17 B18 B17 A18")),
        Subsignal("g", Pins("J17 J18 H16 H17 G18 G17 F18 E17")),
        Subsignal("b", Pins("R17 P18 N17 M17 M18 L16 L18 K17")),
        IOStandard("LVCMOS33"),
    ),
    # The twelve FPGA-side buttons. Active low: each switch pulls its line to ground. The
    # external pull-up packs on the rev 4.1 buttons sheet (gamebub-pcb
    # handheld_rev4_1/buttons.kicad_sch: RN1601, RN1602, RN1603 and R1602) are marked DNP,
    # not fitted; the sheet's note gives the Artix-7 internal pull-up current instead. The
    # board relies on the FPGA's internal pull-ups, so they are required here (the same
    # PULLTYPE PULLUP property the upstream constraints set). Task 7b must confirm the open
    # flow (openXC7) actually emits the pull-up in its FASM, not only Vivado.
    (
        "buttons",
        0,
        Subsignal("a", Pins("L6")),
        Subsignal("b", Pins("K6")),
        Subsignal("x", Pins("M6")),
        Subsignal("y", Pins("N5")),
        Subsignal("up", Pins("N16")),
        Subsignal("down", Pins("L13")),
        Subsignal("left", Pins("L14")),
        Subsignal("right", Pins("M16")),
        Subsignal("l", Pins("K13")),
        Subsignal("r", Pins("K5")),
        Subsignal("start", Pins("H6")),
        Subsignal("select", Pins("H5")),
        IOStandard("LVCMOS33"),
        Misc("PULLTYPE=PULLUP"),
    ),
    # TLV320DAC3101 I2S; the FPGA generates all four clocks/data.
    (
        "dac",
        0,
        Subsignal("mclk", Pins("T15")),
        Subsignal("wclk", Pins("R15")),
        Subsignal("bclk", Pins("R16")),
        Subsignal("din", Pins("P15")),
        IOStandard("LVCMOS33"),
    ),
    # QSPI link from the ESP32-S3 MCU (the FPGA is the slave). clk and dq[0] share nets with
    # CCLK and DIN used during slave-serial configuration.
    (
        "mcu_qspi",
        0,
        Subsignal("clk", Pins("T14")),
        Subsignal("cs_n", Pins("U17")),
        Subsignal("dq", Pins("V15 V16 U16 V17")),
        IOStandard("LVCMOS33"),
    ),
    # Interrupt to the MCU. Active HIGH, driven push-pull: it drives the gate of an n-FET
    # (Q801) whose drain is the MCU's active-low, level-triggered interrupt line (see
    # docs/gamebub-mcu-protocol.md). The upstream XDC calls this port `mcu_irq_n`, a name
    # left over from rev 1, where it was open-drain and active low.
    ("mcu_irq", 0, Pins("N15"), IOStandard("LVCMOS33")),
    # PMOD header, 4 I/O with 200R series resistors.
    ("pmod", 0, Pins("V14 U14 U13 R13"), IOStandard("LVCMOS33")),
    # Test points.
    ("tp", 0, Pins("M14 M13 N14 P14"), IOStandard("LVCMOS33")),
]


class Platform(Xilinx7SeriesPlatform):
    default_clk_name = "clk50"
    default_clk_period = 1e9 / 50e6

    def __init__(self, toolchain: str = "vivado"):
        if toolchain not in TOOLCHAINS:
            raise ValueError(
                f"unsupported toolchain {toolchain!r} for Game Bub: use one of {TOOLCHAINS}"
            )
        Xilinx7SeriesPlatform.__init__(self, DEVICE, _io, toolchain=toolchain)
        if toolchain == "vivado":
            # Bank 0 is powered at 3.3 V on this board; without these Vivado's DRC warns
            # CFGBVS-1. nextpnr-xilinx has no such property.
            self.add_platform_command("set_property CFGBVS VCCO [current_design]")
            self.add_platform_command("set_property CONFIG_VOLTAGE 3.3 [current_design]")

    def do_finalize(self, fragment):
        Xilinx7SeriesPlatform.do_finalize(self, fragment)
        self.add_period_constraint(
            self.lookup_request(self.default_clk_name, loose=True), self.default_clk_period
        )
