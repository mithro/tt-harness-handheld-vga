# SPDX-License-Identifier: Apache-2.0
"""A model of what the board's demo runner writes for one entry, from `docs/demo-runner.md`.

The oracle for the firmware's runner: `run()` lists the writes, in order, with the runner's own
nominal times (its waits, and the 2 ms reset sleep), not wall time.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def gamepad_levels(word: int) -> list[int]:
    """The `ui_in` bits 4..6 values written for one report of `word`, in order.

    Per bit, bit 0 first: data with the clock low, data with the clock high, data with the clock
    low. Then the latch high and low with the last data bit still on the data line, then all
    low. A value equal to the one before it (starting from idle, 0) is not written again.
    """
    seq: list[int] = []
    data = 0
    for i in range(WORD_BITS):
        data = PAD_DATA if (word >> i) & 1 else 0
        seq += [data, data | PAD_CLOCK, data]
    seq += [data | PAD_LATCH, data, 0]
    out: list[int] = []
    last = 0
    for level in seq:
        if level != last:
            out.append(level)
            last = level
    return out


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
    reports: dict[int, list[int]] = {}
    for i, step in enumerate(steps):
        if isinstance(step, Gamepad):
            reports[i] = gamepad_levels(gamepad_bits(step.controllers))
    report: list[int] | None = None
    base = entry.ui_in
    t = 0

    def send() -> None:
        for level in report:
            emit(t, "gamepad", base | level)

    while True:
        for i, step in enumerate(steps):
            if isinstance(step, Set):
                base = step.value
                emit(t, "ui_in", step.value)
            elif isinstance(step, Gamepad):
                report = reports[i]
                send()
            elif isinstance(step, WaitMs):
                t += step.ms
        # out of steps: reset the project, then start over
        emit(t, "rst_n", 0)
        t += RESET_MS
        emit(t, "rst_n", 1)
        if report is not None:
            send()
