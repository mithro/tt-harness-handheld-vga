# SPDX-License-Identifier: Apache-2.0
"""ReferenceStimulus tests (Migen simulator).

The stimulus is driven as the recorder drives it: `clock_index` counts, with `advance` high
in the cycle at whose end it increments and any number of idle cycles between.
"""

from __future__ import annotations

import pytest
from migen.sim import run_simulation

from tt_harness.core.stimulus import RESET_CLOCKS, ReferenceStimulus


def _trace(stim: ReferenceStimulus, steps: int, start: int = 0, idle: int = 3) -> list[dict]:
    """One record per system cycle: clock_index, advance and the outputs seen in it."""
    seen: list[dict] = []

    def record():
        seen.append(
            {
                "index": (yield stim.clock_index),
                "advance": (yield stim.advance),
                "ena": (yield stim.ena),
                "rst_n": (yield stim.rst_n),
                "ui_in": (yield stim.ui_in),
                "uio_in": (yield stim.uio_in),
            }
        )

    def gen():
        index = start
        yield stim.clock_index.eq(index)
        for _ in range(steps):
            for _ in range(idle):
                yield
                yield from record()
            yield stim.advance.eq(1)
            yield
            yield from record()
            index = (index + 1) & 0xFFFFFFFF
            yield stim.advance.eq(0)
            yield stim.clock_index.eq(index)
        for _ in range(idle):
            yield
            yield from record()

    run_simulation(stim, gen())
    return seen


def test_reset_for_first_ten_clocks() -> None:
    assert RESET_CLOCKS == 10
    seen = _trace(ReferenceStimulus(ui_in=0x5A, uio_in=0xC3), steps=25)
    assert {c["index"] for c in seen} == set(range(26))
    for c in seen:
        assert c["rst_n"] == (0 if c["index"] < 10 else 1), c


def test_rst_n_changes_in_the_same_cycle_as_clock_index_and_only_then() -> None:
    seen = _trace(ReferenceStimulus(ui_in=0, uio_in=0), steps=14)
    changes = [i for i in range(1, len(seen)) if seen[i]["rst_n"] != seen[i - 1]["rst_n"]]
    assert len(changes) == 1
    (i,) = changes
    # The cycle in which the index is first seen as 10, right after the advance cycle.
    assert seen[i]["index"] == 10 and seen[i - 1]["index"] == 9
    assert seen[i - 1]["advance"] == 1


def test_rst_n_is_a_register_with_reset_value_zero() -> None:
    stim = ReferenceStimulus(ui_in=0, uio_in=0)
    assert stim.rst_n.reset.value == 0
    # Without an advance nothing changes it, whatever the index input does.
    seen: list[int] = []

    def gen():
        for index in (0, 5, 10, 11, 2**31, 9):
            yield stim.clock_index.eq(index)
            yield
            seen.append((yield stim.rst_n))

    run_simulation(stim, gen())
    assert seen == [0] * 6


def test_constants() -> None:
    seen = _trace(ReferenceStimulus(ui_in=0x5A, uio_in=0xC3), steps=12)
    for c in seen:
        assert (c["ena"], c["ui_in"], c["uio_in"]) == (1, 0x5A, 0xC3), c


@pytest.mark.parametrize("reset_clocks", [1, 3, 17])
def test_other_reset_lengths(reset_clocks: int) -> None:
    seen = _trace(ReferenceStimulus(ui_in=0, uio_in=0, reset_clocks=reset_clocks), steps=25)
    for c in seen:
        assert c["rst_n"] == (0 if c["index"] < reset_clocks else 1), c


def test_zero_reset_clocks_never_asserts_reset() -> None:
    stim = ReferenceStimulus(ui_in=0xFF, uio_in=0x00, reset_clocks=0)
    assert stim.rst_n.reset.value == 1
    seen = _trace(stim, steps=12)
    for c in seen:
        assert (c["rst_n"], c["ui_in"], c["uio_in"]) == (1, 0xFF, 0x00), c


def test_rst_n_stays_high_when_the_index_wraps() -> None:
    stim = ReferenceStimulus(ui_in=0, uio_in=0)
    seen = _trace(stim, steps=12, start=0)  # release reset first
    assert seen[-1]["rst_n"] == 1
    stim = ReferenceStimulus(ui_in=0, uio_in=0, reset_clocks=0)
    seen = _trace(stim, steps=4, start=2**32 - 2)
    assert {c["index"] for c in seen} == {2**32 - 2, 2**32 - 1, 0, 1, 2}
    assert all(c["rst_n"] == 1 for c in seen)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ui_in": 256, "uio_in": 0},
        {"ui_in": -1, "uio_in": 0},
        {"ui_in": 0, "uio_in": 256},
        {"ui_in": 0, "uio_in": 0, "reset_clocks": -1},
    ],
)
def test_rejects_out_of_range(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ReferenceStimulus(**kwargs)
