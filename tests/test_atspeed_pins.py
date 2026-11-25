# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: B023  (generators defined in loops run within the iteration that made them)
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


def test_the_checker_itself_rejects_runts() -> None:
    n = NORMAL
    ok = [n] * 6 + [(1, 1, REF_UI)] + [(0, 1, REF_UI)] * 10 + [(1, 1, REF_UI)] * 3 + [n] * 6
    assert assert_clean(ok) == [(14, 10)]
    runt_gap = (
        [n] * 6 + [(1, 1, REF_UI), (0, 1, REF_UI)] + [(0, 0, SW_UI)] + [(1, 1, REF_UI)] + [n] * 5
    )
    with pytest.raises(AssertionError):
        assert_clean(runt_gap)
    pulse_in_block = (
        [n] * 6 + [(0, 1, REF_UI)] * 4 + [(1, 1, REF_UI)] + [(0, 1, REF_UI)] * 6 + [n] * 6
    )
    with pytest.raises(AssertionError):
        assert_clean(pulse_in_block)
    close = [n] * 6 + [(0, 1, REF_UI)] * 10 + [n] * 2 + [(0, 1, REF_UI)] * 10 + [n] * 6
    with pytest.raises(AssertionError):
        assert_clean(close)
    half = [n] * 6 + [(1, 1, SW_UI)] + [n] * 6
    with pytest.raises(AssertionError):
        assert_clean(half)


@pytest.mark.parametrize("late", ["run", "gen"])
def test_skew_between_run_and_gen_does_not_start_a_check_twice(late: str) -> None:
    """`run` and `gen` change on the same system edge and cross through separate
    synchronisers; the simulator has no skew, so one crossing is made a project clock slower
    (either one): the project must still begin once, with one clean sequence."""
    for period, offset in ((39_700, 5_050), (400_000, 5_050)):
        b = make()
        rec, out = Recorder(b), {}
        per = per_clock((period, offset))
        sig = [b.check.run_level] if late == "run" else [b.check.gen]

        def sysgen():
            yield from setup(b)
            yield from waits(8 * per)
            yield from start(b, 20)
            out["n"] = yield from wait_done(b, 20_000)
            out["crc"] = yield b.check.crc
            yield from waits(30 * per)

        go2(b, sysgen(), rec, (period, offset), late=tuple(sig))
        assert out["n"] is not None, ("done never rose", late, period)
        assert out["crc"] == zlib.crc32(toy_bytes(20))
        blocks = assert_clean(rec.pins)
        assert len(blocks) == 1, (late, period, blocks)
