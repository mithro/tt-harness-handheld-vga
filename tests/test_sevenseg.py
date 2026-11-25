# SPDX-License-Identifier: Apache-2.0
"""`SevenSegSampler` in the Migen simulator: exact duty values for known bit patterns."""

from __future__ import annotations

import itertools

import pytest
from migen import run_simulation

from tt_harness.core.sevenseg import SevenSegSampler

L = 6  # a 64-clock window keeps the tests fast; the arithmetic is the same at any size


def expected_duty(high_clocks: int, log2: int) -> int:
    """The documented mapping: round-half-up of high_clocks * 255 / 2**log2."""
    return (high_clocks * 255 + (1 << (log2 - 1))) >> log2


def read_duty(dut):
    values = []
    for d in dut.duty:
        values.append((yield d))
    return values


def capture(dut, drive, windows: int) -> list[list[int]]:
    """Run until `windows` window pulses were seen; the duty values at each pulse."""
    out: list[list[int]] = []

    def run():
        n = 0
        while len(out) < windows:
            yield from drive(n)
            if (yield dut.window):
                out.append((yield from read_duty(dut)))
            yield
            n += 1

    run_simulation(dut, run())
    return out


def test_constant_segments_read_0_and_255() -> None:
    dut = SevenSegSampler(L)

    def drive(n):
        yield dut.uo_out.eq(0b10101010)

    for duty in capture(dut, drive, 3)[1:]:
        assert duty == [0, 255, 0, 255, 0, 255, 0, 255]


def test_half_duty_reads_128_within_1() -> None:
    dut = SevenSegSampler(L)

    def drive(n):
        b0 = n & 1  # alternates every clock
        b1 = (n % (1 << L)) < (1 << (L - 1))  # first half of each window
        b2 = (n % 4) == 0  # one clock in four
        yield dut.uo_out.eq(b0 | (b1 << 1) | (b2 << 2))

    for duty in capture(dut, drive, 4)[1:]:
        assert abs(duty[0] - 128) <= 1
        assert abs(duty[1] - 128) <= 1
        assert duty[2] == expected_duty(1 << (L - 2), L)  # 16 of 64 clocks
        assert duty[3:] == [0] * 5


@pytest.mark.parametrize("high", range((1 << L) + 1))
def test_every_count_maps_by_the_documented_formula(high: int) -> None:
    dut = SevenSegSampler(L)

    def drive(n):
        # bit 0 is high for the first `high` clocks of every window of 2**L clocks
        yield dut.uo_out.eq(1 if (n % (1 << L)) < high else 0)

    duty = capture(dut, drive, 3)[2]
    assert duty[0] == expected_duty(high, L)


def test_duty_mapping_is_monotonic_and_spans_0_to_255() -> None:
    values = [expected_duty(c, L) for c in range((1 << L) + 1)]
    assert values[0] == 0 and values[-1] == 255
    assert values == sorted(values)


def test_window_publishes_all_eight_together() -> None:
    dut = SevenSegSampler(L)
    changes: list[tuple[int, int, int]] = []  # (cycle, mask of changed duties, window)
    pulses: list[int] = []

    def run():
        last = [0] * 8
        for n in range(6 * (1 << L)):
            # bit i is high for (i+1)*7 clocks of every window: eight different values
            yield dut.uo_out.eq(sum(((n % (1 << L)) < (i + 1) * 7) << i for i in range(8)))
            now = yield from read_duty(dut)
            window = yield dut.window
            mask = sum((now[i] != last[i]) << i for i in range(8))
            if mask:
                changes.append((n, mask, window))
            if window:
                pulses.append(n)
            last = now
            yield

    run_simulation(dut, run())
    assert len(pulses) >= 4
    assert {b - a for a, b in itertools.pairwise(pulses)} == {1 << L}
    assert changes, "the duties never changed"
    for cycle, _, window in changes:
        assert window == 1, f"duty changed at cycle {cycle} without a window pulse"
    assert changes[0][1] == 0xFF  # the first publication moves all eight at once


def test_outputs_hold_between_windows_and_pulse_is_one_clock() -> None:
    dut = SevenSegSampler(L)
    seen: list[tuple[int, list[int]]] = []

    def run():
        for n in range(4 * (1 << L)):
            yield dut.uo_out.eq(0xFF if (1 << L) - 1 <= n < (2 << L) - 1 else 0x00)
            seen.append(((yield dut.window), (yield from read_duty(dut))))
            yield

    run_simulation(dut, run())
    pulses = [i for i, (w, _) in enumerate(seen) if w]
    assert all(b - a == 1 << L for a, b in itertools.pairwise(pulses))
    first, second = pulses[1], pulses[2]
    for i in range(first, second):
        assert seen[i][1] == [255] * 8
    assert seen[second][1] == [0] * 8


def test_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        SevenSegSampler(0)
