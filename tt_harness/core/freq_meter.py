# SPDX-License-Identifier: Apache-2.0
"""Meters for a clock that is asynchronous to the system clock: frequency and time high.

`FreqMeter` counts the rising edges of a measured clock during a gate of system-clock
cycles. The scheme:

1. In the measured clock's domain a free-running binary counter advances on every rising
   edge, and a register holds its Gray code (`n ^ (n >> 1)`), so exactly one bit of that
   register changes per measured clock, across the wrap as well.
2. The Gray register crosses into the system domain through two flip-flops per bit (a
   `MultiReg`). Because only one bit is in flight at any time, a sample is always a value the
   counter really held: the one before or the one after the edge that raced the sample,
   never a mixture.
3. The system domain converts the sample back to binary and registers it. At every gate
   boundary the difference from the value at the previous boundary (modulo 2**width) is the
   count; nothing is ever reset or stopped, so no edge is lost between gates and consecutive
   counts add up exactly.

Resolution. Any window of the gate's length holds floor or ceil of (gate x f_meas / f_sys)
rising edges. Each boundary sample may also be one count early or late when an edge races
the synchroniser, so a single reading is within +/-1 of the ideal value in the ordinary case
and at most 1 further out when both boundaries resolve the unlucky way; the error does not
accumulate (the sum of N readings has the same bound as one). The counter must not advance
by 2**width or more in one gate: 32 bits is enough for a 1 s gate up to 4.29 GHz.

The crossing is asynchronous by design and must not be timed as a synchronous path. The
only requirement is that the Gray bits arrive within one measured-clock period of each
other, which any placement meets at the frequencies of interest.

`HighTimeMeter` counts the system-clock cycles of a gate at which a level, sampled through a
synchroniser, was high: the fraction of time the level is high, provided the sampling
instants are spread over the level's period (see `sampling_phases`).
"""

from __future__ import annotations

from fractions import Fraction
from functools import reduce
from operator import xor

from migen import If, Module, Signal
from migen.genlib.cdc import MultiReg


def sampling_phases(f_signal: Fraction, f_sample: Fraction) -> int:
    """How many distinct phases of a periodic signal a coherent sampling clock visits.

    When both frequencies come from one oscillator their ratio is a fraction p/q in lowest
    terms, and the samples fall on exactly q equally spaced phases of the signal's period, in
    a fixed repeating pattern. A fraction-high statistic then has a resolution of 1/q of the
    period however long it runs, and is biased by up to that much.
    """
    return (Fraction(f_signal) / Fraction(f_sample)).denominator


class Gate(Module):
    """`tick` is high for one system clock in every `gate_cycles`.

    Meters that must report the same gate share one `Gate`. Separate gate counters with the
    same reset value are not a substitute: flip-flops are not all released on the same clock
    edge after configuration, so two such counters can start a clock apart, and a reader
    that takes every meter's count on one meter's `valid` then gets a count that is one gate
    old (0 in the first gate) from the others.
    """

    def __init__(self, gate_cycles: int) -> None:
        if type(gate_cycles) is not int or gate_cycles < 2:
            raise ValueError(f"gate_cycles must be an integer of at least 2, not {gate_cycles!r}")
        self.gate_cycles = gate_cycles
        self.tick = Signal()
        self.left = left = Signal(max=gate_cycles, reset=gate_cycles - 1)
        self.comb += self.tick.eq(left == 0)
        self.sync += If(self.tick, left.eq(gate_cycles - 1)).Else(left.eq(left - 1))


def _own_or_shared(meter: Module, gate: int | Gate) -> Gate:
    """`gate` itself if it is a `Gate`; otherwise a new one, owned by `meter`."""
    if isinstance(gate, Gate):
        return gate
    own = Gate(gate)
    meter.submodules += own
    return own


class FreqMeter(Module):
    """Rising edges of the clock of domain `meas_domain` per `gate_cycles` system clocks.

    Outputs, in the system domain: `count` (the edges in the gate that just ended) and
    `valid`, high for one system clock when `count` changes. The first gate after
    configuration starts at configuration. The measured clock is the clock of the domain
    named `meas_domain`, which the parent module provides; it may stop (the count is then
    0) and has no reset. `gate_cycles` is the gate's length, or a `Gate` shared with other
    meters (which its owner adds as a submodule).
    """

    def __init__(self, gate_cycles: int | Gate, meas_domain: str = "meas", width: int = 32) -> None:
        self.gate = gate = _own_or_shared(self, gate_cycles)
        self.gate_cycles = gate.gate_cycles
        self.count = Signal(width)
        self.valid = Signal()

        # Measured domain: binary counter and its Gray code, both registered.
        binary = Signal(width)
        self.gray = gray = Signal(width)
        following = Signal(width)
        self.comb += following.eq(binary + 1)
        meas = getattr(self.sync, meas_domain)
        meas += [binary.eq(following), gray.eq(following ^ following[1:])]

        # System domain.
        gray_sys = Signal(width)
        self.specials += MultiReg(gray, gray_sys, odomain="sys", n=2)
        current = Signal(width)
        previous = Signal(width)
        self.sync += current.eq(
            sum(reduce(xor, [gray_sys[j] for j in range(i, width)]) << i for i in range(width))
        )
        self.sync += [
            self.valid.eq(gate.tick),
            If(gate.tick, self.count.eq(current - previous), previous.eq(current)),
        ]


class HighTimeMeter(Module):
    """System clocks per gate at which the asynchronous input `level` was sampled high.

    Outputs `count` and `valid` as `FreqMeter`. `level` goes through a two-flop synchroniser.
    `gate_cycles` is the gate's length or a shared `Gate`, as for `FreqMeter`.
    """

    def __init__(self, gate_cycles: int | Gate, width: int = 32) -> None:
        self.gate = gate = _own_or_shared(self, gate_cycles)
        if gate.gate_cycles >= 2**width:
            raise ValueError(f"a gate of {gate.gate_cycles} cycles does not fit {width} bits")
        self.level = Signal()
        self.count = Signal(width)
        self.valid = Signal()

        sample = Signal()
        self.specials += MultiReg(self.level, sample, odomain="sys", n=2)
        running = Signal(width)
        self.sync += [
            self.valid.eq(gate.tick),
            If(gate.tick, self.count.eq(running + sample), running.eq(0)).Else(
                running.eq(running + sample)
            ),
        ]
