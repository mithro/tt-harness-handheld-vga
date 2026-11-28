# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: B023  (the generators defined in the loops run within the iteration that made them)
"""The order of the control writes is kept at the project's pins.

Firmware writes `ui_in`, `ena` (and `gamepad_enable`), then releases `rst_n`. A design may read
its `ui_in` straps in its first clocks out of reset, so on the first project clock at which the
design sees `rst_n` high, `ena` and `ui_in` must already hold the values written before the
release was written (`ProjectSlot` docstring, "The control word").

Each case records the pins as the design sees them at every project clock edge and requires,
for every edge after the release was written at which `rst_n` is high, the written `ena` and
`ui_in`; and that `rst_n` does rise. Run at 0.4, 2.5, 25, 50 and 66 MHz against the 50 MHz
system clock, at several phases, with the clock running and with it stopped during the writes,
from power-up and from a design that is already running (where a release bit delivered earlier
must not release the new reset early).

Limits: Migen's zero-delay simulator (see `tests/test_slot.py`).
"""

from __future__ import annotations

import pytest
from migen import Signal
from slot_sim import SYS_PERIOD, gate, run

from tt_harness.core.slot import ProjectSlot

# Project clock periods in ps: 0.4, 2.5, 25.2, 50 and 66 MHz.
PERIODS = {"0.4": 2_500_000, "2.5": 400_000, "25": 39_700, "50": 20_000, "66": 15_150}
OFFSETS = (50, 5_050, 9_950, 13_350)  # first project edge, ps: never on a system edge
FINAL_UI = 0xA5


def waits(n: int):
    for _ in range(n):
        yield


def _settle(period: int) -> int:
    """System clocks that are certainly enough for anything in flight to arrive: three
    transfers in a row (at most 4 project clocks and 3 system clocks each) and the two reset
    flops are under 20 project clocks."""
    return 40 + 24 * period // SYS_PERIOD


def _offsets(period: int) -> tuple[int, ...]:
    """Phases of the project clock tried: against a clock much slower than `sys` the phase
    within a system period changes nothing, so one."""
    return tuple(o for o in OFFSETS if o < period) if period < 100_000 else OFFSETS[1:2]


def scenario(period: int, offset: int, stopped: bool, start: str, gap: int, gamepad: bool = False):
    """Run one case; return (pins seen at each project edge after the release was written,
    emulator lines at the same edges)."""
    slot = gate(
        ProjectSlot(None, Signal(), 100_000_000, 25_175_000, sim_clock=True),
        running=not (stopped and start == "power_up"),
    )
    phase = {"released": False}
    pins: list[tuple[int, int, int]] = []
    lines: list[int] = []

    def rec():
        yield "passive"
        emu = slot.gamepad
        while True:
            if (yield slot.ce_project) and phase["released"]:
                pins.append(((yield slot.pin_rst_n), (yield slot.pin_ena), (yield slot.pin_ui_in)))
                lines.append((yield emu.latch) | (yield emu.clock) << 1 | (yield emu.data) << 2)
            yield

    def sysgen():
        yield from waits(7)
        if start == "running_design":
            # A design already out of reset with other values; then a short reset.
            yield slot.ui_in.eq(0x3C)
            yield slot.ena.eq(0)
            yield slot.rst_n.eq(1)
            yield from waits(_settle(period))
            assert (yield slot.pin_rst_n) == 1
            if stopped:
                yield slot.ce_project.eq(0)
                yield from waits(3)
            yield slot.rst_n.eq(0)
            yield
        elif start == "two_writes":
            yield slot.ui_in.eq(0x11)
            yield from waits(4)
        # Firmware order: ui_in, ena, then release, `gap` system clocks apart.
        yield slot.ui_in.eq(FINAL_UI)
        yield from waits(gap)
        yield slot.ena.eq(1)
        if gamepad:
            yield slot.gamepad_enable.eq(1)
        yield from waits(gap)
        yield slot.rst_n.eq(1)
        phase["released"] = True
        if stopped:
            yield from waits(25)
            yield slot.ce_project.eq(1)
        yield from waits(_settle(period))

    run(slot, [sysgen()], [rec()], project=(period, offset))
    return pins, lines


CASES = [
    (name, stopped, start, gap)
    for name in PERIODS
    for stopped in (False, True)
    for start in ("power_up", "two_writes", "running_design")
    for gap in (1, 0)
    if not (start == "two_writes" and gap == 0)
]


@pytest.mark.parametrize(("clock", "stopped", "start", "gap"), CASES)
def test_rst_n_never_rises_before_the_ena_and_ui_in_written_before_the_release(
    clock: str, stopped: bool, start: str, gap: int
) -> None:
    period = PERIODS[clock]
    for offset in _offsets(period):
        pins, _ = scenario(period, offset, stopped, start, gap)
        high = [k for k, p in enumerate(pins) if p[0] == 1]
        assert high, f"offset {offset}: rst_n never rose ({pins[:12]})"
        wrong = [(k, pins[k]) for k in high if pins[k][1:] != (1, FINAL_UI)]
        assert not wrong, (
            f"offset {offset}: rst_n high at project clock {wrong[0][0]} after the release with "
            f"ena, ui_in = {wrong[0][1][1]}, {wrong[0][1][2]:#x} (first high at {high[0]})"
        )
        # Once released it stays released.
        assert all(p[0] == 1 for p in pins[high[0] :])


@pytest.mark.parametrize("clock", ["2.5", "25", "66"])
@pytest.mark.parametrize("stopped", [False, True])
def test_gamepad_enable_written_before_the_release_is_in_place_at_the_release(
    clock: str, stopped: bool
) -> None:
    """`gamepad_enable` is part of the control word: on the first clock out of reset bits 6:4
    are already the emulator's lines (one project clock late, as always), whatever `ui_in`
    has there. `ui_in` 0xA5 has bits 6:4 = 010; the emulator's lines are not constant."""
    period = PERIODS[clock]
    pins, lines = scenario(period, 5_050, stopped, "power_up", 1, gamepad=True)
    high = [k for k, p in enumerate(pins) if p[0] == 1 and k > 0]
    assert high
    for k in high:
        assert pins[k][1] == 1 and pins[k][2] & 0x8F == FINAL_UI & 0x8F
        assert (pins[k][2] >> 4) & 7 == lines[k - 1], f"clock {k}: {pins[k]} lines {lines[k - 1]}"
    # Not vacuous: the lines differed from the `ui_in` bits at some released clock.
    assert any(lines[k - 1] != (FINAL_UI >> 4) & 7 for k in high)


@pytest.mark.parametrize("clock", list(PERIODS))
def test_release_latency_when_nothing_is_in_flight(clock: str) -> None:
    """From the release being written (reset held long before) to the first project clock at
    which the design sees `rst_n` high: 5 project clocks after the word is picked up, which
    is two system clocks after the write."""
    period = PERIODS[clock]
    for offset in _offsets(period):
        slot = gate(ProjectSlot(None, Signal(), 100_000_000, 25_175_000, sim_clock=True))
        phase = {"released": False}
        seen: list[int] = []

        def rec():
            yield "passive"
            while True:
                if phase["released"]:
                    seen.append((yield slot.pin_rst_n))
                yield

        def sysgen():
            yield slot.ui_in.eq(FINAL_UI)
            yield slot.ena.eq(1)
            yield from waits(_settle(period))
            yield slot.rst_n.eq(1)
            phase["released"] = True
            yield from waits(_settle(period))

        run(slot, [sysgen()], [rec()], project=(period, offset))
        first = seen.index(1)
        bound = 5 + -(-2 * SYS_PERIOD // period) + 1
        assert 5 <= first <= bound, f"offset {offset}: released after {first} project clocks"
