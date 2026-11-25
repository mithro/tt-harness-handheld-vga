# SPDX-License-Identifier: Apache-2.0
"""Scenarios for the pin-sequence checker (`tests/pin_check.py`): one clean sequence per start."""

from __future__ import annotations

import zlib

import pytest
from pin_check import NORMAL, REF_UI, SW_UI, assert_clean
from test_atspeed import toy_bytes, waits
from test_atspeed_abort import (
    CLOCKS,
    Recorder,
    go2,
    make,
    pulse,
    setup,
    start,
    wait_done,
)

CLOCKS = CLOCKS | {"mid": (400_000, 5_050)}  # 2.5 MHz: a project clock slower than `sys`


def per_clock(cfg) -> int:
    """System clocks per project clock (at least 1)."""
    return max(1, cfg[0] // 20_000)


def run_scenario(clock, k: int, n_first: int = 30, n_second: int = 25, tail: int = 200):
    """start N1, abort after k project clocks, start N2 on the very next system clock."""
    b = make()
    rec, out = Recorder(b), {}
    per = per_clock(CLOCKS[clock])

    def sysgen():
        yield from setup(b)
        yield from waits(8 * per)  # the pins reach their normal values first
        yield from start(b, n_first)
        yield from waits(k * per)  # k project clocks (system clocks at the normal clock)
        yield from pulse(b.check.abort)
        yield b.check.clocks.eq(n_second)
        yield from pulse(b.check.start)
        out["n"] = yield from wait_done(b, 60_000)
        out["crc"] = yield b.check.crc
        yield from waits(tail * per)

    go2(b, sysgen(), rec, CLOCKS[clock])
    return rec, out


@pytest.mark.parametrize(
    ("clock", "k"),
    [
        ("normal", 3),
        ("normal", 9),
        ("normal", 20),
        ("mid", 9),
        pytest.param("mid", 3, marks=pytest.mark.slow),
        pytest.param("mid", 20, marks=pytest.mark.slow),
        pytest.param("fast", 9, marks=pytest.mark.slow),
    ],
)
def test_abort_then_start_on_the_next_clock_gives_clean_sequences(clock: str, k: int) -> None:
    rec, out = run_scenario(clock, k)
    assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(25)), (k, out)
    assert_clean(rec.pins)


@pytest.mark.slow
@pytest.mark.parametrize("clock", ["normal", "mid", "slow"])
def test_every_abort_delay_gives_clean_sequences(clock: str) -> None:
    ks = range(1, 41) if clock != "slow" else (1, 3, 6, 9, 15, 20, 30, 40)
    for k in ks:
        rec, out = run_scenario(clock, k, tail=50)
        assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(25)), (clock, k, out)
        assert_clean(rec.pins)


def single_start(clock_cfg, n: int = 20):
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from waits(8 * per_clock(clock_cfg))
        yield from start(b, n)
        out["n"] = yield from wait_done(b, 20_000)
        out["crc"] = yield b.check.crc
        yield from waits(30 * per_clock(clock_cfg))

    go2(b, sysgen(), rec, clock_cfg)
    return rec, out


@pytest.mark.parametrize(
    ("period", "offset"),
    [
        (400_000, 5_050),
        pytest.param(400_000, 123_450, marks=pytest.mark.slow),
        pytest.param(400_000, 301_150, marks=pytest.mark.slow),
        pytest.param(1_000_000, 5_050, marks=pytest.mark.slow),
        pytest.param(1_000_000, 333_350, marks=pytest.mark.slow),
        pytest.param(1_000_000, 777_750, marks=pytest.mark.slow),
    ],
)
def test_one_start_is_exactly_one_clean_sequence_at_slow_project_clocks(
    period: int, offset: int
) -> None:
    """A project clock slower than `sys` sees `run` and `gen` a clock apart in either order."""
    rec, out = single_start((period, offset))
    assert out["n"] is not None, "done never rose"
    assert out["crc"] == zlib.crc32(toy_bytes(20))
    blocks = assert_clean(rec.pins)
    assert len(blocks) == 1, blocks


def test_recipe_start_abort_start_on_the_next_clock_at_2_5_mhz() -> None:
    """The re-reviewer's recipe: project period 400 000 ps, start N=3, wait 120 system clocks,
    abort, start N=5 on the very next system clock: done with the CRC of N = 5."""
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from waits(8 * per_clock((400_000, 0)))
        yield from start(b, 3)
        yield from waits(120)
        yield from pulse(b.check.abort)
        yield b.check.clocks.eq(5)
        yield from pulse(b.check.start)
        out["n"] = yield from wait_done(b, 60_000)
        out["crc"] = yield b.check.crc
        yield from waits(100)

    go2(b, sysgen(), rec, (400_000, 5_050))
    assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(5))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(5))}
    assert_clean(rec.pins)


def test_the_checker_itself_rejects_malformed_sequences() -> None:
    n = NORMAL
    low, high = (0, 1, REF_UI), (1, 1, REF_UI)
    ok = [n] * 6 + [low] * 10 + [high] * 4 + [n] * 6
    assert assert_clean(ok) == [(14, 10)]
    assert assert_clean([n] * 6 + [low] * 3 + [n] * 6, full_last=False) == [(3, 3)]
    bad = {
        "override on with reset high first": [n] * 6 + [high] + [low] * 10 + [high] * 3 + [n] * 6,
        "rst_n pulse inside a block": [n] * 6
        + [low] * 4
        + [high]
        + [low] * 6
        + [high] * 2
        + [n] * 6,
        "reset low for 11 clocks": [n] * 6 + [low] * 11 + [high] * 2 + [n] * 6,
        "reset low for 9 then high": [n] * 6 + [low] * 9 + [high] * 3 + [n] * 6,
        "gap of 3": [n] * 6 + [low] * 10 + [high] + [n] * 3 + [low] * 10 + [high] + [n] * 6,
        "lag sample (rst low after override off)": [n] * 6
        + [low] * 10
        + [high]
        + [(0, 0, SW_UI)]
        + [n] * 6,
        "on/off/on": [n] * 6 + [low] * 3 + [n] * 2 + [low] * 10 + [high] + [n] * 6,
        "partial override": [n] * 6 + [(0, 1, SW_UI)] * 10 + [high] + [n] * 6,
        "runt reset pulse": [n] * 6 + [(0, 0, SW_UI)] + [n] * 6,
    }
    for what, pins in bad.items():
        with pytest.raises(AssertionError):
            assert_clean(pins)
            pytest.fail(f"accepted: {what}")
    # a gap of exactly MIN_GAP_CLOCKS is fine
    from tt_harness.core.atspeed import MIN_GAP_CLOCKS

    two = [n] * 6 + [low] * 10 + [high] + [n] * MIN_GAP_CLOCKS + [low] * 10 + [high] + [n] * 6
    assert len(assert_clean(two)) == 2


# Skew on each crossing wire ------------------------------------------------------------------
#
# The simulator has no skew; `late=` makes the crossing of one signal a project clock slower
# (the worst a first flop can do). A proper handshake must not care about any single wire.

WIRES = ["cmd.req", "cmd.ack_o", "rep.req", "rep.ack_o"]


def wire(b, name: str):
    crossing, signal = name.split(".")
    return getattr(getattr(b.check, f"{crossing}_x"), signal)


def late_scenario(kind: str, late_name: str, clock: str):
    b = make()
    rec, out = Recorder(b), {}
    per = per_clock(CLOCKS[clock])

    def sysgen():
        yield from setup(b, per)
        yield from start(b, 30)
        if kind == "abort_restart":
            yield from waits(9 * per)
            yield from pulse(b.check.abort)
            yield b.check.clocks.eq(25)
            yield from pulse(b.check.start)
        elif kind == "restart":
            yield from waits(9 * per)
            yield b.check.clocks.eq(25)
            yield from pulse(b.check.start)
        elif kind == "clock_stop":
            yield b.ce_project.eq(0)  # stopped before the command was captured
            yield from waits(120)
            yield b.ce_project.eq(1)
        out["n"] = yield from wait_done(b, 40_000)
        out["crc"] = yield b.check.crc
        yield from waits(12 * per)

    go2(b, sysgen(), rec, CLOCKS[clock], late=(wire(b, late_name),))
    return rec, out, 30 if kind == "clock_stop" else 25


@pytest.mark.parametrize("late_name", WIRES)
@pytest.mark.parametrize(
    "kind",
    [
        "abort_restart",
        "restart",
        "clock_stop",
    ],
)
def test_one_clock_of_skew_on_any_crossing_wire_breaks_nothing(kind: str, late_name: str) -> None:
    rec, out, n = late_scenario(kind, late_name, "normal")
    assert out["n"] is not None, "done never rose"
    assert out["crc"] == zlib.crc32(toy_bytes(n))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(n))}
    assert_clean(rec.pins)


@pytest.mark.slow
@pytest.mark.parametrize("late_name", WIRES)
@pytest.mark.parametrize("kind", ["abort_restart", "restart", "clock_stop"])
@pytest.mark.parametrize("clock", ["mid", "fast"])
def test_skew_on_any_crossing_wire_at_other_clock_ratios(
    kind: str, late_name: str, clock: str
) -> None:
    rec, out, n = late_scenario(kind, late_name, clock)
    assert out["n"] is not None, "done never rose"
    assert out["crc"] == zlib.crc32(toy_bytes(n))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(n))}
    assert_clean(rec.pins)


# The two scenarios the review asked for ------------------------------------------------------


@pytest.mark.parametrize("clock", ["normal", "mid"])
def test_start_on_the_very_next_clock_after_done_gives_the_new_crc(clock: str) -> None:
    """Done, then `start` on the very next system clock at a project period of at least 3 system
    clocks: the previous design missed the short low of `run` and hung."""
    b = make()
    rec, out = Recorder(b), {}
    per = per_clock(CLOCKS[clock])

    def sysgen():
        yield from setup(b, per)
        yield from start(b, 20)
        out["n1"] = yield from wait_done(b, 40_000)
        out["crc1"] = yield b.check.crc
        yield b.check.clocks.eq(33)
        yield from pulse(b.check.start)  # the very next system clock
        out["n2"] = yield from wait_done(b, 40_000)
        out["crc2"] = yield b.check.crc
        yield from waits(12 * per)

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["crc1"] == zlib.crc32(toy_bytes(20))
    assert out["n2"] is not None and out["crc2"] == zlib.crc32(toy_bytes(33))
    assert len(assert_clean(rec.pins)) == 2


@pytest.mark.parametrize("clock", ["normal", "mid"])
def test_abort_mid_count_then_start_gives_a_clean_sequence_and_the_right_crc(clock: str) -> None:
    rec, out = run_scenario(clock, 12)
    assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(25))
    blocks = assert_clean(rec.pins)
    assert len(blocks) == 2 and blocks[0][1] <= 10
