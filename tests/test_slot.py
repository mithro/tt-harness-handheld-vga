# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: B023  (the generators defined in the loops run within the iteration that made them)
"""`ProjectSlot` tests: the only place where the `sys` and `project` clock domains meet.

What these tests can and cannot show. They run in Migen's simulator with two unrelated
clocks (`sys` 20 ns; `project` 39.7 ns, 27.3 ns, 100 ns or 1 us, at many phase offsets).
That simulator is zero-delay and event-ordered, so it shows that the PROTOCOLS are
functionally right at every relative phase: no lost or mixed value, the right latency, the
reset released only just after a project clock edge and asserted at once. It cannot show
anything about METASTABILITY (a synchroniser's first flop is simply a flop here, and it
cannot show bit skew on a bus), nor timing closure. Those rest on the structures being the
standard ones (two-flop `MultiReg`; a toggle handshake whose data is stable before the
request is visible; an asynchronous-assert, synchronous-release reset chain), each of which
is stated in `tt_harness/core/cdc.py` and `tt_harness/core/slot.py`, and on the netlist gate
(`tt_harness/input_gate.py`) and constraints of the later build task. The reset cell's
exact asynchronous behaviour, as Migen emits it, is also run in Verilator
(`tests/test_atspeed.py`).

A stopped project clock is modelled by a clock enable on every project-domain register
(`tests/slot_sim.py`).
"""

from __future__ import annotations

import pytest
from migen import Signal
from migen.fhdl.tools import list_targets
from migen.genlib.cdc import MultiReg
from slot_sim import SYS_PERIOD, TICK_PERIOD, edge_times, gate, run
from test_gamepad import _expected_levels

from tt_harness.compare import inputs
from tt_harness.core.cdc import AsyncClearFlop
from tt_harness.core.gamepad import FRAME_SLOTS
from tt_harness.core.slot import CLOCK_RUNNING_TIMEOUT, CTL_ENA, CTL_GAMEPAD, CTL_UI_IN, ProjectSlot

SYS_HZ = 50_000_000
PERIODS = (39_700, 27_300)  # project clock periods unrelated to 20 ns, in ps
OFFSETS = [50 + 2_000 * i for i in range(10)]  # first project edge, ps; never on a sys edge


def make(clock_hz: int = 25_175_000, running: bool = True, **kw) -> ProjectSlot:
    """A slot on the simulator: no project instance, clock driven by the test bench."""
    slot = ProjectSlot(None, Signal(), 100_000_000, clock_hz, sim_clock=True, **kw)
    return gate(slot, running)


def sys_edge(c: int) -> int:
    """Time of the c-th sys rising edge (c = 0 is the first)."""
    return SYS_PERIOD // 2 + c * SYS_PERIOD


def waits(n: int):
    for _ in range(n):
        yield


# Controls -------------------------------------------------------------------------------------


def test_controls_reach_the_project() -> None:
    slot = make()
    seen: dict[str, int] = {}

    def sysgen():
        yield
        seen["ena0"] = yield slot.pin_ena
        seen["rst0"] = yield slot.pin_rst_n
        seen["ui0"] = yield slot.pin_ui_in
        yield slot.ena.eq(1)
        yield slot.rst_n.eq(1)
        yield slot.ui_in.eq(0xA5)
        yield from waits(80)
        seen["ena"] = yield slot.pin_ena
        seen["rst"] = yield slot.pin_rst_n
        seen["ui"] = yield slot.pin_ui_in
        yield slot.ena.eq(0)
        yield slot.ui_in.eq(0x3C)
        yield from waits(80)
        seen["ena2"] = yield slot.pin_ena
        seen["ui2"] = yield slot.pin_ui_in
        seen["rst2"] = yield slot.pin_rst_n

    run(slot, [sysgen()])
    # Powers up safe: design in reset, disabled, inputs 0.
    assert (seen["ena0"], seen["rst0"], seen["ui0"]) == (0, 0, 0)
    assert (seen["ena"], seen["rst"], seen["ui"]) == (1, 1, 0xA5)
    assert (seen["ena2"], seen["ui2"], seen["rst2"]) == (0, 0x3C, 1)


# Multi-bit handshake --------------------------------------------------------------------------

# (gap in sys clocks before the write, which of the two values): back-to-back writes, short
# gaps, gaps about the length of a transfer, long gaps, repeated.
GAPS = [0, 0, 0, 0, 1, 1, 2, 3, 5, 7, 9, 11, 13, 15, 17, 20, 25, 33, 0, 0, 40, 60, 0, 1, 0, 2]


def _writes(zero: int, one: int, rounds: int = 2) -> list[int]:
    return [one if i % 2 == 0 else zero for i in range(len(GAPS) * rounds)]


def _collapse(seq: list[int]) -> list[int]:
    out: list[int] = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def _is_subsequence(small: list[int], big: list[int]) -> bool:
    it = iter(big)
    return all(any(v == w for w in it) for v in small)


@pytest.mark.parametrize(
    ("period", "which"),
    [
        (39_700, "ui_in"),
        (27_300, "gamepad_buttons"),
        pytest.param(27_300, "ui_in", marks=pytest.mark.slow),
        pytest.param(39_700, "gamepad_buttons", marks=pytest.mark.slow),
    ],
)
def test_multibit_control_is_never_mixed(period: int, which: str) -> None:
    """The project only ever holds written values, in order, and ends on the last one."""
    zero, one = (0x00, 0xFF) if which == "ui_in" else (0x000000, 0xFFFFFF)
    # `ui_in` is the top field of the control word (the other fields stay 0 here).
    crossing, shift = ("ctl_x", CTL_UI_IN) if which == "ui_in" else ("buttons_x", 0)
    values = _writes(zero, one, rounds=1)
    gaps = GAPS
    delivered_everywhere: set[tuple[int, int]] = set()
    for offset in OFFSETS:
        if offset >= period:
            continue
        slot = make()
        x = getattr(slot, crossing)
        port = getattr(slot, which)
        seen: list[int] = []
        broken: list[str] = []

        def sysgen():
            for gap, value in zip(gaps, values, strict=True):
                yield from waits(gap)
                yield port.eq(value)
            yield from waits(120)

        def projgen():
            while True:
                word = yield x.dst
                assert word & ((1 << shift) - 1) == 0
                seen.append(word >> shift)
                yield

        def monitor():
            # Protocol invariants, sampled every tick: `hold` changes only when no
            # transfer is in flight (and with the request), `dst` only while one is.
            prev = None
            while True:
                cur = ((yield x.hold), (yield x.busy), (yield x.dst), (yield x.req))
                if prev is not None:
                    if cur[0] != prev[0] and (prev[1] or cur[3] == prev[3]):
                        broken.append(f"hold changed while busy or without a request: {prev} {cur}")
                    if cur[2] != prev[2] and not prev[1]:
                        broken.append(f"dst changed with no transfer in flight: {prev} {cur}")
                prev = cur
                yield

        run(
            slot, [sysgen()], [_passive(projgen())], [_passive(monitor())], project=(period, offset)
        )
        assert not broken, broken[:3]
        # Every delivered value is one that was written (or the power-up value) ...
        assert set(seen) <= {zero, one}, f"offset {offset}: mixed value {set(seen)}"
        # ... in order: what the project saw is a subsequence of what was written ...
        written = _collapse([zero, *values])
        assert _is_subsequence(_collapse(seen), written), f"offset {offset}: out of order"
        # ... and the newest one always arrives.
        assert seen[-1] == values[-1], f"offset {offset}: last write not delivered"
        # Not vacuous: values really changed hands more than a few times.
        assert len(_collapse(seen)) >= 6, f"offset {offset}: {len(_collapse(seen))} changes"
        delivered_everywhere.add((period, offset))
    assert len(delivered_everywhere) >= 8


def _passive(gen):
    yield "passive"
    yield from gen


@pytest.mark.parametrize("period", [39_700, pytest.param(100_000, marks=pytest.mark.slow)])
def test_multibit_latency_is_within_the_stated_bound(period: int) -> None:
    """Idle write to project: at most Ts + 3 Tp (plus the final register: + Tp); a write that
    lands just after a transfer started: at most 6 Tp + 3 Ts (+ Tp). Measured at 2 ns."""
    ts, tp = SYS_PERIOD, period
    idle_bound = ts + 4 * tp + 2 * TICK_PERIOD
    busy_bound = 7 * tp + 4 * ts + 2 * TICK_PERIOD
    worst = {"idle": 0, "busy": 0}
    for offset in OFFSETS[::3]:
        slot = make()
        pin_log: list[tuple[int, int]] = []  # (time, pin_ui_in) at every tick
        writes: list[tuple[int, int, str]] = []  # (time visible, value, kind)
        clock = {"t": 0}

        def tickgen():
            while True:
                clock["t"] += TICK_PERIOD
                pin_log.append((clock["t"], (yield slot.pin_ui_in)))
                yield

        def sysgen():
            c = 0

            def write(value, kind):
                nonlocal c
                yield slot.ui_in.eq(value)
                c += 1  # visible on the port after the edge now in progress
                writes.append((sys_edge(c - 1), value, kind))
                yield

            yield from waits(3)
            c = 3
            for k in range(6):  # sparse writes: idle path
                yield from write(0x10 + k, "idle")
                yield from waits(150)
                c += 150
            for k in range(4):  # burst: the second write lands while the first is in flight
                yield from write(0x80 + 2 * k, "first")
                yield from waits(1 + k)
                c += 1 + k
                yield from write(0x81 + 2 * k, "busy")
                yield from waits(250)
                c += 250

        run(slot, [sysgen()], tick_gens=[_passive(tickgen())], project=(period, offset))
        for t_w, value, kind in writes:
            if kind == "first":
                continue
            t_pin = next(t for t, v in pin_log if t > t_w and v == value)
            latency = t_pin - t_w
            key = "idle" if kind == "idle" else "busy"
            worst[key] = max(worst[key], latency)
            assert latency <= (idle_bound if key == "idle" else busy_bound), (
                f"{kind} write latency {latency} ps at offset {offset}"
            )
    print(f"worst idle {worst['idle']} ps, worst busy {worst['busy']} ps (Tp {tp}, Ts {ts})")


# Reset ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("period", [39_700, pytest.param(27_300, marks=pytest.mark.slow)])
def test_reset_release_is_synchronous_at_every_phase(period: int) -> None:
    """`rst_n` at the pin rises just after a project clock edge and falls at once on
    assertion. Which edge: the release bit travels in the control word, so with nothing in
    flight the request falls one system clock after the write, the word is picked up one
    system clock later (edge `c + 1` below), and the pin rises at the fifth project edge after
    that (two synchroniser flops, the capture, the two reset flops)."""
    for offset in OFFSETS:
        if offset >= period:
            continue
        slot = make()
        log: list[tuple[int, int]] = []
        clock = {"t": 0}
        cycles = {"release": [], "assert": []}

        def tickgen():
            while True:
                clock["t"] += TICK_PERIOD
                log.append((clock["t"], (yield slot.pin_rst_n)))
                yield

        def sysgen():
            c = 0
            for round_ in range(3):
                for _ in range(40 + 3 * round_):  # release lands at a different sys phase
                    yield
                    c += 1
                yield slot.rst_n.eq(1)
                cycles["release"].append(c + 1)  # rst_req falls at edge c + 1
                for _ in range(60):
                    yield
                    c += 1
                yield slot.rst_n.eq(0)
                cycles["assert"].append(c + 1)
                for _ in range(15):
                    yield
                    c += 1

        run(slot, [sysgen()], tick_gens=[_passive(tickgen())], project=(period, offset))
        until = clock["t"]
        edges = edge_times(period, offset, until)
        rises = [t for (t, v), (_, p) in zip(log[1:], log, strict=False) if v == 1 and p == 0]
        falls = [t for (t, v), (_, p) in zip(log[1:], log, strict=False) if v == 0 and p == 1]
        assert len(rises) == 3 and len(falls) == 3, (offset, rises, falls)
        for c, tr in zip(cycles["release"], rises, strict=True):
            t_word = sys_edge(c + 1)  # the word with the release bit is picked up here
            e5 = [e for e in edges if e > t_word][4]
            assert 0 <= tr - e5 <= 2 * TICK_PERIOD, (
                f"offset {offset}: rose at {tr}, fifth edge after the pick-up {e5}"
            )
        for c, tf in zip(cycles["assert"], falls, strict=True):
            assert 0 <= tf - sys_edge(c) <= 2 * TICK_PERIOD, f"offset {offset}: fell at {tf}"
        # Never high while the request is asserted (no clock needed to see it fall).
        for t, v in log:
            for c in cycles["assert"]:
                if sys_edge(c) + 2 * TICK_PERIOD <= t < sys_edge(c) + 15 * SYS_PERIOD:
                    assert v == 0


def test_reset_asserts_with_the_project_clock_stopped() -> None:
    """The clock stops, then the request: `rst_n` falls with no clock edge at all, stays low
    after the request is released, and rises after the clock restarts: the word with the
    release bit 0 (in flight) arrives in 3 project clocks and is acknowledged, the request
    falls and the word with the release bit follows (about 5 system clocks), and that takes
    5 project clocks (and one for phase) to the pin."""
    slot = make()
    log: list[tuple[int, int]] = []
    clock = {"t": 0}
    marks: dict[str, int] = {}

    def tickgen():
        while True:
            clock["t"] += TICK_PERIOD
            log.append((clock["t"], (yield slot.pin_rst_n)))
            yield

    def sysgen():
        yield slot.rst_n.eq(1)
        yield from waits(40)
        assert (yield slot.pin_rst_n) == 1
        yield slot.ce_project.eq(0)  # the project clock stops
        yield from waits(5)
        yield slot.rst_n.eq(0)
        yield from waits(3)
        marks["low"] = yield slot.pin_rst_n
        yield slot.rst_n.eq(1)  # release the request: the clock is still stopped
        yield from waits(100)
        marks["still_low"] = yield slot.pin_rst_n
        yield slot.ce_project.eq(1)  # the clock restarts
        marks["restart"] = clock["t"]
        yield from waits(40)
        marks["high"] = yield slot.pin_rst_n

    run(slot, [sysgen()], tick_gens=[_passive(tickgen())])
    assert marks["low"] == 0, "rst_n did not fall asynchronously with the clock stopped"
    assert marks["still_low"] == 0, "rst_n was released with no clock"
    assert marks["high"] == 1
    rise = next(
        t
        for (t, v), (_, p) in zip(log[1:], log, strict=False)
        if v == 1 and p == 0 and t > marks["restart"]
    )
    assert rise - marks["restart"] <= 9 * 39_700 + 5 * SYS_PERIOD + 2 * TICK_PERIOD


# Gamepad mux ----------------------------------------------------------------------------------


def test_gamepad_mux() -> None:
    """With `gamepad_enable` bits 6:4 of the project's `ui_in` are latch/clock/data from the
    emulator, one project clock later; otherwise (and everywhere else) they are `ui_in`."""
    word = 0x008100
    slot = make(clock_hz=1_000_000)  # slow declared clock: a frame is 1667 clocks
    emu = slot.gamepad
    pins: list[int] = []
    emu_out: list[int] = []
    after_off: list[int] = []

    def sysgen():
        yield slot.rst_n.eq(1)
        yield slot.ui_in.eq(0x8F | 0x20)  # bit 5 high, 4 and 6 low, 7 and 3:0 high
        yield slot.gamepad_buttons.eq(word)
        yield slot.gamepad_enable.eq(1)
        # About 3.4 frames of 1667 project clocks of 39.7 ns, in system clocks of 20 ns.
        yield from waits(int(3.4 * 1667 * 39_700 / SYS_PERIOD))
        yield slot.gamepad_enable.eq(0)
        yield from waits(100)
        after_off.append((yield slot.pin_ui_in))

    def projgen():
        while True:
            pin = yield slot.pin_ui_in
            pins.append(pin)
            e = (yield emu.latch) | ((yield emu.clock) << 1) | ((yield emu.data) << 2)
            emu_out.append(e)
            yield

    run(slot, [sysgen()], [_passive(projgen())], project=(39_700, 5_050))
    # Bits 3:0 and 7 always come from ui_in.
    # (after the power-up values have been replaced by the written ones)
    assert {p & 0x8F for p in pins[len(pins) // 8 :]} == {0x8F}
    gp_bits = [(p >> 4) & 7 for p in pins]
    # While the gamepad is enabled, the pin is the emulator's output one project clock later.
    # (Enabling and disabling cross, so compare only the middle of the run.)
    mid = range(len(pins) // 4, 3 * len(pins) // 4)
    assert all(gp_bits[k] == emu_out[k - 1] for k in mid)
    # The levels are the real runner's levels for `word`, and not those of zero buttons.
    expected = _expected_levels(word)
    lv = _collapse([(g & 1) << 4 | (g >> 1 & 1) << 5 | (g >> 2 & 1) << 6 for g in gp_bits])
    assert _contains(lv, expected), "the frame for the written buttons never appeared"
    assert _expected_levels(0) != expected
    # Disabled: bits 6:4 are the ui_in ones again (bit 5 high only).
    assert after_off == [0x8F | 0x20]
    assert FRAME_SLOTS == 75


def _contains(seq: list[int], sub: list[int]) -> bool:
    n = len(sub)
    return any(seq[i : i + n] == sub for i in range(len(seq) - n + 1))


# A stopped project clock ----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["clock_hz_zero", "clock_never_toggles"])
def test_stopped_clock_never_blocks_the_system_side(how: str) -> None:
    """Writes complete, status reads 0, and nothing in `sys` waits for the project."""
    if how == "clock_hz_zero":
        slot = gate(ProjectSlot(None, Signal(), 100_000_000, 0))
    else:
        slot = make()
    done: dict[str, int] = {}

    def sysgen():
        cycles = 0
        for value in (0x11, 0x22, 0x33):
            yield slot.ui_in.eq(value)
            yield slot.gamepad_buttons.eq(value * 0x10101)
            yield slot.ena.eq(value & 1)
            yield slot.gamepad_enable.eq(1)
            yield slot.clock_enable.eq(1)
            yield slot.rst_n.eq(value & 1)
            for _ in range(30):
                yield
                cycles += 1
        done["cycles"] = cycles
        done["locked"] = yield slot.locked
        done["running"] = yield slot.clock_running
        done["rst_pin"] = yield slot.pin_rst_n
        done["ctl_x_busy"] = yield slot.ctl_x.busy
        # No system-side register is stuck: the latest value is held, ready to go.
        done["held"] = yield slot.ctl_x.hold

    run(slot, [sysgen()], project=None)
    assert done["cycles"] == 90
    assert done["locked"] == 0 and done["running"] == 0
    assert done["rst_pin"] == 0
    assert done["ctl_x_busy"] == 1  # a transfer waits for the clock; the writer never does
    # The in-flight word carries the first values written (release bit 0); see below.
    assert done["held"] == 0x11 << CTL_UI_IN | 1 << CTL_ENA | 1 << CTL_GAMEPAD


def test_values_written_while_stopped_arrive_after_the_clock_starts() -> None:
    slot = make(running=False)  # the clock is stopped from the start
    out: dict[str, int] = {}

    def sysgen():
        yield slot.rst_n.eq(1)
        yield slot.ena.eq(1)
        for value in (0x11, 0x22, 0x33, 0x44):
            yield slot.ui_in.eq(value)
            yield from waits(10)
        yield from waits(200)
        out["before"] = (yield slot.pin_ui_in), (yield slot.pin_ena), (yield slot.pin_rst_n)
        out["status"] = (yield slot.locked), (yield slot.clock_running)
        yield slot.ce_project.eq(1)  # the clock starts
        for n in range(1, 60):
            yield
            if (yield slot.pin_ui_in) == 0x44:
                out["cycles_to_latest"] = n
                break
        yield from waits(20)
        out["after"] = (yield slot.pin_ui_in), (yield slot.pin_ena), (yield slot.pin_rst_n)
        out["status2"] = (yield slot.locked), (yield slot.clock_running)

    run(slot, [sysgen()])
    assert out["before"] == (0, 0, 0) and out["status"] == (0, 0)
    assert out["after"] == (0x44, 1, 1)
    # The in-flight value (0x11) lands within ~3 Tp, the newest (0x44) within ~6 Tp + 3 Ts +
    # one more for the final register: 8 clocks of 39.7 ns plus 3 of 20 ns, in sys clocks.
    assert out["cycles_to_latest"] <= 20, out
    assert out["status2"][1] == 1


@pytest.mark.parametrize(
    "period", [pytest.param(1_000_000, marks=pytest.mark.slow), 100_000, 20_000]
)
def test_clock_running_reads_1_for_any_clock_and_0_when_stopped(period: int) -> None:
    slot = make()
    log: list[int] = []

    def sysgen():
        for _ in range(CLOCK_RUNNING_TIMEOUT * 4):
            yield
            log.append((yield slot.clock_running))
        yield slot.ce_project.eq(0)
        for _ in range(CLOCK_RUNNING_TIMEOUT * 2 + 200):
            yield
            log.append((yield slot.clock_running))

    run(slot, [sysgen()], project=(period, 5_050))
    n = CLOCK_RUNNING_TIMEOUT * 4
    settled = log[CLOCK_RUNNING_TIMEOUT:n]  # after it first sees the clock
    assert all(v == 1 for v in settled), "clock_running dropped with a running clock"
    assert log[-1] == 0, "clock_running still 1 long after the clock stopped"
    assert log[0] == 0
    # `locked` follows the clock in simulation (there is no MMCM).
    assert CLOCK_RUNNING_TIMEOUT >= 4 * 50  # alive toggles every 4 clocks; 1 MHz = 50 sys/clock


# Fragment-level rules -------------------------------------------------------------------------


def _real_slot() -> ProjectSlot:
    return ProjectSlot("tt_um_example", Signal(), 100_000_000, 25_175_000)


def test_project_inputs_are_project_domain_registers() -> None:
    """Every project input but the clock is a constant or a direct `project` register output.
    A new input fails here until it is classified."""
    slot = _real_slot()
    found = inputs.project_inputs(slot, "project")
    assert set(found) == {"clk", "rst_n", "ena", "ui_in", "uio_in"}
    for port, source in found.items():
        if port == inputs.CLOCK_PORT:
            continue
        assert source.glitch_free, f"{port}: {source.kind}: {source.detail}"
        assert source.kind == "constant" or source.domain == "project", (
            f"{port} is a register of domain {source.domain!r}, not 'project'"
        )
    assert found["uio_in"].kind == "constant"
    for port in ("rst_n", "ena", "ui_in"):
        assert found[port].kind == "register" and found[port].domain == "project"


def _sync_targets(fragment) -> dict[str, set]:
    return {d: list_targets(stmts) for d, stmts in fragment.sync.items()}


def test_reset_request_and_every_crossing_source_is_a_direct_sys_register() -> None:
    """Each signal that crosses INTO `project` (the reset request, the single-bit levels, the
    handshake's hold and request) is assigned only in a `sys` synchronous block; each
    signal crossing back is assigned only in a `project` one (or is the MMCM's own LOCKED)."""
    slot = _real_slot()
    fragment = slot.get_fragment()
    sync = _sync_targets(fragment)
    comb = list_targets(fragment.comb)

    def sys_register(sig) -> bool:
        return (
            sig in sync["sys"]
            and sig not in comb
            and not any(sig in t for d, t in sync.items() if d != "sys")
        )

    def project_register(sig) -> bool:
        return (
            sig in sync["project"]
            and sig not in comb
            and not any(sig in t for d, t in sync.items() if d != "project")
        )

    multiregs = [s for s in fragment.specials if isinstance(s, MultiReg)]
    flops = [s for s in fragment.specials if isinstance(s, AsyncClearFlop)]
    into_project = [m for m in multiregs if m.odomain == "project"]
    into_sys = [m for m in multiregs if m.odomain == "sys"]
    assert len(into_project) == 2  # the two handshakes' requests: nothing else crosses
    for m in into_project:
        assert sys_register(m.i), f"a MultiReg into project from a non-sys-register {m.i}"
    for f in flops:
        assert sys_register(f.clear), "the asynchronous reset request is not a sys register"
    assert {f.cd for f in flops} == {"project"} and len(flops) == 2
    for x in (slot.ctl_x, slot.buttons_x):
        assert sys_register(x.hold) and sys_register(x.req)
        assert project_register(x.ack_o) and project_register(x.dst)
    locked_src = slot.clock.locked
    for m in into_sys:
        assert project_register(m.i) or m.i is locked_src, f"unexpected source {m.i}"
    # The MMCM clock-enable input is a direct sys register too.
    enable_drivers = [s for s in fragment.comb if slot.clock.enable in list_targets([s])]
    assert len(enable_drivers) == 1 and sys_register(enable_drivers[0].r)
    # Nothing from `sys` is used directly in a `project` register: the slot's own project
    # statements read no signal that is assigned in `sys` (hold is the one allowed read).
    sys_signals = sync["sys"]
    allowed = {slot.ctl_x.hold, slot.buttons_x.hold}
    from migen.fhdl.tools import list_signals

    for stmts in fragment.sync.get("project", []):
        used = list_signals(stmts) - list_targets([stmts])
        direct = (used & sys_signals) - allowed
        assert not direct, f"a project register reads sys signals {direct} directly"


def test_clock_running_threshold_just_above_and_just_below() -> None:
    """Threshold f = ALIVE_DIVIDE / (CLOCK_RUNNING_TIMEOUT x Ts) = 4 / (512 x 20 ns) = 0.39 MHz:
    a clock of 0.42 MHz (gap between flips 480 system clocks) reads 1 throughout; one of
    0.36 MHz (560 system clocks) reads 0 between flips."""
    runs = {}
    for name, period in (("above", 2_400_000), ("below", 2_800_000)):
        slot = make()
        log: list[int] = []

        def sysgen():
            for _ in range(CLOCK_RUNNING_TIMEOUT * 4):
                yield
                log.append((yield slot.clock_running))

        run(slot, [sysgen()], project=(period, 5_050))
        runs[name] = log[CLOCK_RUNNING_TIMEOUT * 2 :]  # after the clock has been seen
    assert set(runs["above"]) == {1}
    assert 0 in runs["below"] and 1 in runs["below"]
