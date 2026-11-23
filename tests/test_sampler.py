# SPDX-License-Identifier: Apache-2.0
"""UoSampler tests (Migen simulator)."""

from __future__ import annotations

from migen import If, Module, Signal
from migen.sim import run_simulation

from tt_harness.core.sampler import UoSampler

CYCLES = 60


class _Bench(Module):
    """Drives `uo` from a free-running counter and strobes every third cycle."""

    def __init__(self) -> None:
        self.submodules.dut = UoSampler()
        self.counter = Signal(8)
        self.phase = Signal(2)
        self.sync += [
            self.counter.eq(self.counter + 1),
            If(self.phase == 2, self.phase.eq(0)).Else(self.phase.eq(self.phase + 1)),
        ]
        self.comb += [
            self.dut.uo.eq(self.counter),
            self.dut.strobe.eq(self.phase == 0),
        ]


def _trace() -> list[dict[str, int]]:
    """Per-cycle values of the sampler's ports."""
    bench = _Bench()
    dut = bench.dut
    trace: list[dict[str, int]] = []

    def gen():
        for _ in range(CYCLES):
            trace.append(
                {
                    "uo": (yield dut.uo),
                    "strobe": (yield dut.strobe),
                    "valid": (yield dut.valid),
                    "data": (yield dut.data),
                    "count": (yield dut.count),
                }
            )
            yield

    run_simulation(bench, gen())
    return trace


def test_strobed_values_in_order() -> None:
    trace = _trace()
    # Every strobe except a possible one in the final cycle has its valid cycle in the trace.
    strobed = [t["uo"] for t in trace[:-1] if t["strobe"]]
    captured = [t["data"] for t in trace if t["valid"]]
    assert len(strobed) == (CYCLES - 1 + 2) // 3
    assert captured == strobed
    # The counter makes the values distinct, so order and gaps are both checked.
    assert strobed == [(3 * k) & 0xFF for k in range(len(strobed))]


def test_no_valid_without_strobe() -> None:
    trace = _trace()
    assert trace[0]["valid"] == 0
    for c in range(1, CYCLES):
        assert trace[c]["valid"] == trace[c - 1]["strobe"], f"cycle {c}"


def test_count_equals_number_of_strobes() -> None:
    trace = _trace()
    for c in range(CYCLES):
        assert trace[c]["count"] == sum(t["strobe"] for t in trace[:c]), f"cycle {c}"
