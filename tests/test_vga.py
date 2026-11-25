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
from vga_model import VgaModel, encode

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
