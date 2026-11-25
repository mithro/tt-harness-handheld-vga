# SPDX-License-Identifier: Apache-2.0
"""Tests of `tt_harness.core.vga`: pinout, timing measurement, capture, framebuffer, classify.

The Migen-simulator tests use `tests/vga_model.py` (which knows nothing of the code under test)
and mostly a small synthetic mode, 40 x 20 clocks in total with a 16 x 8 picture, because the
Python simulator manages only some tens of thousands of clocks a second; each test states its
own numbers. Two tests run a real 640 x 480 frame size. The Verilator test (marked
`toolchain`) runs a real pool design.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from migen import Module, Signal
from migen.sim import run_simulation
from vga_model import VgaModel, encode  # noqa: F401

from tt_harness.core import vga

REPO = Path(__file__).resolve().parent.parent


# Pinout ------------------------------------------------------------------------------------

# Written out here, independently of vga.py: (bit, what) of the Tiny VGA Pmod on `uo_out`.
# Source: the vgacap signal map (hsync, vsync, R1, R0, G1, G0, B1, B0) = bits (7,3,0,4,1,5,2,6).
PINOUT = {
    0: ("r", 2),
    4: ("r", 1),
    1: ("g", 2),
    5: ("g", 1),
    2: ("b", 2),
    6: ("b", 1),
    3: ("vsync", 1),
    7: ("hsync", 1),
}


@pytest.mark.parametrize("bit", range(8))
def test_decode_one_bit_at_a_time(bit: int) -> None:
    name, weight = PINOUT[bit]
    expected = {"r": 0, "g": 0, "b": 0, "hsync": 0, "vsync": 0}
    expected[name] = weight
    r, g, b, hsync, vsync = vga.decode(1 << bit)
    assert (r, g, b, hsync, vsync) == (
        expected["r"],
        expected["g"],
        expected["b"],
        expected["hsync"],
        expected["vsync"],
    )


def test_decode_all_bytes_agree_with_the_models_encoder() -> None:
    for colour in range(64):
        for hs in (0, 1):
            for vs in (0, 1):
                r, g, b, h, v = vga.decode(encode(colour, hs, vs))
                assert (h, v) == (hs, vs)
                assert vga.pack(r, g, b) == colour
                assert vga.unpack(colour) == (r, g, b)
                assert (r, g, b) == (colour >> 4, (colour >> 2) & 3, colour & 3)


def test_decode_works_on_signals() -> None:
    class Probe(Module):
        def __init__(self) -> None:
            self.uo_out = Signal(8)
            self.r, self.g, self.b = Signal(2), Signal(2), Signal(2)
            self.h, self.v = Signal(), Signal()
            r, g, b, h, v = vga.decode(self.uo_out)
            self.comb += [self.r.eq(r), self.g.eq(g), self.b.eq(b), self.h.eq(h), self.v.eq(v)]

    dut = Probe()
    seen = {}

    def gen():
        for value in (0x01, 0x10, 0x82, 0x48, 0xFF):
            yield dut.uo_out.eq(value)
            yield
            row = []
            for sig in (dut.r, dut.g, dut.b, dut.h, dut.v):
                row.append((yield sig))
            seen[value] = row

    run_simulation(dut, gen())
    assert seen == {
        0x01: [2, 0, 0, 0, 0],  # R1
        0x10: [1, 0, 0, 0, 0],  # R0
        0x82: [0, 2, 0, 1, 0],  # G1 + hsync
        0x48: [0, 0, 1, 0, 1],  # B0 + vsync
        0xFF: [3, 3, 3, 1, 1],
    }


def test_pixel_format_is_red_in_the_top_bits() -> None:
    assert vga.pack(3, 0, 0) == 0b110000
    assert vga.pack(0, 3, 0) == 0b001100
    assert vga.pack(0, 0, 3) == 0b000011
    assert vga.unpack(0b100111) == (2, 1, 3)


# classify and the mode table ---------------------------------------------------------------


def test_classify_640x480_at_60() -> None:
    mode = vga.classify(800, 525, 96, 2, 25_175_000)
    assert mode is not None
    assert (mode.divider, mode.h_offset, mode.v_offset, mode.width, mode.height) == (
        1,
        96 + 48,
        2 + 33,
        640,
        480,
    )
    assert "640x480" in mode.name


def test_classify_double_rate_design() -> None:
    mode = vga.classify(1600, 525, 192, 2, 50_350_000)
    assert mode is not None
    assert (mode.divider, mode.h_offset, mode.v_offset, mode.width, mode.height) == (
        2,
        192 + 48 * 2,
        35,
        640,
        480,
    )


def test_classify_800x600_and_1024x768() -> None:
    m = vga.classify(1056, 628, 128, 4, 40_000_000)
    assert m is not None
    assert (m.divider, m.h_offset, m.v_offset, m.width, m.height) == (1, 128 + 88, 4 + 23, 800, 600)
    m = vga.classify(1344, 806, 136, 6, 65_000_000)
    assert m is not None
    assert (m.divider, m.h_offset, m.v_offset, m.width, m.height) == (
        1,
        136 + 160,
        6 + 29,
        1024,
        768,
    )
    m = vga.classify(2112, 628, 256, 4, 80_000_000)  # 800x600 at divider 2
    assert m is not None
    assert (m.divider, m.h_offset, m.width, m.height) == (2, 256 + 88 * 2, 800, 600)


def test_classify_derives_offsets_from_the_measured_sync_width() -> None:
    odd = vga.classify(800, 525, 64, 3, 25_175_000)
    assert odd is not None
    assert (odd.h_offset, odd.v_offset) == (64 + 48, 3 + 33)


def test_classify_is_not_vetoed_by_a_wrong_clock() -> None:
    for clock in (0, 20_000_000, 25_000_000, 31_500_000, 100_000_000):
        mode = vga.classify(800, 525, 96, 2, clock)
        assert mode is not None
        assert (mode.divider, mode.width, mode.height) == (1, 640, 480)


def test_classify_reports_the_refresh_from_the_clock() -> None:
    a = vga.classify(800, 525, 96, 2, 25_175_000)
    b = vga.classify(800, 525, 96, 2, 25_000_000)
    assert a is not None
    assert b is not None
    assert a.name != b.name  # 59.9 Hz against 59.5 Hz
    assert "59.9" in a.name
    assert "59.5" in b.name


@pytest.mark.parametrize(
    ("cpl", "lpf", "hs", "vs"),
    [
        (801, 525, 96, 2),  # not a mode
        (800, 526, 96, 2),
        (800, 524, 96, 2),
        (1200, 525, 144, 2),  # 1.5 times 800: not a whole divider
        (2400, 525, 288, 2),  # divider 3 is not in the table
        (4095, 2047, 1, 1),  # saturated counters
        (0, 0, 0, 0),  # stuck sync
        (800, 525, 0, 2),  # no hsync pulse
        (800, 525, 700, 2),  # the picture would not fit
        (800, 525, 96, 0),
    ],
)
def test_unknown_mode_classifies_as_none(cpl: int, lpf: int, hs: int, vs: int) -> None:
    assert vga.classify(cpl, lpf, hs, vs, 25_175_000) is None


def test_every_table_entry_classifies_as_itself() -> None:
    for spec in vga.MODES:
        for divider in spec.dividers:
            mode = vga.classify(
                spec.h_total * divider,
                spec.v_total,
                spec.h_sync * divider,
                spec.v_sync,
                spec.pixel_clock_hz * divider,
            )
            assert mode is not None, spec.name
            assert (mode.width, mode.height, mode.divider) == (spec.width, spec.height, divider)
            assert mode.h_offset == (spec.h_sync + spec.h_back) * divider
            assert mode.v_offset == spec.v_sync + spec.v_back


def test_table_has_no_ambiguous_entries() -> None:
    seen: dict[tuple[int, int], str] = {}
    for spec in vga.MODES:
        for divider in spec.dividers:
            key = (spec.h_total * divider, spec.v_total)
            assert key not in seen, f"{spec.name} x{divider} collides with {seen[key]}"
            seen[key] = spec.name


def test_the_c_header_is_current() -> None:
    committed = (REPO / "tt_harness" / "core" / "vga_modes.h").read_text()
    assert committed == vga.modes_header(), (
        "tt_harness/core/vga_modes.h is stale: regenerate it with "
        "`uv run python -m tt_harness.core.vga > tt_harness/core/vga_modes.h`"
    )


def test_the_c_header_lists_every_entry() -> None:
    text = vga.modes_header()
    entries = sum(len(spec.dividers) for spec in vga.MODES)
    assert text.count('\n    {"') == entries
    assert f"#define VGA_MODE_COUNT {entries}" in text
    assert "malloc" not in text
    assert "640x480" in text
