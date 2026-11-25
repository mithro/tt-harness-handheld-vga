# SPDX-License-Identifier: Apache-2.0
"""At-speed check: run the project at full speed under the reference stimulus and CRC its outputs.

The CPU side (`sys`) writes `clocks` (N), pulses `start`, and `done` rises with `crc`, the zlib
CRC-32 of the first N bytes of the reference dump, byte k being `uo_out` after the k-th rising
edge counted from 0 = the first rising edge at which the design sees `rst_n` low
(`tt_harness/compare/recorder.py`, `tt_harness/reference.py`). So `crc == zlib.crc32(dump[:N])`.
The check runs entirely in the project clock domain and never stops or slows the clock.

Ports. `sys`: `start` (one-cycle pulse; ignored while `busy`; N = `clocks` is sampled at
the `start` pulse), `abort` (one-cycle pulse; wins over a `start` in the same cycle),
`busy` (a start is pending or running), `done` (level: the most recent start finished;
cleared by `start` and `abort`), `crc` (valid while `done`). `project`: `uo_out` (input),
`override`, `override_rst`, `override_ui_in` (outputs, direct flip-flop outputs; see
`ProjectSlot.attach`). Constructor `ui_in` is the reference's fixed `ui_in`; `uio_in` is the
slot's constant 0, which is what every pool design's reference uses. N = 0 gives the CRC of
nothing (0). `sys_hz` and `min_project_hz` size the launch delay (below).

Guarantee at the project's pins. Each accepted `start` gives the design exactly ONE clean
sequence: the slot's normal values, then the override (`ena` 1, reference `ui_in`) with
`rst_n` high for one clock, low for exactly 10 clocks, high again while counting, then the
override released and the normal values back. There is never an on/off/on of `ena` or `ui_in`
or a pulse of `rst_n`, whatever the timing of `start`, `abort` and a restart, at any project
clock from `min_project_hz` up and across clock stops. An aborted or reset check is cut short
(its low phase may be shorter than 10) but is still one block. Consecutive sequences are
separated by at least `MIN_GAP_CLOCKS` project clocks of fully normal pins (derived below).

Protocol (sys on the left, project on the right; every arrow is a two-flop `MultiReg`)::

    sys regs: run, pend, done, gen (toggle), n_hold, dly      project: state, my_gen, fin, rel

    start  -> pend, n_req := clocks
    pend & ~run & my_gen_s == gen & rel_s     launch:  run := 1, n_hold := n_req, dly := D
        run ----------------------------------------> run_s
    D sys clocks later (dly 1 -> 0)           gen toggles
        gen ----------------------------------------> gen_s
                      IDLE (rel = 1):  run_s & gen_s != my_gen  -> BEGIN  (rel := 0)
                                       ~run_s & gen_s != my_gen -> my_gen := gen_s (consume, nothing runs)
                      BEGIN (2 edges): my_gen := gen_s; if run_s: RUN (override on) else UNWIND
                      RUN / FIN:       ~run_s -> UNWIND (override off, fin := 0)
                      UNWIND (3 edges) -> IDLE, rel := 1
        (my_gen, fin, rel) --- each its own MultiReg ---> my_gen_s, fin_s, rel_s
    run & dly == 0 & my_gen_s == gen & fin_s  accept:  run := 0, done := 1
    abort                                     run := 0, pend := 0, done := 0, dly := 0   (next sys clock)
    system reset                              run, pend, done, dly := 0   (gen, n_req, n_hold not reset)

The begin rule. The project begins a check only from IDLE (`rel` = 1, i.e. the override is
off and the reset chain is back to normal) and only when it sees BOTH `run_s` high AND `gen_s`
different from `my_gen`; it sets `my_gen := gen_s` at that begin and never while idle with `run_s`
high, so an early `run_s` does nothing. The launch rule. The system side launches only when the
project has consumed the previous generation (`my_gen_s == gen`) AND is fully released
(`rel_s`), so a new check cannot start while the previous one is still unwinding.

Why `run` and `gen` can be seen in either order and it is still right. The simulator has no
skew, but two synchronisers on signals that change on the same edge can differ by a project clock
in either direction. Here they never change together: `run` rises at the launch and `gen` toggles
`D` system clocks later, `D = 3 x ceil(sys_hz / min_project_hz) + 4`: at least three project
clocks of the slowest supported project clock (the same 0.39 MHz threshold as `clock_running`),
so the project always sees `run_s` high no later than `gen_s` changes: `run_s` high with `gen_s`
unchanged does nothing, and the toggle then starts exactly one begin. If the project clock is
stopped both are settled when it resumes. An abort before `gen` toggles cancels the launch
(nothing crossed that the project would act on: `run_s` rises and falls with `gen_s` unchanged).
An abort after it leaves the project to consume the generation: `~run_s & gen_s != my_gen`
consumes it without a begin (`gen` is a toggle, a single change in flight, since a launch waits
for `my_gen_s == gen`), or, if the project got there first, to begin and then unwind (an abort
during a check, still one block). A project clock slower than `min_project_hz` is outside the
supported range (the `D` argument fails): at-speed checks need a clock the slot reports as
running.

Abort, system reset, clock stopped:
- abort and system reset clear `run`, `pend`, `done` at the next system clock, whatever the
  project clock does, so `busy` and `done` read 0 one system clock after the abort pulse;
- a `start` requested and not aborted stays as `run` (and, after `D`, `gen`) while the clock is
  stopped and runs when it resumes; an aborted one never runs: either `gen` never toggled, or
  `run_s` is low when the project sees `gen_s` change, and the generation is consumed;
- after a system reset `gen` keeps its value (toggles are never reset): a launch waits for
  `my_gen_s == gen` (the project consuming any unconsumed change, or still running and
  unwinding because `run` is low) and for `rel_s`, then toggles: the same argument as without a
  reset;
- a `start` given while the previous run is still unwinding waits in `pend` (visible as
  `busy`, never blocking the system side) until the project is released, then launches once; a
  `start` given while `busy` is ignored.

Why each signal is safe when it arrives early or late:
- `run` (level, sys -> project): above. Dropping it is the one way a check ends early: the
  project acts whenever it next runs, at most `UNWIND_CLOCKS` project clocks after the clock
  runs.
- `gen` (toggle, sys -> project): changes only at a launch (single change in flight).
- `n_hold` (32 bits, sys -> project, no synchroniser): changes only at the launch, `D` clocks
  before `gen`, and a launch needs the project idle, so it is stable for the whole begin
  and is read at BEGIN, at least two project clocks after `gen_s` changed.
- `my_gen`, `rel`, `fin` (project -> sys), each through its own `MultiReg`. They are read in
  two combinations only: `(my_gen, rel)` for the launch and `(my_gen, fin)` for `done`. In each
  pair the two bits never change on the same project edge and consecutive changes are at least
  two project edges apart (BEGIN lasts 2 edges: `rel` falls on the first, `my_gen` changes on the
  second; `rel` rises 3 edges after the unwind and `my_gen` has not changed since; `fin`
  rises ~14 edges after `my_gen`), so a sample of a pair is always a value the pair really
  held, provided the skew of two synchronisers (about one `sys` clock) is less than that
  separation: a project clock of at most 50 MHz gives 2 x 20 ns against 20 ns. The accepted
  `done` needs `my_gen_s == gen`, so an older generation's `fin` or an aborted check cannot
  be accepted; the launch needs `rel_s`, so it cannot begin during an unwind.
- `crc` (32 bits, project -> sys, no synchroniser): `crc_out` is written once per check, one
  project clock BEFORE `fin` rises, and not written again until a later check completes, which
  needs a new launch that first clears `done`; `sys` reads it only while `done` is high.
- `done`, `busy`, `pend`, `dly`: sys-only registers.

Not proven here, for the build task: the returning status bits (`my_gen`, `rel`, `fin`) and the
32-bit buses `crc` and `n_hold`, which cross without synchronisers or on the argument that
they are stable, need a skew / maximum-delay constraint of less than one project period
between the bits of one pair or bus (20 ns at 50 MHz), because the arguments above hold the
stable value for at least two project clocks and tolerate about one sys clock of skew, no more.

What the check does, in project clocks. Let A be the clock edge at which BEGIN acts (the
second BEGIN edge). Edge A + j is the j-th edge after it::

    A        override on, reset requested
    A+1      pin_ena = 1, pin_ui_in = reference ui_in   (slot's final registers); FF1 low
    A+2      FF2 low: rst_n at the pin is 0
    A+3      clock 0: the first rising edge the design sees with rst_n low
    ...      rst_n is low at exactly the edges A+3..A+12 = clocks 0..9 (`RESET_CLOCKS`) and
             high at A+13 = clock 10
    A+4+k    `samp` registers uo_out: the value just BEFORE this edge, i.e. after edge A+3+k
             = byte k
    A+5+k    `crc_acc` absorbs byte k
    A+4+N    the last byte (k = N-1) is absorbed
    then     `crc_out := ~crc_acc` (final XOR), override released, and one project clock later
             `fin` rises

The check is held for at least 13 clocks even when N is small. When it finishes the override
is released and the pins revert to the slot's normal values (ena, ui_in and rst_n as the
software set them); the design's INTERNAL state stays where the stimulus left it (running), and
software resets it afterwards. The reset from the check goes through D of the slot's first
reset flip-flop, so there is no logic on the pin; if software holds the reset request asserted
the design stays in reset and the CRC is of a design held in reset.

Unwind and gap, in project edges. `run_s` low is seen after edges e1, e2 (two flops); the FSM
acts on e3 = u: override and `rstq` drop; the slot's final registers carry `ena` and `ui_in`
to the pins on u + 1, the reset chain returns `rst_n` high on u + 2 (FF1 on u + 1, FF2 on
u + 2), and `rel` rises on u + 3. `UNWIND_CLOCKS` = 5: `ena`/`ui_in` are normal within 4 edges
of the first edge that samples `run` low (e1..u+1), plus one for a first flop that resolves
late; `RESET_BACK_CLOCKS` = `UNWIND_CLOCKS` + 2 for `rst_n`. The earliest next begin is the
BEGIN edge after the IDLE edge u + 4: A >= u + 6, the override reaches the pins after A + 1 >=
u + 7, so the pins are normal at least from u + 2 to u + 6: `MIN_GAP_CLOCKS` = 5 samples (the
launch delay makes it far longer in practice).

Interpreting the CRC. After a design that was already running, the result equals the
reference only for designs whose reset initialises ALL their state; run straight after
configuration it parallels the reference simulation (which is how the tests run it).

Firmware and `clock_hz: 0`. With no project clock an at-speed check is meaningless: firmware must
not start one. If it does, `busy` stays high (it waits for a clock that never comes) and `abort`
recovers it, one system clock later; nothing then ever runs on the project side.
"""

from __future__ import annotations

from math import ceil

from migen import Cat, Constant, If, Module, Signal
from migen.genlib.cdc import MultiReg

from tt_harness.compare.recorder import crc32_update
from tt_harness.core.stimulus import RESET_CLOCKS

MIN_CLOCKS_BEFORE_RELEASE = RESET_CLOCKS + 3  # edges after A before the sequence can complete
UNWIND_CLOCKS = 5
RESET_BACK_CLOCKS = UNWIND_CLOCKS + 2
MIN_GAP_CLOCKS = 5
DEFAULT_SYS_HZ = 50_000_000
DEFAULT_MIN_PROJECT_HZ = 390_000  # the `clock_running` threshold

S_IDLE, S_BEGIN, S_RUN, S_FIN, S_UNW = range(5)


def launch_delay(sys_hz: int, min_project_hz: int) -> int:
    """System clocks between `run` rising and `gen` toggling (`D` above)."""
    return 3 * ceil(sys_hz / min_project_hz) + 4


class AtSpeedCheck(Module):
    def __init__(
        self,
        ui_in: int = 0,
        sys_hz: int = DEFAULT_SYS_HZ,
        min_project_hz: int = DEFAULT_MIN_PROJECT_HZ,
    ) -> None:
        if not 0 <= ui_in <= 0xFF:
            raise ValueError(f"ui_in must be 0..255, got {ui_in}")
        if sys_hz < 1 or min_project_hz < 1:
            raise ValueError("sys_hz and min_project_hz must be positive")
        d = launch_delay(sys_hz, min_project_hz)
        self.launch_delay = d
        # sys side
        self.start = Signal()
        self.abort = Signal()
        self.clocks = Signal(32)
        self.busy = Signal()
        self.done = Signal()
        self.crc = Signal(32)
        # project side
        self.uo_out = Signal(8)
        self.override = Signal()
        self.override_rst = Signal()
        self.override_ui_in = Signal(8)

        # ---- sys ----
        self.gen = gen = Signal(reset_less=True)
        self.run_level = run = Signal()
        pend = Signal()
        dly = Signal(max=d + 1)
        n_req = Signal(32, reset_less=True)
        self.n_hold = n_hold = Signal(32, reset_less=True)
        gen_p_s = Signal()  # the project's my_gen, in sys
        rel_s = Signal()
        fin_s = Signal()
        caught_up = Signal()
        launch = Signal()
        self.comb += [
            caught_up.eq(gen_p_s == gen),
            launch.eq(pend & ~run & caught_up & rel_s),
            self.busy.eq(run | pend),
        ]
        self.sync += [
            If(
                self.start & ~run & ~pend,
                pend.eq(1),
                n_req.eq(self.clocks),
                self.done.eq(0),
            ),
            If(launch, run.eq(1), pend.eq(0), n_hold.eq(n_req), dly.eq(d)),
            If(run & (dly != 0), dly.eq(dly - 1), If(dly == 1, gen.eq(~gen))),
            If(run & (dly == 0) & caught_up & fin_s, run.eq(0), self.done.eq(1)),
            If(self.abort, run.eq(0), pend.eq(0), self.done.eq(0), dly.eq(0)),
        ]

        # ---- project ----
        sp = self.sync.project
        run_s = Signal()
        gen_s = Signal()
        my_gen = Signal(reset_less=True)
        fin = Signal(reset_less=True)
        rel = Signal(reset=1, reset_less=True)
        self.specials += [
            MultiReg(run, run_s, odomain="project", n=2),
            MultiReg(gen, gen_s, odomain="project", n=2),
            MultiReg(my_gen, gen_p_s, odomain="sys", n=2),
            MultiReg(rel, rel_s, odomain="sys", n=2),
            MultiReg(fin, fin_s, odomain="sys", n=2),
        ]
        state = Signal(3, reset_less=True)
        tmr = Signal(2, reset_less=True)
        cnt = Signal(32, reset_less=True)
        left = Signal(32, reset_less=True)
        win = Signal(reset_less=True)
        samp = Signal(8, reset_less=True)
        samp_v = Signal(reset_less=True)
        crc_acc = Signal(32, reset_less=True)
        crc_out = Signal(32, reset_less=True)
        fin_data = Signal(reset_less=True)
        fin_pending = Signal(reset_less=True)
        ovr = Signal(reset_less=True)
        rstq = Signal(reset_less=True)
        new_gen = Signal()
        complete = Signal()
        self.comb += [
            new_gen.eq(gen_s != my_gen),
            complete.eq(fin_data & (cnt >= MIN_CLOCKS_BEFORE_RELEASE)),
            self.override.eq(ovr),
            self.override_rst.eq(rstq),
            self.override_ui_in.eq(Constant(ui_in, 8)),
            self.crc.eq(crc_out),
        ]

        def to_unwind():
            return [
                state.eq(S_UNW),
                tmr.eq(2),
                ovr.eq(0),
                rstq.eq(0),
                fin.eq(0),
                fin_data.eq(0),
                fin_pending.eq(0),
                win.eq(0),
            ]

        sp += [
            samp.eq(self.uo_out),
            samp_v.eq(win),
            If(samp_v, crc_acc.eq(Cat(*crc32_update(crc_acc, samp)))),
            If(
                state == S_IDLE,
                If(run_s & new_gen, state.eq(S_BEGIN), tmr.eq(1), rel.eq(0)).Elif(
                    ~run_s & new_gen, my_gen.eq(gen_s)
                ),
            )
            .Elif(
                state == S_BEGIN,
                If(
                    tmr != 0,
                    tmr.eq(tmr - 1),
                ).Else(
                    my_gen.eq(gen_s),
                    If(
                        run_s,
                        state.eq(S_RUN),
                        cnt.eq(0),
                        left.eq(n_hold),
                        win.eq(0),
                        fin_data.eq(0),
                        crc_acc.eq(0xFFFFFFFF),
                        ovr.eq(1),
                        rstq.eq(1),
                    ).Else(*to_unwind()),
                ),
            )
            .Elif(
                state == S_RUN,
                If(~run_s, *to_unwind()).Else(
                    cnt.eq(cnt + 1),
                    If(cnt == RESET_CLOCKS - 1, rstq.eq(0)),
                    If((cnt == 2) & (left != 0), win.eq(1)),
                    If((cnt == 2) & (left == 0), fin_data.eq(1)),
                    If(win & (left == 1), win.eq(0)),
                    If(win, left.eq(left - 1)),
                    If(samp_v & ~win, fin_data.eq(1)),
                    If(
                        complete,
                        crc_out.eq(~crc_acc),
                        ovr.eq(0),
                        rstq.eq(0),
                        fin_data.eq(0),
                        fin_pending.eq(1),
                        state.eq(S_FIN),
                    ),
                ),
            )
            .Elif(
                state == S_FIN,
                If(~run_s, *to_unwind()).Elif(fin_pending, fin.eq(1), fin_pending.eq(0)),
            )
            .Elif(
                state == S_UNW,
                If(tmr != 0, tmr.eq(tmr - 1)).Else(state.eq(S_IDLE), rel.eq(1)),
            ),
        ]
