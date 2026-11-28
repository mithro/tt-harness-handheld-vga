# SPDX-License-Identifier: Apache-2.0
"""`MultiBitCrossing` in both directions: whole values only, newest wins, `valid` and `busy`.

The `ProjectSlot` tests (`test_slot.py`) cover the sys -> project use; here the same protocol is
run in both directions with a wide word (the at-speed check's command and report words), at
several relative phases of two unrelated clocks, with writes back to back and spread out. The
Migen simulator is zero-delay: it shows the protocol is functionally right, not anything about
metastability (see `test_slot.py`).
"""

from __future__ import annotations

import pytest
from migen import Module
from slot_sim import SYS_PERIOD, gate, run

from tt_harness.core.cdc import MultiBitCrossing

WIDTH = 48
ONES = (1 << WIDTH) - 1
GAPS = [0, 0, 0, 1, 1, 2, 3, 5, 7, 9, 11, 13, 17, 21, 0, 0, 30, 45, 0, 2]


class _Bench(Module):
    def __init__(self, forward: bool) -> None:
        idomain, odomain = ("sys", "project") if forward else ("project", "sys")
        self.submodules.x = MultiBitCrossing(WIDTH, idomain=idomain, odomain=odomain)


def _writer(x, gaps, values, wait_after: int):
    for gap, value in zip(gaps, values, strict=True):
        for _ in range(gap):
            yield
        yield x.src.eq(value)
    for _ in range(wait_after):
        yield


def _collapse(seq):
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def _subsequence(small, big):
    it = iter(big)
    return all(any(v == w for w in it) for v in small)


@pytest.mark.parametrize("forward", [True, False], ids=["sys_to_project", "project_to_sys"])
@pytest.mark.parametrize(
    ("period", "offset"),
    [
        (39_700, 5_050),
        (39_700, 14_050),
        (100_000, 5_050),
        (15_152, 3_050),  # 66 MHz: the project clock clearly faster than `sys`
        (10_000, 4_050),  # 100 MHz
        pytest.param(27_300, 3_050, marks=pytest.mark.slow),
        pytest.param(27_300, 9_050, marks=pytest.mark.slow),
        pytest.param(1_000_000, 5_050, marks=pytest.mark.slow),
        pytest.param(20_100, 4_050, marks=pytest.mark.slow),
    ],
)
def test_whole_values_only_newest_wins_and_valid_marks_each_delivery(
    forward: bool, period: int, offset: int
) -> None:
    b = gate(_Bench(forward))
    x = b.x
    values = [ONES if i % 2 == 0 else 0 for i in range(len(GAPS))]
    seen: list[int] = []
    valids: list[tuple[int, int]] = []

    def observer():
        yield "passive"
        prev = yield x.dst
        while True:
            dst, valid = (yield x.dst), (yield x.valid)
            seen.append(dst)
            valids.append((int(dst != prev), valid))
            prev = dst
            yield

    per = max(1, period // SYS_PERIOD)
    writer = _writer(x, GAPS, values, wait_after=200 * per)
    if forward:
        run(b, [writer], [observer()], project=(period, offset))
    else:
        run(b, [observer()], [writer], project=(period, offset))
    assert set(seen) <= {0, ONES}, "a mixed value was delivered"
    assert _subsequence(_collapse(seen), _collapse([0, *values])), "out of order"
    assert seen[-1] == values[-1], "the newest value was not delivered"
    assert len(_collapse(seen)) >= 3  # at 1 MHz most writes are overtaken (newest wins)
    # `valid` is high exactly in the cycle in which `dst` took a new value.
    assert all(changed == valid for changed, valid in valids[1:]), "valid does not mark deliveries"


@pytest.mark.parametrize("forward", [True, False], ids=["sys_to_project", "project_to_sys"])
def test_busy_is_high_from_the_pickup_until_the_acknowledge_and_the_sender_never_blocks(
    forward: bool,
) -> None:
    b = gate(_Bench(forward))
    x = b.x
    out: dict = {}

    def writer():
        for _ in range(5):
            yield
        for v in (1, 2, 3, 4):  # four writes in four clocks: never waits
            yield x.src.eq(v)
            yield
        out["busy_soon"] = yield x.busy
        for _ in range(400):
            yield
        out["busy_late"] = yield x.busy
        out["dst"] = yield x.dst

    if forward:
        run(b, [writer()], [])
    else:
        run(b, [], [writer()])
    assert out["busy_soon"] == 1 and out["busy_late"] == 0 and out["dst"] == 4
