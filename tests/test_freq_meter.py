# SPDX-License-Identifier: Apache-2.0
"""`FreqMeter` and `HighTimeMeter` in the Migen simulator, with two clock domains.

The simulator has no metastability: what these tests show is the arithmetic (the count per
gate, its +/-1 bound at every phase, that nothing is lost between gates and across the
counter's wrap) and the property the crossing rests on (one Gray bit changes per measured
clock). Times are in simulator units; the system clock's period is `SYS`.
"""

from __future__ import annotations

import itertools
from fractions import Fraction

import pytest
from migen import Module, Signal, run_simulation

from tt_harness.core.freq_meter import FreqMeter, HighTimeMeter, sampling_phases

SYS = 20
STOPPED = 10**9  # a period longer than any simulation here: the clock never rises


def readings(dut, gates: int, clocks: dict, meter=None) -> list[int]:
    meter = meter or dut
    out: list[int] = []

    def watch():
        while len(out) < gates:
            if (yield meter.valid):
                out.append((yield meter.count))
            yield

    run_simulation(dut, watch(), clocks=clocks)
    return out


@pytest.mark.parametrize(
    "meas_period",
    [
        14,  # faster than the system clock, ratio 10/7
        40,  # exactly half
        54,  # slower, ratio 10/27
        6,  # more than three times faster
    ],
)
def test_count_is_within_one_of_the_ratio(meas_period: int) -> None:
    gate = 64
    got = readings(FreqMeter(gate), 6, {"sys": SYS, "meas": meas_period})
    ideal = Fraction(gate * SYS, meas_period)
    # The first gate starts at configuration, before the synchroniser has filled.
    assert all(abs(c - ideal) <= 1 for c in got[1:]), (got, float(ideal))
    assert got[0] <= ideal + 1
    # Nothing is lost between gates: the readings add up to the edges of the whole span.
    assert abs(sum(got[1:]) - ideal * len(got[1:])) <= 1, got


def test_a_stopped_clock_reads_zero() -> None:
    got = readings(FreqMeter(32), 4, {"sys": SYS, "meas": STOPPED})
    assert got == [0, 0, 0, 0]


@pytest.mark.parametrize("meas_period", [14, 20, 26])
def test_no_phase_gives_a_torn_count(meas_period: int) -> None:
    gate = 16
    ideal = Fraction(gate * SYS, meas_period)
    seen = set()
    for phase in range(meas_period):
        got = readings(FreqMeter(gate), 5, {"sys": SYS, "meas": (meas_period, phase)})
        seen.update(got[1:])
        assert all(abs(c - ideal) <= 1 for c in got[1:]), (phase, got, float(ideal))
    assert len(seen) <= 3, seen


def test_counts_are_right_across_the_counter_wrap() -> None:
    # A 4-bit counter wraps every 16 edges; a gate holds about 11.4 of them.
    gate = 8
    got = readings(FreqMeter(gate, width=4), 12, {"sys": SYS, "meas": 14})
    ideal = Fraction(gate * SYS, 14)
    assert all(abs(c - ideal) <= 1 for c in got[1:]), got


def test_one_gray_bit_changes_per_measured_clock() -> None:
    dut = FreqMeter(8, width=4)
    values: list[int] = []

    def watch():
        for _ in range(40):  # more than two wraps of a 4-bit counter
            values.append((yield dut.gray))
            yield

    run_simulation(dut, {"meas": watch()}, clocks={"sys": SYS, "meas": 14})
    steps = [a ^ b for a, b in itertools.pairwise(values)]
    assert all(s.bit_count() == 1 for s in steps), values
    assert len(set(values)) == 16


def test_gate_must_be_sensible() -> None:
    with pytest.raises(ValueError, match="gate_cycles"):
        FreqMeter(1)
    with pytest.raises(ValueError, match="does not fit"):
        HighTimeMeter(2**8, width=8)


class _Level(Module):
    """A `HighTimeMeter` watching a level that is high `high` of every 4 meas clocks."""

    def __init__(self, gate: int, high: int) -> None:
        self.submodules.meter = HighTimeMeter(gate)
        step = Signal(2)
        self.sync.meas += [step.eq(step + 1), self.meter.level.eq(step < high)]


@pytest.mark.parametrize("high,want", [(0, 0.0), (4, 1.0), (2, 0.5), (1, 0.25)])
def test_high_time_meter_reads_the_fraction_high(high: int, want: float) -> None:
    gate = 280
    dut = _Level(gate, high)
    # The level's period is 4 x 14 = 56 units against a sampling period of 20: the ratio is
    # 5/14, so the samples visit 14 phases and the reading is good to 1/14 of the period.
    got = readings(dut, 4, {"sys": SYS, "meas": 14}, meter=dut.meter)
    assert sampling_phases(Fraction(1, 56), Fraction(1, SYS)) == 14
    assert all(abs(c / gate - want) <= 1 / 14 + 1e-9 for c in got[1:]), got


def test_sampling_phases_of_the_probe_settings() -> None:
    fin = Fraction(100_000_000)
    assert sampling_phases(fin * 9 / Fraction("35.75"), fin) == 143  # settings A and B
    assert sampling_phases(fin * 9 / Fraction("36.75"), fin) == 49  # setting C
    assert sampling_phases(fin * 9 / 36, fin) == 4  # setting D: four phases, no resolution
