# SPDX-License-Identifier: Apache-2.0
"""Tests of `tt_harness.core.vga`: pinout, timing measurement, capture, framebuffer, classify.

The Migen-simulator tests use `tests/vga_model.py` (which knows nothing of the code under test)
and mostly a small synthetic mode, 40 x 20 clocks in total with a 16 x 8 picture, because the
Python simulator manages only some tens of thousands of clocks a second; each test states its
own numbers. Two tests run a real 640 x 480 frame size. The Verilator test (marked
`toolchain`) runs a real pool design.
"""

from __future__ import annotations

import re
import subprocess
import zlib
from pathlib import Path

import pytest
from migen import ClockDomain, Module, Signal
from migen.fhdl import verilog
from migen.sim import run_simulation
from vga_model import VgaModel, encode

from tt_harness import pool, reference, tools, verdict, verilate
from tt_harness.compare import inputs
from tt_harness.core import vga
from tt_harness.host.frame import Recording, write_vgacap

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


# Helpers for the simulator tests -------------------------------------------------------------

# A small synthetic mode: 40 x 20 clocks in total (800 clocks a frame), picture 16 x 8.
SMALL = {
    "h_active": 16,
    "h_front": 4,
    "h_sync": 6,
    "h_back": 14,
    "v_active": 8,
    "v_front": 2,
    "v_sync": 2,
    "v_back": 8,
}


def small(**changes) -> VgaModel:
    return VgaModel(**{**SMALL, **changes})


def trace(dut, values, observe: dict, events: dict | None = None) -> dict[str, list[int]]:
    """Drive `dut.uo_out` with `values`, one per clock; return `observe` values per clock.

    Entry c of each list is the signal in the clock in which `values[c]` is on `uo_out`:
    registers show what the clocks before c produced, combinational outputs show c itself.
    `values[0]` is on the pins from reset, so the first clock after reset is clock -1.
    `events[c]` is a list of statements (signal.eq(...)) applied in clock c.
    """
    seen: dict[str, list[int]] = {name: [] for name in observe}
    values = list(values)

    dut.uo_out.reset = values[0]  # in place from the start: clock -1 is the first after reset

    def gen():
        for c, value in enumerate(values):
            yield from (events or {}).get(c, ())
            yield dut.uo_out.eq(value)
            yield
            for name, signal in observe.items():
                seen[name].append((yield signal))

    run_simulation(dut, gen())
    return seen


def pin_rises(values: list[int], bit: int) -> list[int]:
    """Clocks in which the pin goes from 0 to 1 (the pin, whatever its polarity)."""
    return [
        c for c in range(1, len(values)) if values[c] >> bit & 1 and not values[c - 1] >> bit & 1
    ]


def pin_active_edges(values: list[int], bit: int, positive: bool) -> list[int]:
    """Clocks in which a sync pin goes to its active level (the sync pulse's leading edge)."""
    level = int(positive)
    return [
        c
        for c in range(1, len(values))
        if (values[c] >> bit & 1) == level and (values[c - 1] >> bit & 1) != level
    ]


HSYNC_BIT, VSYNC_BIT = 7, 3

MEASUREMENTS = (
    "clocks_per_line",
    "lines_per_frame",
    "hsync_width",
    "vsync_lines",
    "hsync_positive",
    "vsync_positive",
)
TIMING_OUTPUTS = (*MEASUREMENTS, "frames", "locked", "no_signal")


def observe_timing(t) -> dict:
    return {name: getattr(t, name) for name in TIMING_OUTPUTS}


def measured(seen: dict, c: int) -> tuple:
    return tuple(seen[k][c] for k in MEASUREMENTS)


# VgaTiming -----------------------------------------------------------------------------------


@pytest.mark.parametrize("hsync_positive", [False, True])
@pytest.mark.parametrize("vsync_positive", [False, True])
@pytest.mark.parametrize("vsync_phase", ["line_start", "hsync"])
def test_positive_sync_polarities(hsync_positive, vsync_positive, vsync_phase) -> None:
    model = small(
        hsync_positive=hsync_positive, vsync_positive=vsync_positive, vsync_phase=vsync_phase
    )
    t = vga.VgaTiming()
    clocks = 4 * model.clocks_per_frame
    seen = trace(t, model.stream(clocks), observe_timing(t))
    assert measured(seen, clocks - 1) == (
        40,
        20,
        6,
        2,
        int(hsync_positive),
        int(vsync_positive),
    )
    assert seen["locked"][clocks - 1] == 1


@pytest.mark.parametrize("hsync_positive", [False, True])
@pytest.mark.parametrize("vsync_positive", [False, True])
@pytest.mark.parametrize("skip", [0, 333, 400 + 61])
def test_locks_within_three_frames_from_reset(hsync_positive, vsync_positive, skip) -> None:
    """Reset in any phase of the frame: published at the second vsync, locked at the third."""
    model = small(hsync_positive=hsync_positive, vsync_positive=vsync_positive)
    t = vga.VgaTiming()
    clocks = 4 * model.clocks_per_frame
    values = list(model.stream(clocks, skip=skip))
    seen = trace(t, values, observe_timing(t))
    rises = pin_rises(values, VSYNC_BIT)  # polarity-independent: the pin's 0 -> 1 edges
    # `frames` counts the publications; registers show the clock after the edge.
    assert [seen["frames"][c] for c in (rises[1], rises[1] + 1, rises[2], rises[2] + 1)] == [
        0,
        1,
        1,
        2,
    ]
    expected = (40, 20, 6, 2, int(hsync_positive), int(vsync_positive))
    assert measured(seen, rises[1] + 1) == expected
    assert seen["locked"][rises[1] + 1] == 0  # one measurement is not yet two the same
    assert seen["locked"][rises[2]] == 0
    assert seen["locked"][rises[2] + 1] == 1
    assert rises[2] + 1 <= 3 * model.clocks_per_frame  # within three frames of reset
    assert all(seen["locked"][c] for c in range(rises[2] + 1, clocks))
    assert all(measured(seen, c) == expected for c in range(rises[1] + 1, clocks))


def test_double_rate_design_is_divided() -> None:
    model = small(divider=2)
    t = vga.VgaTiming()
    clocks = 4 * model.clocks_per_frame
    seen = trace(t, model.stream(clocks), observe_timing(t))
    assert measured(seen, clocks - 1) == (80, 20, 12, 2, 0, 0)
    assert seen["locked"][clocks - 1] == 1


def test_measurements_publish_together() -> None:
    first = small()
    second = small(
        h_active=12,
        h_front=2,
        h_sync=4,
        h_back=8,
        v_active=9,
        v_front=3,
        v_sync=3,
        v_back=5,
        hsync_positive=True,
        vsync_positive=True,
    )
    assert (second.h_total, second.v_total) == (26, 20)
    values = list(first.stream(3 * first.clocks_per_frame))
    values += list(second.stream(5 * second.clocks_per_frame, skip=17))
    t = vga.VgaTiming()
    seen = trace(t, values, observe_timing(t))
    publications = [c for c in range(1, len(values)) if seen["frames"][c] != seen["frames"][c - 1]]
    assert all(seen["frames"][c] == seen["frames"][c - 1] + 1 for c in publications)
    moved = []
    for c in range(1, len(values)):
        if measured(seen, c) != measured(seen, c - 1):
            moved.append(c)
            assert c in publications, f"clock {c}: a measurement changed between publications"
    assert len(moved) >= 2  # the first measurement and the change of mode
    # The frame that spans the change of mode is measured as it is (a partial mix); the
    # frames after it give the second mode's numbers, all at once.
    assert measured(seen, len(values) - 1) == (26, 20, 4, 3, 1, 1)
    final = next(c for c in moved if measured(seen, c) == (26, 20, 4, 3, 1, 1))
    assert measured(seen, final - 1) != measured(seen, final)
    assert seen["frames"][final] == seen["frames"][final - 1] + 1


def test_frames_wrap() -> None:
    t = vga.VgaTiming()
    t.frames.reset = 0xFFFF  # one publication from wrapping
    model = small()
    values = list(model.stream(4 * model.clocks_per_frame))
    seen = trace(t, values, {"frames": t.frames})
    assert seen["frames"][0] == 0xFFFF
    assert seen["frames"][-1] == 2  # 0xFFFF -> 0 -> 1 -> 2


def test_no_signal_from_reset_until_the_first_measurement() -> None:
    t = vga.VgaTiming(no_signal_clocks=1000)
    seen = trace(t, [0x00] * 1200, observe_timing(t))  # a dead bus: no sync pulses at all
    assert all(seen["no_signal"])
    assert not any(seen["locked"])
    assert seen["frames"][-1] == 0
    t = vga.VgaTiming(no_signal_clocks=1000)
    model = small()
    values = list(model.stream(3 * model.clocks_per_frame))
    seen = trace(t, values, observe_timing(t))
    rises = pin_rises(values, VSYNC_BIT)
    assert seen["no_signal"][rises[1]] == 1
    assert seen["no_signal"][rises[1] + 1] == 0  # cleared with the first measurement


def test_no_signal_returns_when_the_signal_stops() -> None:
    n = 1000
    t = vga.VgaTiming(no_signal_clocks=n)
    model = small()
    live = list(model.stream(4 * model.clocks_per_frame))
    values = live + [live[-1] & ~(1 << HSYNC_BIT)] * 2500
    # then the signal comes back, in another phase of the frame
    values += list(model.stream(5 * model.clocks_per_frame, skip=123))
    seen = trace(t, values, observe_timing(t))
    last_rise = pin_rises(live, VSYNC_BIT)[-1]
    assert not any(seen["no_signal"][c] for c in range(last_rise + 1, last_rise + n + 1))
    assert seen["no_signal"][last_rise + n + 1] == 1
    assert all(seen["no_signal"][c] for c in range(last_rise + n + 1, len(live) + 2500))
    assert seen["locked"][last_rise + n] == 1
    assert seen["locked"][last_rise + n + 1] == 0  # not locked on nothing
    # and back again: the first rise only starts a frame, the next gives a measurement
    rises = [c for c in pin_rises(values, VSYNC_BIT) if c >= len(live) + 2500]
    assert seen["no_signal"][rises[0] + 1] == 1
    assert seen["no_signal"][rises[1]] == 1
    assert seen["no_signal"][rises[1] + 1] == 0
    assert seen["frames"][-1] > seen["frames"][last_rise + n + 1]


def test_an_edge_exactly_one_period_apart_is_in_time() -> None:
    model = small()
    n = model.clocks_per_frame  # the frame period is exactly the limit
    t = vga.VgaTiming(no_signal_clocks=n)
    values = list(model.stream(5 * n))
    seen = trace(t, values, observe_timing(t))
    rises = pin_rises(values, VSYNC_BIT)
    assert not any(seen["no_signal"][c] for c in range(rises[1] + 1, len(values)))


def test_no_signal_limit_is_above_the_largest_frame() -> None:
    # 800 x 600 at divider 2, 2112 x 628 clocks, is the largest frame in the mode table.
    largest = max(m.h_total * max(m.dividers) * m.v_total for m in vga.MODES)
    assert largest == 2112 * 628
    assert vga.NO_SIGNAL_CLOCKS == 1 << 23
    assert vga.NO_SIGNAL_CLOCKS >= 6 * largest


def test_stuck_hsync_does_not_look_like_a_mode() -> None:
    model = small()
    values = [v | (1 << HSYNC_BIT) for v in model.stream(8 * model.clocks_per_frame)]
    t = vga.VgaTiming()
    seen = trace(t, values, observe_timing(t))
    # vsync still pulses; hsync is stuck high: no lines, a line length that is saturated.
    last = measured(seen, len(values) - 1)
    assert last[:4] == (vga.CLOCKS_MAX, 0, 0, 0)
    assert vga.classify(*last[:4], 25_175_000) is None


def test_slow_hsync_saturates_instead_of_wrapping() -> None:
    # A line of 5000 clocks does not fit in the 12-bit counter: it reads 4095, not 904.
    model = VgaModel(2000, 1000, 1000, 1000, 1, 0, 1, 0)  # 5000 x 2
    assert (model.h_total, model.v_total) == (5000, 2)
    t = vga.VgaTiming()
    clocks = 2 * model.clocks_per_frame + 100  # two vsync rises: the first publication
    seen = trace(t, model.stream(clocks), observe_timing(t))
    assert seen["frames"][-1] == 1
    clocks_per_line, lines_per_frame, hsync_width, vsync_lines, hp, vp = measured(seen, clocks - 1)
    assert clocks_per_line == vga.CLOCKS_MAX == 4095
    # The pulse (the shorter phase) is still measured, whole.
    assert (lines_per_frame, hsync_width, vsync_lines, hp, vp) == (2, 1000, 1, 0, 0)


def test_many_lines_saturate_instead_of_wrapping() -> None:
    # 2100 lines does not fit in the 11-bit counter: it reads 2047, not 52.
    model = VgaModel(1, 1, 1, 1, 2000, 50, 3, 47)
    assert model.v_total == 2100
    t = vga.VgaTiming()
    start = 2040 * model.h_total  # just before the vsync pulse: the second rise is soon
    clocks = model.clocks_per_frame + 400
    seen = trace(t, model.stream(clocks, skip=start), observe_timing(t))
    assert seen["frames"][-1] == 1
    last = measured(seen, clocks - 1)
    assert last[1] == vga.LINES_MAX == 2047
    assert last[3] == 3  # the short pulse is still measured
    assert last[5] == 0  # and found to be the short level: negative sync


# VgaCapture ----------------------------------------------------------------------------------


def design_colour(model, x, y) -> int:
    """The colour the design draws at design pixel (x, y); black outside its picture."""
    return model.picture(x, y) if y < model.v_active else 0


def row_shift(model) -> int:
    """Rows by which a capture at v_offset = vsync lines + back porch is below the design's.

    Valid only for a vsync edge at or before the hsync leading edge in the line ("line_start"
    and "hsync" phases): counter-based designs (the model's too) start a line before its hsync
    pulse, so the line that begins at an hsync edge is followed by the next design line's
    picture, and the capture, like vgacap and the reference frames, shows the design one line
    low. The exact shift depends on where the design puts its vsync edge: with it after the
    hsync edge ("after_hsync") it is 2 (`test_a_vsync_after_the_hsync_edge_shifts_two_lines`).
    """
    return 2 if model.vsync_phase == "after_hsync" else 1


def colour_of(value: int) -> int:
    """The 6-bit colour of a `uo_out` byte, decoded here from the Tiny VGA pinout."""
    r1, r0 = value >> 0 & 1, value >> 4 & 1
    g1, g0 = value >> 1 & 1, value >> 5 & 1
    b1, b0 = value >> 2 & 1, value >> 6 & 1
    return r1 << 5 | r0 << 4 | g1 << 3 | g0 << 2 | b1 << 1 | b0


def make_capture(
    width,
    height,
    model,
    h_offset,
    v_offset,
    divider=1,
    h_step=1,
    v_step=1,
    hsync_positive=None,
    vsync_positive=None,
):
    cap = vga.VgaCapture(width, height)
    cap.hsync_positive.reset = int(
        model.hsync_positive if hsync_positive is None else hsync_positive
    )
    cap.vsync_positive.reset = int(
        model.vsync_positive if vsync_positive is None else vsync_positive
    )
    cap.divider.reset = divider
    cap.h_offset.reset = h_offset
    cap.v_offset.reset = v_offset
    cap.h_step.reset = h_step
    cap.v_step.reset = v_step
    return cap


CAPTURE_OUTPUTS = ("we", "addr", "data", "frame_done")


def capture_run(cap, values, events=None):
    """Run `values`; return (writes, frame_done clocks); a write is (clock, addr, data)."""
    seen = trace(cap, values, {name: getattr(cap, name) for name in CAPTURE_OUTPUTS}, events)
    writes = [(c, seen["addr"][c], seen["data"][c]) for c in range(len(values)) if seen["we"][c]]
    done = [c for c in range(len(values)) if seen["frame_done"][c]]
    return writes, done


def expected_writes(
    model, values, width, height, h_offset, v_offset, divider=1, h_step=1, v_step=1
):
    """The writes a capture of `values` must make, worked out from the pins alone.

    Frame k starts at the k-th vsync leading edge. The first hsync leading edge at or after
    that clock starts line 0 (as vgacap counts); the picture's row y is line `v_offset + y *
    v_step`, and its pixel x is sampled `h_offset + x * h_step * divider + divider // 2` clocks
    after that line's hsync leading edge; the write comes one clock after that.
    """
    hedges = pin_active_edges(values, HSYNC_BIT, model.hsync_positive)
    vedges = pin_active_edges(values, VSYNC_BIT, model.vsync_positive)
    out = []
    for tv in vedges:
        after = [i for i, e in enumerate(hedges) if e >= tv]
        if not after:
            continue
        number1 = after[0]
        frame = []
        for y in range(height):
            index = number1 + v_offset + y * v_step
            if index >= len(hedges):
                break
            for x in range(width):
                sample = hedges[index] + h_offset + x * h_step * divider + divider // 2
                if sample + 1 >= len(values):
                    break
                frame.append((sample + 1, y * width + x, colour_of(values[sample])))
        if len(frame) == width * height:
            out.append(frame)
    return out


@pytest.mark.parametrize("hsync_positive", [False, True])
@pytest.mark.parametrize("vsync_positive", [False, True])
@pytest.mark.parametrize("vsync_phase", ["line_start", "hsync"])
def test_capture_writes_the_picture_at_the_offsets(
    hsync_positive, vsync_positive, vsync_phase
) -> None:
    model = small(
        hsync_positive=hsync_positive, vsync_positive=vsync_positive, vsync_phase=vsync_phase
    )
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(16, 8, model, h_offset=6 + 14, v_offset=2 + 8)
    writes, done = capture_run(cap, values)
    frames = expected_writes(model, values, 16, 8, 20, 10)
    assert len(frames) >= 2
    assert writes == [w for frame in frames for w in frame]
    # Row-major addressing, and the colour is the picture's at that point.
    for frame in frames:
        for _, addr, data in frame:
            x, y = addr % 16, addr // 16
            assert data == design_colour(model, x, y + row_shift(model))
    assert [w[1] for w in writes[:128]] == list(range(128))
    # `frame_done`: the clock after the last write of each frame, one clock wide.
    assert done == [frame[-1][0] + 1 for frame in frames]


def test_frame_done_is_one_clock_and_follows_the_last_write() -> None:
    model = small()
    values = list(model.stream(4 * model.clocks_per_frame))
    cap = make_capture(16, 8, model, 20, 10)
    seen = trace(cap, values, {n: getattr(cap, n) for n in CAPTURE_OUTPUTS})
    for c in range(1, len(values)):
        if seen["frame_done"][c]:
            assert not seen["frame_done"][c - 1]
            assert seen["we"][c - 1] and seen["addr"][c - 1] == 127
    assert sum(seen["frame_done"]) == 3


@pytest.mark.parametrize("divider", [2, 3, 4])
def test_a_divided_pixel_is_sampled_in_its_middle_clock(divider) -> None:
    # The clocks of a pixel other than clock divider // 2 carry a wrong colour.
    model = small(divider=divider, dirty=True)
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(16, 8, model, h_offset=20 * divider, v_offset=10, divider=divider)
    writes, _ = capture_run(cap, values)
    frames = expected_writes(model, values, 16, 8, 20 * divider, 10, divider)
    assert len(frames) >= 2
    assert writes == [w for frame in frames for w in frame]
    for _, addr, data in writes:
        assert data == design_colour(model, addr % 16, addr // 16 + row_shift(model))


def test_divider_two_samples_the_second_clock() -> None:
    model = small(divider=2, dirty=True)
    # Pixel (0, 0) is clocks 0 and 1 of its pixel; the colour is on the second only.
    assert model.pixel(0, 0, 0) != model.pixel(0, 0, 1)
    assert vga.unpack(colour_of(model.pixel(0, 0, 1))) == vga.unpack(model.picture(0, 0))
    values = list(model.stream(2 * model.clocks_per_frame))
    cap = make_capture(16, 8, model, h_offset=40, v_offset=10, divider=2)
    writes, _ = capture_run(cap, values)
    hedges = pin_active_edges(values, HSYNC_BIT, False)
    vedge = pin_active_edges(values, VSYNC_BIT, False)[0]
    number1 = next(i for i, e in enumerate(hedges) if e >= vedge)
    # The first write: line 10 (its hsync edge is 10 edges after line 0's), 40 clocks on, the
    # second clock of the pixel, and one clock for the registered write.
    assert writes[0][0] == hedges[number1 + 10] + 40 + 1 + 1


def test_quarter_size_capture_decimates() -> None:
    model = small()
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(8, 4, model, 20, 10, h_step=2, v_step=2)
    writes, done = capture_run(cap, values)
    frames = expected_writes(model, values, 8, 4, 20, 10, h_step=2, v_step=2)
    assert len(frames) >= 2
    assert writes == [w for frame in frames for w in frame]
    for _, addr, data in writes:
        x, y = addr % 8, addr // 8
        assert data == design_colour(model, 2 * x, 2 * y + row_shift(model))  # first of two
    assert len(done) == len(frames)


def test_quarter_size_capture_of_a_divided_design() -> None:
    model = small(divider=2, dirty=True)
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(8, 4, model, 40, 10, divider=2, h_step=2, v_step=2)
    writes, _ = capture_run(cap, values)
    frames = expected_writes(model, values, 8, 4, 40, 10, divider=2, h_step=2, v_step=2)
    assert len(frames) >= 2
    assert writes == [w for frame in frames for w in frame]
    assert all(d == design_colour(model, 2 * (a % 8), 2 * (a // 8) + 1) for _, a, d in writes)


def test_larger_mode_is_cropped_top_left() -> None:
    model = small()  # 16 x 8 picture into a 12 x 6 buffer
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(12, 6, model, 20, 10)
    writes, done = capture_run(cap, values)
    frames = expected_writes(model, values, 12, 6, 20, 10)
    assert len(frames) >= 2
    assert writes == [w for frame in frames for w in frame]
    assert all(a < 72 for _, a, _ in writes)  # nothing written beyond the buffer
    assert all(d == design_colour(model, a % 12, a // 12 + 1) for _, a, d in writes)
    assert len(writes) == 72 * len(frames)
    assert len(done) == len(frames)


def test_settings_of_zero_mean_one() -> None:
    model = small()
    values = list(model.stream(2 * model.clocks_per_frame))
    ones = capture_run(make_capture(16, 8, model, 1, 1, 1, 1, 1), values)
    zeros = capture_run(make_capture(16, 8, model, 0, 0, 0, 0, 0), values)
    assert ones == zeros
    assert ones[0]  # and it wrote something


def test_capture_ignores_a_polarity_that_is_wrong() -> None:
    # With the polarity inputs wrong the capture still makes only legal writes (no crash, in
    # range), though not the picture: software must set them from VgaTiming.
    model = small()
    values = list(model.stream(3 * model.clocks_per_frame))
    cap = make_capture(16, 8, model, 20, 10, hsync_positive=True, vsync_positive=True)
    writes, _ = capture_run(cap, values)
    assert all(0 <= a < 128 for _, a, _ in writes)


def _snapshots(writes, done):
    """The memory (dict addr -> data) at each frame_done clock."""
    memory: dict[int, int] = {}
    out = []
    pending = iter(writes)
    nxt = next(pending, None)
    for stop in done:
        while nxt is not None and nxt[0] <= stop:
            memory[nxt[1]] = nxt[2]
            nxt = next(pending, None)
        out.append(dict(memory))
    return out


def test_blank_writes_zeros_for_the_frames_that_start_while_it_is_asserted() -> None:
    model = small()
    n = model.clocks_per_frame
    values = list(model.stream(7 * n))
    vedges = pin_active_edges(values, VSYNC_BIT, False)
    cap = make_capture(16, 8, model, 20, 10)
    # Blank goes up just after frame 1's picture is complete, and comes down in the middle of
    # frame 3, after that frame's vsync edge: frame 3 is still all zeros. The clear sweep
    # (128 clocks) is still running at frame 2's vsync edge, so frame 2 is held off (nothing
    # of it is written, and it has no frame_done); frame 3 is the first frame written.
    frames = expected_writes(model, values, 16, 8, 20, 10)
    up = frames[1][-1][0] + 5  # just after frame 1's last write
    assert up + 128 > vedges[2] > up
    down = vedges[3] + 12
    events = {up: [cap.blank.eq(1)], down: [cap.blank.eq(0)]}
    writes, done = capture_run(cap, values, events)
    # (frames[k] is the frame that starts at vedges[k])
    snaps = _snapshots(writes, done)
    assert len(snaps) == 5  # frames 0, 1, 3, 4, 5
    picture = {a: d for _, a, d in frames[1]}
    assert all(len(s) == 128 for s in snaps)
    assert snaps[1] == picture  # frame 1, before blank
    assert all(v == 0 for v in snaps[2].values())  # frame 3: blank was up at its boundary
    assert snaps[3] == {a: d for _, a, d in frames[4]}  # frame 4 is the picture again
    expected = [(c, a, 0) for c, a, _ in frames[3]]
    assert [w for w in writes if frames[3][0][0] <= w[0] <= frames[3][-1][0]] == expected
    assert not [
        w for w in writes if frames[2][0][0] <= w[0] <= frames[2][-1][0] and w[0] > up + 128
    ]


def test_blank_clears_the_buffer_when_there_is_no_signal() -> None:
    model = small()
    cap = make_capture(16, 8, model, 20, 10)
    values = [0x00] * 400  # nothing on the pins at all
    writes, done = capture_run(cap, values, {100: [cap.blank.eq(1)]})
    # The buffer is cleared at once, one address a clock from address 0, in zeros...
    assert writes == [(101 + i, i, 0) for i in range(128)]
    assert done == []  # ...which is not a frame
    # and only once while blank stays up
    assert len(writes) == 128


def test_blank_released_and_raised_again_clears_again() -> None:
    model = small()
    cap = make_capture(16, 8, model, 20, 10)
    values = [0x00] * 700
    events = {50: [cap.blank.eq(1)], 300: [cap.blank.eq(0)], 400: [cap.blank.eq(1)]}
    writes, _ = capture_run(cap, values, events)
    assert [w[0] for w in writes if w[0] < 300] == list(range(51, 51 + 128))
    assert [w[0] for w in writes if w[0] > 300] == list(range(401, 401 + 128))


def test_write_port_is_registered() -> None:
    from migen import Instance

    class Walk(Module):
        def __init__(self) -> None:
            self.cap = vga.VgaCapture(16, 8)
            self.submodules += self.cap
            self.specials += Instance(
                "thing",
                name="project",
                i_we=self.cap.we,
                i_addr=self.cap.addr,
                i_data=self.cap.data,
                i_done=self.cap.frame_done,
            )

    found = inputs.project_inputs(Walk(), "project")
    assert set(found) == {"we", "addr", "data", "done"}
    for port, source in found.items():
        assert source.kind == "register", f"{port}: {source.kind}: {source.detail}"


# VgaTiming and VgaCapture together -----------------------------------------------------------


class Bench(Module):
    """Timing and capture on one `uo_out`; software's part is played by `blank = no_signal`."""

    def __init__(self, width, height, no_signal_clocks, settings) -> None:
        self.uo_out = Signal(8)
        self.timing = vga.VgaTiming(no_signal_clocks)
        self.capture = vga.VgaCapture(width, height)
        self.submodules += self.timing, self.capture
        self.comb += [
            self.timing.uo_out.eq(self.uo_out),
            self.capture.uo_out.eq(self.uo_out),
            self.capture.hsync_positive.eq(self.timing.hsync_positive),
            self.capture.vsync_positive.eq(self.timing.vsync_positive),
            self.capture.blank.eq(self.timing.no_signal),
        ]
        h_offset, v_offset = settings
        self.capture.h_offset.reset = h_offset
        self.capture.v_offset.reset = v_offset
        self.capture.divider.reset = 1


def test_no_sync_reports_no_signal_and_blanks() -> None:
    model = small(hsync_positive=True)  # polarity comes from the timing block here
    live = list(model.stream(5 * model.clocks_per_frame))
    values = live + [0x00] * 3000  # then the design stops driving anything
    bench = Bench(16, 8, 1000, (20, 10))
    seen = trace(
        bench,
        values,
        {
            "no_signal": bench.timing.no_signal,
            "we": bench.capture.we,
            "addr": bench.capture.addr,
            "data": bench.capture.data,
            "done": bench.capture.frame_done,
        },
    )
    writes = [(c, seen["addr"][c], seen["data"][c]) for c in range(len(values)) if seen["we"][c]]
    done = [c for c in range(len(values)) if seen["done"][c]]
    snaps = _snapshots(writes, done)
    # From reset there is no signal, so the screen is black until the picture is measured.
    assert seen["no_signal"][0] == 1
    assert all(len(s) == 128 for s in snaps)
    assert all(v == 0 for v in snaps[0].values())
    # Once measured, the picture appears (no_signal is low from the second vsync edge, so
    # the capture's frame boundary is the third)...
    picture = {y * 16 + x: design_colour(model, x, y + 1) for y in range(8) for x in range(16)}
    assert snaps[-1] == picture
    # ... and when the sync stops no_signal comes up and the buffer is cleared, not frozen.
    assert seen["no_signal"][len(live) + 2999] == 1
    last_write = writes[-1][0]
    assert last_write > len(live)  # the clear came after the picture
    memory: dict[int, int] = {}
    for c, a, d in writes:
        memory[a] = d
    assert len(memory) == 128
    assert all(v == 0 for v in memory.values())


# Framebuffer ---------------------------------------------------------------------------------


class FramebufferBench(Module):
    """A framebuffer with its two clock domains defined, as a top level would."""

    def __init__(self, width, height) -> None:
        self.clock_domains.cd_project = ClockDomain("project")
        self.clock_domains.cd_video = ClockDomain("video")
        self.submodules.fb = vga.Framebuffer(width, height, "video")


def test_framebuffer_sizes() -> None:
    for width, height, bits in ((640, 480, 1_843_200), (320, 240, 460_800)):
        fb = vga.Framebuffer(width, height, "video")
        assert fb.mem.depth == width * height
        assert fb.mem.width == 6
        assert fb.mem.depth * fb.mem.width == bits
        assert len(fb.data) == 6
        assert len(fb.rd_data) == 6
        assert 2 ** len(fb.addr) >= width * height > 2 ** (len(fb.addr) - 1)
        assert len(fb.rd_addr) == len(fb.addr)


def test_framebuffer_written_in_one_clock_read_in_another() -> None:
    bench = FramebufferBench(8, 4)
    fb = bench.fb
    value = {a: (a * 5 + 3) & 0x3F for a in range(32)}
    written = []
    got = {}

    def write():
        for a in range(32):
            yield fb.addr.eq(a)
            yield fb.data.eq(value[a])
            yield fb.we.eq(1)
            yield
        yield fb.we.eq(0)
        yield
        written.append(True)

    def read():
        while not written:
            yield
        # Synchronous read: the address presented in one video clock gives its data after
        # the next edge, i.e. in the following iteration; one address per clock, pipelined.
        previous = None
        for a in [*range(32), 0]:
            yield fb.rd_addr.eq(a)
            yield
            if previous is not None:
                got[previous] = yield fb.rd_data
            previous = a

    run_simulation(bench, {"project": write(), "video": read()}, clocks={"project": 10, "video": 7})
    assert got == value


def test_framebuffer_read_returns_old_or_new_while_it_is_being_written() -> None:
    # No handshake: a reader may see a frame being overwritten (tearing). It sees the old
    # or the new pixel, whole.
    bench = FramebufferBench(8, 4)
    fb = bench.fb
    seen = set()
    stop = []

    def write():
        for i in range(60):
            yield fb.addr.eq(5)
            yield fb.data.eq(0x15 if i % 2 else 0x2A)
            yield fb.we.eq(1)
            yield
        stop.append(True)

    def read():
        yield fb.rd_addr.eq(5)
        while not stop:
            yield
            seen.add((yield fb.rd_data))

    run_simulation(bench, {"project": write(), "video": read()}, clocks={"project": 10, "video": 7})
    assert seen <= {0, 0x15, 0x2A}
    assert {0x15, 0x2A} <= seen


def _verilog(width, height) -> str:
    bench = FramebufferBench(width, height)
    ios = {
        bench.cd_project.clk,
        bench.cd_video.clk,
        bench.fb.we,
        bench.fb.addr,
        bench.fb.data,
        bench.fb.rd_addr,
        bench.fb.rd_data,
    }
    return str(verilog.convert(bench, ios=ios, name="fb"))


def test_framebuffer_read_data_is_a_register_in_the_read_domain() -> None:
    text = _verilog(8, 4)
    # Writes in the project clock; in the video clock the data register is loaded from the
    # array at the address input, and that register is the read data. No asynchronous read.
    assert re.search(
        r"always @\(posedge project_clk\) begin : \w+\s+if \(we\)\s+mem\[adr\] <= dat_w;", text
    )
    video = re.search(
        r"always @\(posedge video_clk\) begin : \w+\s+(\w+) <= mem\[(\w+)\];\s+end", text
    )
    assert video is not None, text
    assert re.search(rf"assign dat_r = {video.group(1)};", text)
    assert not re.search(r"assign dat_r = mem\[", text)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("yosys") is None, reason="yosys not installed")
@pytest.mark.parametrize(("width", "height", "ramb36_at_most"), [(320, 240, 16), (640, 480, 60)])
def test_framebuffer_infers_block_ram(tmp_path, width, height, ramb36_at_most) -> None:
    top = tmp_path / "fb.v"
    top.write_text(_verilog(width, height))
    log = tmp_path / "yosys.log"
    script = "synth_xilinx -family xc7 -top fb; stat"
    rc = verilate.run(
        [str(tools.find("yosys")), "-q", "-l", str(log), "-p", f"read_verilog {top}; {script}"],
        tmp_path / "run.log",
        tmp_path,
        600,
    )
    assert rc == 0, (tmp_path / "run.log").read_text()
    text = log.read_text()
    cells = {}
    for line in text.splitlines():
        m = re.match(r"\s+(\d+)\s+(\S+)\s*$", line)
        if m:
            cells[m.group(2)] = int(m.group(1))
    ramb = cells.get("RAMB36E1", 0) + cells.get("RAMB18E1", 0)
    assert ramb > 0, f"no block RAM in the netlist:\n{text}"
    assert cells.get("RAMB36E1", 0) <= ramb36_at_most + cells.get("RAMB18E1", 0)
    assert not any(k.startswith(("RAM64", "RAM32", "RAM128", "RAM256")) for k in cells), cells
    assert cells.get("FDRE", 0) + cells.get("FDCE", 0) + cells.get("FDPE", 0) < 100, cells


# Verilator: the blocks on real-size 640 x 480 video ------------------------------------------

HARNESS = REPO / "tt_harness" / "sim" / "vga_tb.cpp"
POOL_ID = "tt07/tt_um_tinytapeout_dvd_screensaver"
FB_WIDTH, FB_HEIGHT = 640, 480


class _Top(Module):
    """VgaTiming + VgaCapture as the harness wires them; software's part is in the C++ and here.

    `blank` is `no_signal`, the polarities come from the timing block, and the settings are
    inputs the harness sets (it classifies the measurement first, as software would).
    """

    def __init__(self) -> None:
        self.clock_domains.cd_sys = ClockDomain()
        self.uo_out = Signal(8)
        self.divider = Signal(3)
        self.h_offset = Signal(12)
        self.v_offset = Signal(11)
        self.h_step = Signal(2)
        self.v_step = Signal(2)
        self.clocks_per_line = Signal(12)
        self.lines_per_frame = Signal(11)
        self.hsync_width = Signal(12)
        self.vsync_lines = Signal(11)
        self.hsync_positive = Signal()
        self.vsync_positive = Signal()
        self.frames = Signal(16)
        self.locked = Signal()
        self.no_signal = Signal()
        self.we = Signal()
        self.addr = Signal(max=FB_WIDTH * FB_HEIGHT)
        self.data = Signal(6)
        self.frame_done = Signal()

        timing = vga.VgaTiming()
        capture = vga.VgaCapture(FB_WIDTH, FB_HEIGHT)
        self.submodules += timing, capture
        self.comb += [
            timing.uo_out.eq(self.uo_out),
            capture.uo_out.eq(self.uo_out),
            capture.hsync_positive.eq(timing.hsync_positive),
            capture.vsync_positive.eq(timing.vsync_positive),
            capture.blank.eq(timing.no_signal),
            capture.divider.eq(self.divider),
            capture.h_offset.eq(self.h_offset),
            capture.v_offset.eq(self.v_offset),
            capture.h_step.eq(self.h_step),
            capture.v_step.eq(self.v_step),
            self.clocks_per_line.eq(timing.clocks_per_line),
            self.lines_per_frame.eq(timing.lines_per_frame),
            self.hsync_width.eq(timing.hsync_width),
            self.vsync_lines.eq(timing.vsync_lines),
            self.hsync_positive.eq(timing.hsync_positive),
            self.vsync_positive.eq(timing.vsync_positive),
            self.frames.eq(timing.frames),
            self.locked.eq(timing.locked),
            self.no_signal.eq(timing.no_signal),
            self.we.eq(capture.we),
            self.addr.eq(capture.addr),
            self.data.eq(capture.data),
            self.frame_done.eq(capture.frame_done),
        ]


@pytest.fixture(scope="module")
def vga_bench(tmp_path_factory) -> Path:
    """The Verilated VgaTiming + VgaCapture (640 x 480 buffer) with the replay harness."""
    if tools.find("verilator") is None:
        pytest.skip("verilator not installed")
    work = tmp_path_factory.mktemp("vga-bench")
    top = _Top()
    ios = {
        top.cd_sys.clk,
        top.uo_out,
        top.divider,
        top.h_offset,
        top.v_offset,
        top.h_step,
        top.v_step,
        top.clocks_per_line,
        top.lines_per_frame,
        top.hsync_width,
        top.vsync_lines,
        top.hsync_positive,
        top.vsync_positive,
        top.frames,
        top.locked,
        top.no_signal,
        top.we,
        top.addr,
        top.data,
        top.frame_done,
    }
    top_v = work / "top.v"
    verilog.convert(top, ios=ios, name="top").write(str(top_v))
    log = work / "verilator.log"
    obj = work / "obj"
    cmd = [
        str(tools.find("verilator")),
        *verilate.BASE_FLAGS,
        "-j",
        "8",
        "--top-module",
        "top",
        "-Mdir",
        str(obj),
        str(HARNESS),
        str(top_v),
    ]
    rc = verilate.run(cmd, log, work, verilate.BUILD_TIMEOUT_S)
    assert rc == 0, f"verilator failed, see {log}"
    return obj / "Vtop"


def run_bench(bench: Path, dump: Path, clocks: int, settings=None, snapshots: Path | None = None):
    """Replay `clocks` bytes of `dump`; return the printed numbers as a dict of ints."""
    cmd = [str(bench), "--dump", str(dump), "--clocks", str(clocks)]
    if settings is not None:
        cmd += ["--settings", *map(str, settings)]
    cmd += ["--size", str(FB_WIDTH), str(FB_HEIGHT)]  # also makes the harness check addresses
    if snapshots is not None:
        cmd += ["--out", str(snapshots)]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    assert done.returncode == 0, done.stderr
    assert "bad_writes=0" in done.stdout
    return {k: int(v) for k, v in (item.split("=") for item in done.stdout.split())}


def read_snapshots(path: Path) -> list[tuple[int, bytes]]:
    """The (clock, pixels) of every frame_done in a snapshot file."""
    data = path.read_bytes()
    size = FB_WIDTH * FB_HEIGHT
    out, pos = [], 0
    while pos < len(data):
        out.append((int.from_bytes(data[pos : pos + 8], "little"), data[pos + 8 : pos + 8 + size]))
        pos += 8 + size
    return out


@pytest.mark.toolchain
def test_measures_640x480_at_25_175(vga_bench, tmp_path) -> None:
    model = VgaModel(640, 16, 96, 48, 480, 10, 2, 33)  # negative sync, as the real mode
    assert model.clocks_per_frame == 420_000
    clocks = 4 * model.clocks_per_frame + 1000
    dump = tmp_path / "model.bin"
    dump.write_bytes(bytes(model.stream(clocks)))
    seen = run_bench(vga_bench, dump, clocks)
    # Negative vsync: the pin rises at the end of the pulse, 393,600 clocks in (line 492),
    # and every 420,000 after that. A measurement is published the clock after the second
    # rise, and the third rise locks.
    assert seen["first_publish"] == 393_600 + 420_000 + 1
    assert seen["first_locked"] == 393_600 + 2 * 420_000 + 1
    assert (
        seen["clocks_per_line"],
        seen["lines_per_frame"],
        seen["hsync_width"],
        seen["vsync_lines"],
        seen["hsync_positive"],
        seen["vsync_positive"],
    ) == (800, 525, 96, 2, 0, 0)
    assert seen["frames"] == 3  # the rises at 1.65M give a third publication
    assert seen["locked"] == 1
    assert seen["no_signal"] == 0
    mode = vga.classify(800, 525, 96, 2, 25_175_000)
    assert mode is not None
    assert (mode.divider, mode.h_offset, mode.v_offset) == (1, 144, 35)


@pytest.mark.toolchain
def test_capture_of_the_model_at_640x480(vga_bench, tmp_path) -> None:
    model = VgaModel(640, 16, 96, 48, 480, 10, 2, 33)
    clocks = 5 * model.clocks_per_frame
    dump = tmp_path / "model.bin"
    dump.write_bytes(bytes(model.stream(clocks)))
    snaps = tmp_path / "snaps.bin"
    seen = run_bench(vga_bench, dump, clocks, (1, 144, 35, 1, 1), snaps)
    frames = read_snapshots(snaps)
    assert seen["frames_done"] == len(frames) == 4  # the fifth frame is not complete
    picture = bytes(design_colour(model, x, y + 1) for y in range(480) for x in range(640))
    # Until the timing is measured `blank` holds the screen black (frames 0 and 1); then the
    # frames are the picture exactly.
    assert [f[1] == bytes(len(picture)) for f in frames] == [True, True, False, False]
    assert frames[2][1] == frames[3][1] == picture


# A pool design -------------------------------------------------------------------------------

# The standard numbers of 640 x 480 @ 60 Hz, used to cut a frame out of the dump (independent
# of tt_harness.core.vga): sync and back porch, in clocks and lines.
STD_H_SYNC, STD_H_BACK, STD_V_SYNC, STD_V_BACK = 96, 48, 2, 33
# The pin's colour as a 6-bit number, by the same bit assignment as `colour_of`.
COLOUR_TABLE = bytes(colour_of(v) for v in range(256))


def cut_frames(samples: bytes, hsync_positive: bool, vsync_positive: bool) -> list[dict]:
    """Cut every complete 640 x 480 frame out of a per-clock dump, from the sync pins alone.

    Independent of the gateware: with the sync pins' levels and the mode's standard numbers
    (sync 96 + back porch 48 clocks, sync 2 + back porch 33 lines), frame i starts at the
    i-th vsync leading edge. The first hsync leading edge at or after it starts line 0; row
    y is line 35 + y, and its pixel x is the byte 144 + x clocks after that line's hsync
    leading edge. Returns `{"pixels": 307200 colour bytes, "last": clock of the last pixel}`.
    """
    level = (b"\x01" if hsync_positive else b"\x00", b"\x01" if vsync_positive else b"\x00")
    hs = samples.translate(bytes((v >> 7) & 1 for v in range(256)))
    vs = samples.translate(bytes((v >> 3) & 1 for v in range(256)))
    lead_h = [
        m.start() + 1 for m in re.finditer(b"\x00\x01" if level[0] == b"\x01" else b"\x01\x00", hs)
    ]
    lead_v = [
        m.start() + 1 for m in re.finditer(b"\x00\x01" if level[1] == b"\x01" else b"\x01\x00", vs)
    ]
    first_pixel = STD_H_SYNC + STD_H_BACK
    first_line = STD_V_SYNC + STD_V_BACK
    out = []
    for tv in lead_v:
        number1 = next((i for i, e in enumerate(lead_h) if e >= tv), None)
        if number1 is None or number1 + first_line + 479 >= len(lead_h):
            continue
        rows = []
        for y in range(480):
            e = lead_h[number1 + first_line + y]
            rows.append(samples[e + first_pixel : e + first_pixel + 640].translate(COLOUR_TABLE))
        last = lead_h[number1 + first_line + 479] + first_pixel + 639
        out.append({"pixels": b"".join(rows), "last": last})
    return out


def read_png_rgb(path: Path) -> tuple[int, int, bytes]:
    """(width, height, RGB bytes) of an 8-bit RGB PNG (all five scanline filters)."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big")
            assert body[8:10] == bytes([8, 2]), "only 8-bit RGB is read"
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3
    rows: list[bytearray] = []
    for y in range(height):
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        ftype = raw[y * (stride + 1)]
        prev = rows[-1] if rows else bytearray(stride)
        for i in range(stride):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        rows.append(line)
    return width, height, b"".join(bytes(r) for r in rows)


def rgb_of(pixels: bytes) -> bytes:
    """RGB bytes (0, 85, 170, 255 per 2-bit channel) of 6-bit pixels."""
    out = bytearray()
    for p in pixels:
        r, g, b = vga.unpack(p)
        out += bytes((r * 85, g * 85, b * 85))
    return bytes(out)


VGACAP_FRAMES = Path("~/github/TinyTapeout/vgacap/build/vgacap-frames").expanduser()


def read_ppm_rgb(path: Path) -> tuple[int, int, bytes]:
    """(width, height, RGB bytes) of a binary PPM (P6, 8 bits)."""
    data = path.read_bytes()
    fields = data.split(maxsplit=4)
    assert fields[0] == b"P6" and fields[3] == b"255"
    width, height = int(fields[1]), int(fields[2])
    return width, height, data[-width * height * 3 :]


def pool_dump(clocks: int) -> tuple[bytes, str]:
    """The pool design's `uo_out` per clock: the shared reference dump, or one made here."""

    ref_dir = verdict.default_reference_dir()
    if verdict.reference_path(ref_dir, POOL_ID).is_file():
        ref = verdict.load_reference(ref_dir, POOL_ID)
        assert ref.clocks >= clocks
        return ref.samples[:clocks], f"shared reference {ref.path}"
    sources = pool.load_sources(POOL_ID)
    out = REPO / "tmp" / "vga-pool-reference"
    return reference.simulate(sources, clocks, out).read_bytes(), "reference.simulate"


@pytest.mark.toolchain
def test_pool_design_frame_matches_reference(vga_bench, tmp_path) -> None:
    clocks = 2_600_000  # six frames of 420,000 clocks
    samples, origin = pool_dump(clocks)
    assert len(samples) == clocks
    dump = tmp_path / "uo_out.bin"
    dump.write_bytes(samples)

    # 1. The design's timing, measured by VgaTiming in simulation, classified.
    measure = run_bench(vga_bench, dump, clocks)
    timing = (
        measure["clocks_per_line"],
        measure["lines_per_frame"],
        measure["hsync_width"],
        measure["vsync_lines"],
    )
    assert timing == (800, 525, 96, 2)
    assert (measure["hsync_positive"], measure["vsync_positive"]) == (1, 1)  # this design's
    assert measure["locked"] == 1
    assert measure["no_signal"] == 0
    mode = vga.classify(*timing, 25_175_000)
    assert mode is not None
    assert (mode.divider, mode.h_offset, mode.v_offset, mode.width, mode.height) == (
        1,
        144,
        35,
        640,
        480,
    )

    # 2. The capture with those settings.
    snaps = tmp_path / "snaps.bin"
    settings = (mode.divider, mode.h_offset, mode.v_offset, 1, 1)
    seen = run_bench(vga_bench, dump, clocks, settings, snaps)
    captured = read_snapshots(snaps)
    assert seen["frames_done"] == len(captured)

    # 3. The same frames cut from the dump by an independent few lines of Python.
    cut = cut_frames(samples, hsync_positive=True, vsync_positive=True)
    assert len(cut) >= 5
    compared = different = 0
    matched = []
    for index, frame in enumerate(cut):
        if index < 2:
            continue  # frames 0 and 1 are from before the polarity was known
        snap = [pixels for clock, pixels in captured if clock == frame["last"] + 2]
        assert len(snap) == 1, f"no frame_done two clocks after the last pixel of frame {index}"
        pixels = snap[0]
        compared += len(pixels)
        different += sum(a != b for a, b in zip(pixels, frame["pixels"], strict=True))
        matched.append(index)
    assert matched == list(range(2, len(cut)))
    assert compared >= 3 * 640 * 480
    assert different == 0, f"{different} of {compared} pixels differ ({origin})"
    # The pictures are real: not black, and the logo moves between frames.
    pictures = [frame["pixels"] for frame in cut[2:]]
    assert all(any(p) for p in pictures)
    assert len(set(pictures)) == len(pictures)

    # 4. The same frames as the vgacap frame reconstruction (C, in the capture project this
    # work shares its stream format with) cuts them, if its tool is built here.
    vgacap_frames = VGACAP_FRAMES
    if vgacap_frames.is_file():
        stream = tmp_path / "pool.vgacap"
        write_vgacap(Recording(len(samples), samples, True, None), stream, 25_175_000, POOL_ID)
        prefix = tmp_path / "vgf"
        done = subprocess.run(
            [str(vgacap_frames), str(stream), str(prefix)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        for index in range(2, len(cut)):
            width, height, rgb = read_ppm_rgb(tmp_path / f"vgf-{index:04d}.ppm")
            assert (width, height) == (640, 480)
            assert rgb == rgb_of(cut[index]["pixels"]), f"vgacap frame {index} differs"

    # 5. And the reference's own frame 2 (cut by the video pipeline that made the reference,
    # saved as a PNG next to it) is one of those frames, if the PNG is there.
    png = verdict.default_reference_dir() / POOL_ID / "frame-002.png"
    if png.is_file():
        width, height, rgb = read_png_rgb(png)
        assert (width, height) == (640, 480)
        hits = [i for i, f in enumerate(cut) if rgb_of(f["pixels"]) == rgb]
        assert len(hits) == 1, f"the reference's frame 2 matches frames {hits}"


# Writes stay inside the buffer ---------------------------------------------------------------

# A small mode with a buffer size that is not a power of two (10 x 7 = 70): 20 x 12 clocks,
# picture 10 x 7, so a frame is 240 clocks.
TINY = {
    "h_active": 10,
    "h_front": 2,
    "h_sync": 3,
    "h_back": 5,
    "v_active": 7,
    "v_front": 1,
    "v_sync": 2,
    "v_back": 2,
}


def test_no_write_is_ever_outside_the_buffer_whatever_the_blank_pulse_start() -> None:
    """A blank pulse at every start time of a running picture; never an address >= w * h."""
    model = VgaModel(**TINY)
    assert model.clocks_per_frame == 240
    cap = make_capture(10, 7, model, h_offset=3 + 5, v_offset=2 + 2)
    spacing = 73  # a whole sweep (70 clocks) and its release apart, coprime to the frame
    pulses = 240
    values = list(model.stream(pulses * spacing + 200))
    events = {}
    for k in range(pulses):
        events[40 + k * spacing] = [cap.blank.eq(1)]
        events[41 + k * spacing] = [cap.blank.eq(0)]
    writes, _ = capture_run(cap, values, events)
    assert len(writes) >= 70 * pulses  # all the sweeps ran
    bad = [w for w in writes if w[1] >= 70]
    pulses_hit = {(w[0] - 40) // spacing for w in bad}
    assert not bad, (
        f"{len(bad)} writes outside the buffer after {len(pulses_hit)} of {pulses} blank-pulse "
        f"start times, first {bad[0]}"
    )
    starts = {(40 + k * spacing) % 240 for k in range(pulses)}
    assert len(starts) == 240  # every start time of the frame was tried


def test_after_a_sweep_the_first_frame_is_clean_and_whole() -> None:
    model = VgaModel(**TINY)
    values = list(model.stream(12 * model.clocks_per_frame))
    cap = make_capture(10, 7, model, 8, 4)
    vedges = pin_active_edges(values, VSYNC_BIT, False)
    # a one-clock blank pulse in the middle of the picture of frame 2
    start = vedges[2] + 6 * 20 + 14
    events = {start: [cap.blank.eq(1)], start + 1: [cap.blank.eq(0)]}
    writes, done = capture_run(cap, values, events)
    frames = expected_writes(model, values, 10, 7, 8, 4)
    # Picture writes are suppressed from the sweep until the next vsync edge: frame 2 is
    # never completed (no frame_done for it), frame 3 onwards are whole pictures.
    assert len(done) == len(frames) - 1
    snaps = _snapshots(writes, done)
    assert snaps[2] == {a: d for _, a, d in frames[3]}
    assert snaps[3] == {a: d for _, a, d in frames[4]}
    assert all(w[1] < 70 for w in writes)


def test_normal_capture_never_writes_outside_the_buffer() -> None:
    model = VgaModel(**TINY)
    values = list(model.stream(6 * model.clocks_per_frame))
    for width, height, h_step, v_step, h_offset, v_offset in (
        (10, 7, 1, 1, 8, 4),
        (9, 5, 1, 1, 8, 4),
        (5, 3, 2, 2, 8, 4),
        (10, 7, 1, 1, 12, 9),  # offsets that run off the end of the line and the frame
        (7, 7, 3, 2, 8, 4),
    ):
        cap = make_capture(width, height, model, h_offset, v_offset, h_step=h_step, v_step=v_step)
        writes, _ = capture_run(cap, values)
        assert writes
        assert all(0 <= a < width * height for _, a, _ in writes), (width, height, h_step)


def test_a_vsync_after_the_hsync_edge_shifts_two_lines() -> None:
    # The relation between v_offset and the design's rows is design-dependent: here vsync
    # changes at the end of the hsync pulse, after its leading edge, and the capture is two
    # design lines low (one for a vsync before or on the edge).
    for phase, shift in (("line_start", 1), ("hsync", 1), ("after_hsync", 2)):
        model = small(vsync_phase=phase)
        assert row_shift(model) == shift
        values = list(model.stream(3 * model.clocks_per_frame))
        cap = make_capture(16, 8, model, 20, 10)
        writes, _ = capture_run(cap, values)
        assert writes
        for _, addr, data in writes:
            assert data == design_colour(model, addr % 16, addr // 16 + shift), phase


def test_a_one_clock_sync_glitch_drops_lock_for_two_frames() -> None:
    model = small()
    n = model.clocks_per_frame
    values = list(model.stream(9 * n))
    # One clock of hsync going active in the middle of the picture area of line 3 of frame 4
    # (hsync is negative: high, the glitch is a 1-clock low).
    glitch = 4 * n + 3 * 40 + 5
    assert values[glitch] >> HSYNC_BIT & 1
    values[glitch] &= ~(1 << HSYNC_BIT)
    t = vga.VgaTiming()
    seen = trace(t, values, observe_timing(t))
    pubs = [c for c in range(1, len(values)) if seen["frames"][c] != seen["frames"][c - 1]]
    # Every measurement changes only on a publication, glitch or not.
    for c in range(1, len(values)):
        if measured(seen, c) != measured(seen, c - 1):
            assert c in pubs
    after = next(i for i, c in enumerate(pubs) if c > glitch)
    locked = [seen["locked"][c] for c in pubs]
    lines = [seen["lines_per_frame"][c] for c in pubs]
    assert locked[0] == 0  # the first measurement is alone
    assert lines[after] == 21 and lines[after + 1] == 20  # the extra edge, in one frame only
    assert locked[after - 1] == 1  # locked before
    assert locked[after] == 0  # the corrupted frame
    assert locked[after + 1] == 0  # and the one after: it differs from the corrupted one
    assert all(locked[after + 2 :])  # then locked again
