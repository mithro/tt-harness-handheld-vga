# SPDX-License-Identifier: Apache-2.0
"""A model of what the board's demo runner writes for one entry, from `docs/demo-runner.md`.

The oracle for the firmware's runner: `run()` lists the writes, in order, with the runner's own
nominal times (its waits, and the 2 ms reset sleep), not wall time.

Provenance: written from `docs/demo-runner.md` alone. That document describes the behaviour of
the board's `demo.py` (LGPL-2.1, tt-demo-configurator checkout at commit 8b7c392, a copy of the
firmware's commit c32d62a); no code of it is used here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain, groupby

from tt_harness.demo.validate import Entry, Gamepad, Set, WaitMs, gamepad_bits

KINDS = ("ena", "clock_hz", "rst_n", "ui_in", "gamepad", "next")
PAD_LATCH = 0x10
PAD_CLOCK = 0x20
PAD_DATA = 0x40
RESET_MS = 2
WORD_BITS = 24


@dataclass(frozen=True)
class Event:
    """One write: `t_ms` since the entry was selected, `kind` (one of `KINDS`), and its value."""

    t_ms: int
    kind: str
    value: int


def _frame_levels(word: int) -> Iterator[int]:
    """Every level of a report, repeats included: one bit time per bit, then the latch tail."""
    wire_bits = [(word >> n) & 1 for n in range(WORD_BITS)]
    for bit in wire_bits:
        data = PAD_DATA * bit
        yield data  # data settles while the clock is low
        yield data | PAD_CLOCK  # rising edge: the receiver samples
        yield data
    held = PAD_DATA * wire_bits[-1]  # the data line still shows the last bit
    yield held | PAD_LATCH
    yield held
    yield 0


def gamepad_levels(word: int) -> list[int]:
    """The `ui_in` bits 4..6 values written for one report of `word`, in order.

    The levels of the frame in `docs/demo-runner.md` with each run of equal consecutive levels
    written once; the line starts idle (0), so a leading 0 is not written either.
    """
    runs = [level for level, _ in groupby(chain([0], _frame_levels(word)))]
    return runs[1:]


class _Done(Exception):
    """The time limit passed: nothing more is written."""


def run(entry: Entry, total_projects: int, until_ms: int) -> list[Event]:
    """The writes the runner makes for `entry` from selection until `until_ms` (included).

    With `total_projects` > 1 the duration timer is armed: at `entry.duration_ms` the model adds a
    final `next` event (value 0) and stops, after every other event up to and including that time.
    """
    next_ms = entry.duration_ms if total_projects > 1 else None
    limit = until_ms if next_ms is None else min(until_ms, next_ms)
    events: list[Event] = []

    def emit(t: int, kind: str, value: int) -> None:
        if t > limit:
            raise _Done
        events.append(Event(t, kind, value))

    try:
        t = 0
        emit(t, "ena", 1)
        emit(t, "clock_hz", entry.clock_hz)
        emit(t, "ui_in", entry.ui_in)
        _play_steps(entry, emit)
    except _Done:
        pass
    if next_ms is not None and next_ms <= until_ms:
        events.append(Event(next_ms, "next", 0))
    return events


def _play_steps(entry: Entry, emit) -> None:
    steps = entry.steps
    if not steps:
        return
    if sum(s.ms for s in steps if isinstance(s, WaitMs)) <= 0:
        raise ValueError("steps must wait for some time in total")
    report: list[int] | None = None  # the buttons held: the levels of the last gamepad step
    base = entry.ui_in
    t = 0

    def send() -> None:
        for level in report:
            emit(t, "gamepad", base | level)

    while True:
        for step in steps:
            if isinstance(step, Set):
                base = step.value
                emit(t, "ui_in", step.value)
            elif isinstance(step, Gamepad):
                report = gamepad_levels(gamepad_bits(step.controllers))
                send()
            elif isinstance(step, WaitMs):
                t += step.ms
        # out of steps: reset the project, then start over
        emit(t, "rst_n", 0)
        t += RESET_MS
        emit(t, "rst_n", 1)
        if report is not None:
            send()
