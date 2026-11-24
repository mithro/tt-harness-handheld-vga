# SPDX-License-Identifier: Apache-2.0
import re

import pytest
from litex.build.generic_platform import IOStandard, Pins, Subsignal
from litex.build.xilinx import XilinxPlatform

from tt_harness.boards.gamebub import platform as gb

# The pin table in docs/gamebub-hardware.md, written out independently of the platform.
EXPECTED = {
    ("clk50", None): ["P17"],
    ("lcd", "dotclk"): ["T18"],
    ("lcd", "hsync"): ["U18"],
    ("lcd", "vsync"): ["T16"],
    ("lcd", "de"): ["R18"],
    ("lcd", "b"): ["R17", "P18", "N17", "M17", "M18", "L16", "L18", "K17"],
    ("lcd", "g"): ["J17", "J18", "H16", "H17", "G18", "G17", "F18", "E17"],
    ("lcd", "r"): ["E18", "E16", "D18", "D17", "C17", "B18", "B17", "A18"],
    ("buttons", "a"): ["L6"],
    ("buttons", "b"): ["K6"],
    ("buttons", "x"): ["M6"],
    ("buttons", "y"): ["N5"],
    ("buttons", "up"): ["N16"],
    ("buttons", "down"): ["L13"],
    ("buttons", "left"): ["L14"],
    ("buttons", "right"): ["M16"],
    ("buttons", "l"): ["K13"],
    ("buttons", "r"): ["K5"],
    ("buttons", "start"): ["H6"],
    ("buttons", "select"): ["H5"],
    ("dac", "mclk"): ["T15"],
    ("dac", "wclk"): ["R15"],
    ("dac", "bclk"): ["R16"],
    ("dac", "din"): ["P15"],
    ("mcu_qspi", "clk"): ["T14"],
    ("mcu_qspi", "cs_n"): ["U17"],
    ("mcu_qspi", "dq"): ["V15", "V16", "U16", "V17"],
    ("mcu_irq", None): ["N15"],
    ("pmod", None): ["V14", "U14", "U13", "R13"],
    ("tp", None): ["M14", "M13", "N14", "P14"],
}

# CSG324 is an 18 x 18 ball grid: rows A-V without I, O, Q, S; columns 1-18.
BALL = re.compile(r"[A-HJ-NPRTUV](1[0-8]|[1-9])")


def _flatten():
    """{(resource, subsignal or None): [balls]} and the IOStandard of each, from _io."""
    pins, standards = {}, {}
    for name, number, *items in gb._io:
        assert number == 0, name
        top_std = [i.name for i in items if isinstance(i, IOStandard)]
        for item in items:
            if isinstance(item, Pins):
                pins[(name, None)] = item.identifiers
                standards[(name, None)] = top_std
            elif isinstance(item, Subsignal):
                sub_pins = [c for c in item.constraints if isinstance(c, Pins)]
                sub_std = [c.name for c in item.constraints if isinstance(c, IOStandard)]
                assert len(sub_pins) == 1, (name, item.name)
                pins[(name, item.name)] = sub_pins[0].identifiers
                standards[(name, item.name)] = sub_std or top_std
    return pins, standards


def test_pins_match_the_hardware_notes():
    pins, _ = _flatten()
    assert pins == EXPECTED


def test_every_pin_is_lvcmos33():
    _, standards = _flatten()
    for key, std in standards.items():
        assert std == ["LVCMOS33"], key


def test_balls_are_valid_csg324_and_unique():
    pins, _ = _flatten()
    balls = [b for bs in pins.values() for b in bs]
    for b in balls:
        assert BALL.fullmatch(b), b
    assert len(balls) == len(set(balls)), sorted(b for b in balls if balls.count(b) > 1)


@pytest.mark.parametrize("toolchain", ["vivado", "openxc7"])
def test_device_and_toolchains(toolchain):
    p = gb.Platform(toolchain=toolchain)
    assert isinstance(p, XilinxPlatform)
    assert p.device == "xc7a100tcsg324-1"
    assert p.default_clk_name == "clk50"
    assert p.default_clk_period == pytest.approx(20.0)


@pytest.mark.parametrize("toolchain", ["ise", "f4pga", "yosys+nextpnr", "quartus", ""])
def test_unknown_toolchain_rejected(toolchain):
    with pytest.raises(ValueError, match="toolchain"):
        gb.Platform(toolchain=toolchain)


def test_request_every_resource():
    p = gb.Platform(toolchain="openxc7")
    names = sorted({name for name, _ in EXPECTED})
    for name in names:
        r = p.request(name)
        assert r is not None, name
    lcd = p.lookup_request("lcd")
    assert len(lcd.r) == len(lcd.g) == len(lcd.b) == 8
    assert len(p.lookup_request("pmod")) == 4
    assert len(p.lookup_request("tp")) == 4
    assert len(p.lookup_request("mcu_qspi").dq) == 4
    assert len(p.lookup_request("mcu_irq")) == 1
    buttons = p.lookup_request("buttons")
    for b in ("a", "b", "x", "y", "up", "down", "left", "right", "l", "r", "start", "select"):
        assert len(getattr(buttons, b)) == 1


BUTTONS = ("a", "b", "x", "y", "up", "down", "left", "right", "l", "r", "start", "select")


@pytest.mark.parametrize("toolchain", ["vivado", "openxc7"])
def test_buttons_have_internal_pullups_in_generated_xdc(toolchain):
    # Rev 4.1 does not fit the external pull-up packs (RN1601-RN1603, R1602 are DNP on
    # buttons.kicad_sch); the buttons rely on the FPGA's internal pull-ups. Both LiteX
    # Xilinx toolchains (Vivado and yosys+nextpnr/openXC7) write the XDC with _build_xdc.
    from litex.build.xilinx.vivado import _build_xdc

    p = gb.Platform(toolchain=toolchain)
    p.request("buttons")
    p.request("lcd")
    named = [
        (f"{res[0]}_{res[2]}" if res[2] else res[0], pins, others, res)
        for _, pins, others, res in p.constraint_manager.get_sig_constraints()
    ]
    xdc = _build_xdc(named, [])
    for b in BUTTONS:
        assert f"set_property PULLTYPE PULLUP [get_ports {{buttons_{b}}}]" in xdc, b
    # Only the buttons get a pull-up.
    assert xdc.count("PULLTYPE PULLUP") == len(BUTTONS)


@pytest.mark.parametrize(("toolchain", "present"), [("vivado", True), ("openxc7", False)])
def test_vivado_xdc_sets_the_configuration_bank_voltage(tmp_path, toolchain, present) -> None:
    """The generated XDC carries CFGBVS and CONFIG_VOLTAGE for Vivado only."""
    from migen import Module

    platform = gb.Platform(toolchain=toolchain)
    top = Module()
    top.comb += platform.request("tp", 0).eq(0)
    platform.build(top, run=False, build_dir=str(tmp_path), build_name="top")
    xdc = (tmp_path / "top.xdc").read_text()
    assert ("set_property CFGBVS VCCO [current_design]" in xdc) is present
    assert ("set_property CONFIG_VOLTAGE 3.3 [current_design]" in xdc) is present
