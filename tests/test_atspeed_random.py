# SPDX-License-Identifier: Apache-2.0
"""A seeded random walk over the at-speed check's commands, with the review's invariants.

Commands: start (a distinct N every time), abort, bursts (start-abort-start on adjacent system
clocks, start-start), a changed N, a system reset in the middle of a check, the project clock
stopped and resumed (also right after a start, before the command is captured). Invariants:

- `done` is only ever high for the most recent start: its `crc` is the CRC of THAT start's N (all
  N distinct), and never after an abort or a system reset (one system clock of lag allowed);
- abort and system reset clear `busy` and `done` within one system clock;
- liveness: a start that is not interrupted reports `done` within a bound once the clock runs,
  so `busy` is never high for ever with the project clock running;
- the pins show a well-formed block per begun check and at least `MIN_GAP_CLOCKS` normal samples
  between blocks (`pin_check.assert_clean`; the record is split at system resets).

Fast tier: a few episodes at three clock ratios; slow tier: many more at 50 MHz (several
phases), nearly equal periods, 25.175 MHz, 2.5 MHz, 1 MHz and 0.4 MHz.
"""

from __future__ import annotations

import itertools
import random
import zlib

import pytest
from pin_check import NORMAL, SW_UI, assert_clean
from slot_sim import run
from test_atspeed import toy_bytes, waits
from test_atspeed_abort import Recorder, make, pulse, setup

from tt_harness.core.atspeed import MIN_GAP_CLOCKS

CLOCKS = {
    "50MHz_a": (20_000 + 100, 4_050),
    "50MHz_b": (20_000 + 100, 14_150),
    "50MHz_c": (20_000 + 100, 9_250),
    "nearly_equal": (20_040, 7_050),
    "25.175MHz": (39_722, 5_050),
    "2.5MHz": (400_000, 5_050),
    "1MHz": (1_000_000, 5_050),
    "0.4MHz": (2_500_000, 5_050),
}
LIVENESS_PROJECT_CLOCKS = 400  # a check of N <= 64 plus the handshakes, with a wide margin


class Model:
    def __init__(self) -> None:
        self.active: tuple[int, int] | None = None  # (index, N) of the most recent live start
        self.cleared_at = -10
        self.started_at = -10
        self.cycle = 0
        self.errors: list[str] = []
        self.dones = 0

    def clear(self) -> None:
        self.active = None
        self.cleared_at = self.cycle


def run_walk(clock: str, seed: int, episodes: int) -> tuple[int, int, list[str]]:
    """Run one random walk; return (number of violations, completed checks, the first few)."""
    rng = random.Random(seed)
    period, offset = CLOCKS[clock]
    per = max(1, period // 20_000)
    b = make()
    rec = Recorder(b)
    model = Model()
    resets: list[int] = []
    completed = [0]

    def watcher():
        yield "passive"
        prev_done = 0
        while True:
            model.cycle += 1
            done = yield b.check.done
            busy = yield b.check.busy
            crc = yield b.check.crc
            if done and not prev_done:
                model.dones += 1
            # the model is updated up to two clocks before the `start` pulse takes effect
            # and clears the previous `done`
            if done and model.cycle - model.started_at > 2:
                if model.active is None:
                    if model.cycle - model.cleared_at > 2:
                        model.errors.append(f"done with nothing started (cycle {model.cycle})")
                elif crc != zlib.crc32(toy_bytes(model.active[1])):
                    model.errors.append(
                        f"done with the wrong crc for start {model.active} (cycle {model.cycle}), crc is that of N={[k for k in range(300) if zlib.crc32(toy_bytes(k)) == crc]}"
                    )
                if busy:
                    model.errors.append("done and busy together")
            prev_done = done
            yield

    def wait_for_done(limit_clocks: int):
        for _ in range(limit_clocks * per):
            yield
            if (yield b.check.done):
                return True
        return False

    def sysgen():
        yield from setup(b, per)
        idx = 0

        def start(n: int):
            nonlocal idx
            idx += 1
            yield b.check.clocks.eq(n)
            yield
            yield from pulse(b.check.start)
            model.active = (idx, n)
            model.started_at = model.cycle

        for ep in range(episodes):
            n = 4 + (ep * 7) % 40 + ep // 40  # distinct for the first 1600 starts
            yield from start(n)
            kind = rng.choice(
                ["plain", "plain", "abort", "restart", "stop", "reset", "burst", "stop_early"]
            )
            delay = rng.randrange(0, 30) * per + rng.randrange(0, 3)
            if kind != "plain":
                yield from waits(delay)
            if kind == "abort":
                yield from pulse(b.check.abort)
                model.clear()
                yield
                if (yield b.check.busy) or (yield b.check.done):
                    model.errors.append("busy/done not clear one clock after abort")
                yield from waits(rng.randrange(0, 20) * per)
                continue
            if kind == "restart":
                n2 = n + 41
                yield from start(n2)
            elif kind == "burst":
                yield from pulse(b.check.abort)
                model.clear()
                yield b.check.clocks.eq(n + 83)
                yield from pulse(b.check.start)  # abort, then start on the very next clocks
                idx += 1
                model.active = (idx, n + 83)
                model.started_at = model.cycle
            elif kind in ("stop", "stop_early"):
                yield b.ce_project.eq(0)
                yield from waits(rng.randrange(5, 60) * per)
                yield b.ce_project.eq(1)
            elif kind == "reset":
                resets.append(len(rec.pins))  # the pin record is split here
                yield b.cd_sys.rst.eq(1)
                yield
                yield
                yield b.cd_sys.rst.eq(0)
                model.clear()
                yield
                if (yield b.check.busy) or (yield b.check.done):
                    model.errors.append("busy/done not clear after a system reset")
                yield b.slot.rst_n.eq(1)
                yield b.slot.ui_in.eq(SW_UI)
                yield from waits(40 + 8 * per)
                continue
            if model.active is not None:
                if (yield from wait_for_done(LIVENESS_PROJECT_CLOCKS)):
                    completed[0] += 1
                    yield from waits(2)
                else:
                    model.errors.append(f"no done for start {model.active} (cycle {model.cycle})")
                    yield from pulse(b.check.abort)
                    model.clear()
            yield from waits(rng.randrange(0, 6))
        # a final start must complete with the clock running
        yield b.ce_project.eq(1)
        yield from start(97)
        if not (yield from wait_for_done(LIVENESS_PROJECT_CLOCKS)):
            model.errors.append("final start never finished")
        yield from waits(12 * per)

    run(b, [sysgen(), watcher()], [rec.project()], project=(period, offset))
    # the pin record, in segments between system resets
    cuts = [0, *resets, len(rec.pins)]
    for lo, hi in itertools.pairwise(cuts):
        seg = rec.pins[lo:hi]
        if lo > 0:  # after a system reset: skip the reset itself and whatever it cut
            k = next((i for i, x in enumerate(seg) if x != NORMAL), len(seg))
            seg = seg[k:]
        if NORMAL in seg:
            try:
                assert_clean(seg, full_last=False)
            except AssertionError as e:
                model.errors.append(f"pins (segment {lo}:{hi}): {e}")
    return len(model.errors), completed[0], model.errors[:5]


@pytest.mark.parametrize(
    ("clock", "seed", "episodes"),
    [
        ("50MHz_a", 1, 8),
        ("25.175MHz", 2, 8),
        ("2.5MHz", 3, 3),
    ],
)
def test_random_walk_fast(clock: str, seed: int, episodes: int) -> None:
    errors, completed, detail = run_walk(clock, seed, episodes)
    assert errors == 0, detail
    assert completed >= 1
    assert MIN_GAP_CLOCKS >= 1


@pytest.mark.slow
@pytest.mark.parametrize("clock", list(CLOCKS))
@pytest.mark.parametrize("seed", [11, 12])
def test_random_walk_large(clock: str, seed: int) -> None:
    episodes = 12 if clock in ("1MHz", "0.4MHz") else 60
    errors, completed, detail = run_walk(clock, seed * 100 + len(clock), episodes)
    assert errors == 0, detail
    assert completed >= episodes // 6
