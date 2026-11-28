# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: B023  (generators defined in loops run within the iteration that made them)
"""`AtSpeedCheck` abort, system reset and restart behaviour (Migen simulator, two clocks).

The properties (see the docstring of `tt_harness/core/atspeed.py`):

- `abort` always works: `busy` and `done` are 0 one system clock after the pulse, whatever the
  project clock does;
- after an abort or a system reset nothing starts on the project side unless a new `start`
  follows; a start requested and not aborted runs when a stopped clock resumes;
- a check in progress when the abort or reset happens stops by itself within a few project
  clocks, its pins return to the slot's normal values, and its result never appears as
  `done`/`crc`, neither at once nor during a later check;
- a `start` given while the previous run is still unwinding is held (never lost, never
  doubled) and the `done` and `crc` seen always belong to the most recent `start`.

The software controls are `ena` 0, `ui_in` 0x11 and `rst_n` released; the check's reference
`ui_in` is 0x5A, so a pin that ever shows `ena` 1 or `ui_in` 0x5A while no check is active
means an override leaked.
"""

from __future__ import annotations

import zlib

import pytest
from migen import ClockDomain, Module, Signal
from pin_check import REF_UI, SW_UI, assert_clean
from slot_sim import gate, run
from test_atspeed import _Toy, toy_bytes, waits

from tt_harness.core.atspeed import UNWIND_CLOCKS, AtSpeedCheck
from tt_harness.core.slot import ProjectSlot

# Project clock settings: (period ps, first edge ps). Normal, slow (1 MHz), fast (about 50 MHz).
CLOCKS = {"normal": (39_700, 5_050), "slow": (1_000_000, 5_050), "fast": (20_100, 4_050)}


def cut_samples(period_ps: int) -> int:
    """Project clocks from an abort to the pins being normal: the command crosses (at worst a
    transfer in flight finishes first: 7 Tp + 4 Ts) and unwinds (`UNWIND_CLOCKS`), plus one for
    the alignment of the abort to the project clock."""
    return 7 + -(-4 * 20_000 // period_ps) + UNWIND_CLOCKS + 1


class Bench(Module):
    def __init__(self) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.submodules.slot = slot = ProjectSlot(
            None, Signal(), 100_000_000, 25_175_000, sim_clock=True
        )
        self.submodules.check = check = AtSpeedCheck(ui_in=REF_UI)
        slot.attach(check)
        self.submodules.toy = _Toy(slot)


def make(running: bool = True) -> Bench:
    return gate(Bench(), running)


def pulse(sig):
    yield sig.eq(1)
    yield
    yield sig.eq(0)


def setup(b: Bench, per: int = 1):
    """Release reset and set `ui_in`; wait until the pins have the normal values (`per` is the
    number of system clocks in a project clock)."""
    yield b.slot.rst_n.eq(1)
    yield b.slot.ui_in.eq(SW_UI)
    yield from waits(40 + 14 * per)


def stop_clock(b: Bench):
    """Stop the project clock with the design released and the pins at the software's values.
    (A bench that starts with the clock stopped releases reset only some clocks after the
    clock starts, because the release crosses in the control word: a check cut before that
    would be over before the first normal sample, where `assert_clean` starts judging.)"""
    assert ((yield b.slot.pin_rst_n), (yield b.slot.pin_ui_in)) == (1, SW_UI)
    yield b.ce_project.eq(0)
    yield from waits(3)


def start(b: Bench, n: int):
    yield b.check.clocks.eq(n)
    yield
    yield from pulse(b.check.start)


class Recorder:
    """Samples the project's pins at every project edge and `done`/`crc` at every sys edge."""

    def __init__(self, b: Bench) -> None:
        self.b = b
        self.pins: list[tuple[int, int, int]] = []  # (rst_n, ena, ui_in) per project edge
        self.done_crcs: list[int] = []  # crc at every sys edge with done high

    def project(self):
        yield "passive"
        while True:
            s = self.b.slot
            if (yield self.b.ce_project):  # a stopped project clock gives no samples
                self.pins.append(((yield s.pin_rst_n), (yield s.pin_ena), (yield s.pin_ui_in)))
            yield

    def sys(self):
        yield "passive"
        while True:
            if (yield self.b.check.done):
                self.done_crcs.append((yield self.b.check.crc))
            yield

    def overridden(self, since: int = 0) -> bool:
        return any(ena == 1 or ui == REF_UI for _, ena, ui in self.pins[since:])


def go(b: Bench, sysgen, rec: Recorder, project=CLOCKS["normal"]) -> None:
    run(b, [sysgen], [rec.project()], tick_gens=[], project=project)


def go2(b: Bench, sysgen, rec: Recorder, project, late: tuple = ()) -> None:
    # `sys`-rate recorder as a second sys generator
    run(b, [sysgen, rec.sys()], [rec.project()], project=project, late=late)


def wait_done(b: Bench, limit: int = 8_000):
    for n in range(limit):
        yield
        if (yield b.check.done):
            return n
    return None


@pytest.mark.parametrize("clock", ["normal", "fast", pytest.param("slow", marks=pytest.mark.slow)])
def test_abort_while_idle_does_nothing_and_the_next_check_is_right(clock: str) -> None:
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from pulse(b.check.abort)
        yield
        out["flags"] = ((yield b.check.busy), (yield b.check.done))
        yield from waits(100)
        out["leak"] = rec.overridden()
        yield from start(b, 30)
        out["n"] = yield from wait_done(b)
        out["crc"] = yield b.check.crc

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["flags"] == (0, 0) and not out["leak"]
    assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(30))


# Sys clocks after `start` at which the abort pulse is given: through the reset phase
# (first ~13 project clocks), counting, and the completion of an N = 40 check.
ABORT_AT = [0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 45, 60, 80, 100, 110, 120, 125, 130, 140]

# The representative subset for the fast run: reset phase, counting, completion.
ABORT_AT_FEW = [0, 3, 8, 20, 60, 125]


@pytest.mark.parametrize(
    ("clock", "n_first", "sweep"),
    [
        ("normal", 40, "few"),
        pytest.param("normal", 40, "all", marks=pytest.mark.slow),
        pytest.param("normal", 1, "all", marks=pytest.mark.slow),
        pytest.param("fast", 40, "all", marks=pytest.mark.slow),
        pytest.param("fast", 1, "all", marks=pytest.mark.slow),
        pytest.param("slow", 40, "all", marks=pytest.mark.slow),
        pytest.param("slow", 1, "all", marks=pytest.mark.slow),
    ],
)
def test_abort_at_every_stage_stops_the_check_and_its_result_never_appears(
    clock: str, n_first: int, sweep: str
) -> None:
    period = CLOCKS[clock][0]
    sys_clocks_per_project = max(1, period // 20_000)
    for k in ABORT_AT_FEW if sweep == "few" else ABORT_AT:
        if clock == "slow" and k not in (0, 3, 8, 15):
            continue  # a 1 MHz check is slow to simulate; these cover reset phase and counting
        b = make()
        rec, out = Recorder(b), {}

        def sysgen():
            yield from setup(b, sys_clocks_per_project)
            yield from start(b, n_first)
            yield from waits(k * (sys_clocks_per_project if clock == "slow" else 1))
            out["seen_before_abort"] = len(rec.done_crcs)  # a check may have finished first
            yield from pulse(b.check.abort)
            yield  # one system clock after the pulse
            out["flags"] = ((yield b.check.busy), (yield b.check.done))
            mark = len(rec.pins)
            yield from waits(40 * sys_clocks_per_project + 20)
            out["rec_at"] = mark
            out["late_done"] = yield b.check.done
            mark2 = len(rec.pins)
            yield from waits(100 * sys_clocks_per_project)
            out["leak_after"] = rec.overridden(mark + cut_samples(period))
            out["quiet"] = len(rec.pins) - mark2
            out["done_before_new"] = len(rec.done_crcs)
            yield from start(b, 25)  # a different N: its CRC differs
            out["n"] = yield from wait_done(b, 40_000)
            out["crc"] = yield b.check.crc
            yield from waits(10 * sys_clocks_per_project)  # let the check's pin block end

        go2(b, sysgen(), rec, CLOCKS[clock])
        assert out["flags"] == (0, 0), (k, out)  # within one system clock of the abort
        # Nothing new is reported after the abort (a result seen BEFORE it is that check's own).
        assert out["late_done"] == 0, (k, out)
        assert out["done_before_new"] <= out["seen_before_abort"] + 2, (k, out)
        assert not out["leak_after"], f"override still on after the unwind bound (k={k})"
        assert out["n"] is not None, (k, out)
        assert out["crc"] == zlib.crc32(toy_bytes(25)), (k, out)
        assert_clean(rec.pins)
        after = rec.done_crcs[out["done_before_new"] :]
        assert after and set(after) == {zlib.crc32(toy_bytes(25))}, (k, after[:3])


def test_abort_with_the_clock_stopped_then_started_never_reports_and_any_begin_is_cut() -> None:
    """Start, project clock stopped, abort, clock back.

    FINDING (the redesign's ruling expected "runs nothing"): the start word is already in the
    handshake's `hold` when the abort is given, and `hold` may not change while a transfer is
    in flight, so on resume that START word is delivered FIRST and the check begins; the
    `run = 0` word follows within the handshake latency and cuts it. What is guaranteed: no
    `done`, one well-formed cut block (reset low from its first clock), pins normal again
    within `cut_samples`.

    The clock is stopped only once the pins are at the software's values (`stop_clock`), so
    that the cut block is one the pin checker judges.
    """
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from stop_clock(b)
        yield from start(b, 40)
        yield from waits(50)
        out["busy_before"] = yield b.check.busy
        yield from pulse(b.check.abort)
        yield
        out["flags"] = ((yield b.check.busy), (yield b.check.done))
        yield b.ce_project.eq(1)  # the clock starts
        yield from waits(600)
        out["done"] = yield b.check.done

    go2(b, sysgen(), rec, CLOCKS["normal"])
    assert out["busy_before"] == 1 and out["flags"] == (0, 0) and out["done"] == 0
    assert len(rec.pins) > 100
    blocks = assert_clean(rec.pins, full_last=False)
    # One cut block: the word in flight begins the check, the `run = 0` word cuts it. Its
    # length is about 3 + ceil(3 Ts / Tp) override samples (measured 3 to 6 at 2.5 to 66 MHz),
    # and the design is held in reset for ALL of them (low samples == block length).
    bound = 3 + -(-3 * 20_000 // CLOCKS["normal"][0])
    assert len(blocks) == 1, blocks
    length, low = blocks[0]
    assert length <= bound and low == length, (blocks, bound)
    assert not rec.overridden(len(rec.pins) - 20), "the override was not released"
    assert rec.pins[-1] == (1, 0, SW_UI)
    assert rec.done_crcs == []


def test_a_start_requested_and_not_aborted_runs_when_the_stopped_clock_resumes() -> None:
    b = make(running=False)
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from start(b, 40)
        yield from waits(300)
        out["busy"] = yield b.check.busy
        out["done0"] = yield b.check.done
        yield b.ce_project.eq(1)
        out["n"] = yield from wait_done(b)
        out["crc"] = yield b.check.crc

    go2(b, sysgen(), rec, CLOCKS["normal"])
    assert out["busy"] == 1 and out["done0"] == 0
    assert out["crc"] == zlib.crc32(toy_bytes(40))


@pytest.mark.parametrize(
    "clock",
    [
        "normal",
        pytest.param("slow", marks=pytest.mark.slow),
        pytest.param("fast", marks=pytest.mark.slow),
    ],
)
def test_stop_the_clock_mid_check_abort_restart_the_clock_start_again(clock: str) -> None:
    b = make()
    rec, out = Recorder(b), {}
    per = max(1, CLOCKS[clock][0] // 20_000)

    def sysgen():
        yield from setup(b, per)
        yield from start(b, 200)
        yield from waits(8 * per)  # in the reset phase / early counting
        yield b.ce_project.eq(0)  # the clock stops mid-check
        yield from waits(100)
        yield from pulse(b.check.abort)
        yield
        out["flags"] = ((yield b.check.busy), (yield b.check.done))
        yield b.ce_project.eq(1)  # the clock restarts
        yield from waits(30 * per + 10)
        yield from start(b, 33)
        out["n"] = yield from wait_done(b, 60_000)
        out["crc"] = yield b.check.crc
        yield from waits(10 * per)  # let the check's pin block end

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["flags"] == (0, 0)
    assert out["n"] is not None and out["crc"] == zlib.crc32(toy_bytes(33))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(33))}
    assert_clean(rec.pins)


@pytest.mark.parametrize(
    ("clock", "stopped"),
    [
        ("normal", False),
        pytest.param("normal", True, marks=pytest.mark.slow),
        pytest.param("fast", False, marks=pytest.mark.slow),
        pytest.param("fast", True, marks=pytest.mark.slow),
    ],
)
def test_system_reset_mid_check_then_a_new_start_gives_the_new_checks_result(
    clock: str, stopped: bool
) -> None:
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from start(b, 200)  # the old check: a different N from the new one
        yield from waits(30)
        if stopped:
            yield b.ce_project.eq(0)
        yield b.cd_sys.rst.eq(1)  # system reset in the middle of the check
        yield
        yield
        yield b.cd_sys.rst.eq(0)
        yield
        out["flags"] = ((yield b.check.busy), (yield b.check.done))
        if stopped:
            yield from waits(50)
            yield b.ce_project.eq(1)
        yield b.slot.rst_n.eq(1)
        yield b.slot.ui_in.eq(SW_UI)
        yield from waits(60)
        out["old_leaked"] = len(rec.done_crcs)
        yield from start(b, 40)
        out["n"] = yield from wait_done(b, 20_000)
        out["crc"] = yield b.check.crc
        yield from waits(3000)  # the old check's completion must not show up later either

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["flags"] == (0, 0) and out["old_leaked"] == 0
    assert out["crc"] == zlib.crc32(toy_bytes(40)) != zlib.crc32(toy_bytes(200))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(40))}


@pytest.mark.parametrize(
    "clock",
    [
        "normal",
        pytest.param("slow", marks=pytest.mark.slow),
        pytest.param("fast", marks=pytest.mark.slow),
    ],
)
def test_two_starts_in_quick_succession_the_last_one_wins(clock: str) -> None:
    """A `start` is accepted at any time: it cancels a check in progress. The first start's
    result never appears; the second, with a different N, is the one reported."""
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b, max(1, CLOCKS[clock][0] // 20_000))
        yield b.check.clocks.eq(21)
        yield
        yield from pulse(b.check.start)
        yield b.check.clocks.eq(30)
        yield from pulse(b.check.start)
        out["n"] = yield from wait_done(b, 40_000)
        out["crc"] = yield b.check.crc
        yield from waits(4000)
        out["busy"] = yield b.check.busy

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["crc"] == zlib.crc32(toy_bytes(30)) and out["busy"] == 0
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(30))}
    assert_clean(rec.pins)


@pytest.mark.parametrize(
    ("clock", "gap"),
    [
        ("normal", 0),
        pytest.param("normal", 1, marks=pytest.mark.slow),
        pytest.param("normal", 2, marks=pytest.mark.slow),
        pytest.param("normal", 3, marks=pytest.mark.slow),
        pytest.param("normal", 5, marks=pytest.mark.slow),
        pytest.param("slow", 0, marks=pytest.mark.slow),
        pytest.param("slow", 1, marks=pytest.mark.slow),
        pytest.param("slow", 2, marks=pytest.mark.slow),
        pytest.param("slow", 3, marks=pytest.mark.slow),
        pytest.param("slow", 5, marks=pytest.mark.slow),
        pytest.param("fast", 0, marks=pytest.mark.slow),
        pytest.param("fast", 1, marks=pytest.mark.slow),
        pytest.param("fast", 2, marks=pytest.mark.slow),
        pytest.param("fast", 3, marks=pytest.mark.slow),
        pytest.param("fast", 5, marks=pytest.mark.slow),
    ],
)
def test_start_immediately_after_abort_is_held_and_runs_the_new_check(clock: str, gap: int) -> None:
    per = max(1, CLOCKS[clock][0] // 20_000)
    for k in (3, 12) if clock == "slow" else (3, 12, 40):
        b = make()
        rec, out = Recorder(b), {}

        def sysgen():
            yield from setup(b, per)
            yield from start(b, 200)
            yield from waits(k * per)
            yield from pulse(b.check.abort)
            yield from waits(gap)
            yield from start(b, 27)  # `clocks` is sampled at `start`
            yield
            out["busy"] = yield b.check.busy  # held or running, but visibly busy
            out["n"] = yield from wait_done(b, 80_000)
            out["crc"] = yield b.check.crc
            yield from waits(3000)

        go2(b, sysgen(), rec, CLOCKS[clock])
        assert out["busy"] == 1 and out["n"] is not None, (k, out)
        assert out["crc"] == zlib.crc32(toy_bytes(27)), (k, out)
        assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(27))}, (k, rec.done_crcs[:3])
        assert_clean(rec.pins)


def test_abort_in_the_same_cycle_as_start_wins() -> None:
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield b.check.clocks.eq(30)
        yield
        yield b.check.start.eq(1)
        yield b.check.abort.eq(1)
        yield
        yield b.check.start.eq(0)
        yield b.check.abort.eq(0)
        yield
        out["flags"] = ((yield b.check.busy), (yield b.check.done))
        yield from waits(500)

    go2(b, sysgen(), rec, CLOCKS["normal"])
    assert out["flags"] == (0, 0) and not rec.overridden() and rec.done_crcs == []


def test_power_up_state_pins_are_safe_until_software_releases_reset() -> None:
    """Before any system-side write: `rst_n` at the project pin is 0, `ena` 0, `ui_in` 0, and
    `rst_n` stays 0 until the software deasserts the reset request."""
    b = make()
    pins: list[tuple[int, int, int]] = []
    out = {}

    def projgen():
        yield "passive"
        while True:
            s = b.slot
            pins.append(((yield s.pin_rst_n), (yield s.pin_ena), (yield s.pin_ui_in)))
            yield

    def sysgen():
        yield from waits(60)  # no write at all
        out["before"] = len(pins)
        yield b.slot.rst_n.eq(1)
        yield from waits(40)

    run(b, [sysgen()], [projgen()])
    assert out["before"] > 20
    assert set(pins[: out["before"]]) == {(0, 0, 0)}
    assert pins[-1][0] == 1  # released only after the software did


@pytest.mark.parametrize("clock", ["normal", pytest.param("slow", marks=pytest.mark.slow)])
def test_aborted_and_restarted_with_the_clock_stopped_runs_only_the_last_start(
    clock: str,
) -> None:
    """Frozen mid-check, abort, start, abort, start: the toggle of two launches must not cancel
    and the old check must not continue with its old N."""
    b = make()
    rec, out = Recorder(b), {}
    per = max(1, CLOCKS[clock][0] // 20_000)

    def sysgen():
        yield from setup(b, per)
        yield from start(b, 200)
        yield from waits(10 * per)  # the project has begun
        yield b.ce_project.eq(0)
        yield from waits(100)
        yield from pulse(b.check.abort)
        yield from start(b, 21)
        yield from waits(30)
        yield from pulse(b.check.abort)
        yield from start(b, 33)
        yield from waits(30)
        out["busy"] = yield b.check.busy
        yield b.ce_project.eq(1)
        out["n"] = yield from wait_done(b, 60_000)
        out["crc"] = yield b.check.crc
        yield from waits(500 * per)

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["busy"] == 1
    assert out["crc"] == zlib.crc32(toy_bytes(33))
    assert set(rec.done_crcs) == {zlib.crc32(toy_bytes(33))}
    assert_clean(rec.pins)


@pytest.mark.parametrize(
    "clock",
    [
        "normal",
        pytest.param("slow", marks=pytest.mark.slow),
        pytest.param("fast", marks=pytest.mark.slow),
    ],
)
def test_start_right_after_done_never_shows_the_old_result_as_the_new_one(clock: str) -> None:
    b = make()
    rec, out = Recorder(b), {}
    per = max(1, CLOCKS[clock][0] // 20_000)

    def sysgen():
        yield from setup(b)
        yield from start(b, 20)
        out["n1"] = yield from wait_done(b, 60_000)
        out["crc1"] = yield b.check.crc
        yield from start(b, 47)  # the very next cycle: the old `fin` is still on its way
        yield  # `start` has been accepted and has cleared `done`; samples so far are the old one's
        out["seen_old"] = len(rec.done_crcs)
        out["n2"] = yield from wait_done(b, 60_000)
        out["crc2"] = yield b.check.crc
        yield from waits(100 * per)

    go2(b, sysgen(), rec, CLOCKS[clock])
    assert out["crc1"] == zlib.crc32(toy_bytes(20))
    assert out["crc2"] == zlib.crc32(toy_bytes(47))
    # After the second start, `done` was never high with the first check's CRC.
    assert set(rec.done_crcs[out["seen_old"] :]) == {zlib.crc32(toy_bytes(47))}


def test_abort_cuts_a_long_check_and_the_override_leaves_the_pins_within_the_bound() -> None:
    """N is too large for the check to end by itself: only the abort can release the pins."""
    b = make()
    rec, out = Recorder(b), {}
    period = CLOCKS["normal"][0]

    def sysgen():
        yield from setup(b)
        yield from start(b, 5000)
        yield from waits(40)  # well into the count
        out["active"] = rec.overridden(len(rec.pins) - 4)
        out["mark"] = len(rec.pins)
        yield from pulse(b.check.abort)
        yield from waits(30)
        out["end"] = len(rec.pins)

    go2(b, sysgen(), rec, CLOCKS["normal"])
    assert out["active"], "the check was not running when it was aborted"
    bound = cut_samples(period)
    assert out["end"] - out["mark"] > bound
    assert not rec.overridden(out["mark"] + bound), "the abort did not cut the running check"
    assert rec.pins[-1] == (1, 0, SW_UI)
    blocks = assert_clean(rec.pins, full_last=False)
    assert len(blocks) == 1 and blocks[0][1] == 10  # cut after the reset phase, in the count


def test_a_run_0_word_with_a_new_seq_never_begins_a_check() -> None:
    """The project's first sight of a `seq` can be its abort word: with the clock stopped a
    start word is in flight, a second start and an abort follow, and on resume the word in
    flight is delivered first and then the newest one, `run = 0` with the second `seq`. Only
    the first start may begin (and is cut); the `run = 0` word must not begin a second one."""
    b = make()
    rec, out = Recorder(b), {}

    def sysgen():
        yield from setup(b)
        yield from stop_clock(b)
        yield from start(b, 40)
        yield from waits(30)
        yield from start(b, 41)  # a second `seq` never delivered with `run = 1`
        yield from waits(30)
        yield from pulse(b.check.abort)
        yield from waits(30)
        yield b.ce_project.eq(1)
        yield from waits(500)
        out["done"] = yield b.check.done

    go2(b, sysgen(), rec, CLOCKS["normal"])
    assert out["done"] == 0 and rec.done_crcs == []
    blocks = assert_clean(rec.pins, full_last=False)
    assert len(blocks) == 1, blocks  # one block for the start word in flight, none for run = 0
    assert rec.pins[-1] == (1, 0, SW_UI)
