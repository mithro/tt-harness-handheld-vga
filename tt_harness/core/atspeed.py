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
nothing (0).

Protocol (sys on the left, project on the right; every arrow is a two-flop `MultiReg`)::

    sys regs: run, pend, done, gen (toggle), n_hold          project regs: running, fin, my_gen

    start  -> pend, n_req := clocks
    pend & ~run & (my_gen_s == gen)   launch:  gen toggles, run := 1, n_hold := n_req
        run ---------------------------------> run_s
        gen ---------------------------------> gen_s
                                                IDLE: my_gen := gen_s; if run_s: begin
                                                RUNNING / FIN: if ~run_s or gen_s != my_gen: unwind
        (my_gen, fin) --- Gray: one bit at a time ---> my_gen_s, fin_s
    run & my_gen_s == gen & fin_s     accept:  run := 0, done := 1
    abort                             run := 0, pend := 0, done := 0     (next sys clock)
    system reset                      run, pend, done := 0   (gen, n_req, n_hold are not reset)

Why a late or early arrival is harmless, signal by signal:

- `run` (level, sys -> project): it can only start a check from IDLE, and a dip of `run` too
  short for the project to see is covered by `gen`; `run` low is the abort of a check in
  progress, seen whenever the project clock next runs. A `start` that is not aborted stays
  as `run` high while the clock is stopped and runs when it resumes (chosen: "requested,
  clock stopped, not aborted" runs; an aborted one never does, because the level is gone
  before the clock returns).
- `gen` (toggle, sys -> project): changes only at a launch, and a launch needs the project to
  have caught up with the previous one (`my_gen_s == gen`), so at most ONE change is ever in
  flight and a change cannot be cancelled by a second one. Whatever the project is doing
  (idle, running, finished, frozen mid-run by a stopped clock), a change makes it unwind and
  look at `run` afresh. So a missed dip of `run` (abort then a quick re-launch, or a reset
  then a launch) restarts the check instead of continuing the old one. A skew of one project
  clock between `run` and `gen` only costs one extra unwind and restart.
- `n_hold` (32 bits, sys -> project, no synchroniser): it changes only at a launch, which needs
  the project to have caught up, i.e. to have already read N at its `begin` (the same edge at
  which `my_gen` took the new value). It is read at `begin`, at least two project clocks after
  the launch edge.
- `(my_gen, fin)` (project -> sys): two flops that change only on different clock edges, so
  the pair is a 2-bit Gray sequence and a sample is always a value it really held (even though
  each bit has its own synchroniser). `my_gen_s == gen` means "the project has seen the
  latest launch", `fin_s` with it means "and it finished that launch": a finished flag of
  an older launch is paired with an older `my_gen` and cannot be accepted, nor can a check that
  was aborted (run is 0), so a `done` is always for the most recent `start`.
- `crc` (32 bits, project -> sys, no synchroniser): `crc_out` is written once per check, one
  project clock BEFORE `fin` rises, and not written again until a later check completes,
  which needs a new launch that clears `done` first. `sys` reads it only while `done` is high.
  A skew between the 32 bits is therefore harmless (the value is stable for longer than any
  synchroniser's resolution time). A max-delay constraint belongs to the later build task.
- `done`/`busy`/`pend`: sys-only registers. Abort is registered in the next system clock:
  `busy` and `done` read 0 ONE SYSTEM CLOCK after the abort pulse, whatever the project clock
  does (including stopped). A system reset clears them the same way, and `gen` is deliberately
  not reset so that the toggle and the project's copy keep agreeing.
- A `start` given while a previous (aborted) run is still unwinding is held in `pend`
  (visible as `busy`, never blocking the system side) until the project has caught up with
  the previous launch; it is then launched once. No start is lost or doubled; a start
  given while `busy` is ignored.

Unwind. When `run` falls (abort, reset, a `done` accepted) or `gen` changes, a check in progress
returns to idle by itself: `override` and `override_rst` drop, the slot's final registers
carry the normal controls to the pins one clock later, and the reset chain releases `rst_n`
two clocks after that (all flip-flop outputs, no glitch). Bound: `UNWIND_CLOCKS` = 8 project
clocks after `run` fell, counted from the first project clock after the request reaches the
project; with the clock stopped it happens when the clock resumes. The result of an unwound
check is never reported.

What the check does, in project clocks. Let A be the clock edge at which the project begins
(IDLE with `run_s` high). Edge A + j is the j-th edge after it::

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

The check is held for at least 13 clocks even when N is small. When it finishes, the override
is released and the pins revert at once to the slot's normal values (ena, ui_in and
rst_n as the software set them); the design's INTERNAL state is where the stimulus left it
(running), and software resets it afterwards. The reset from the check is synchronous (D of the
slot's first reset flip-flop), so the two reset sources need no logic on the pin; if software holds
the reset request asserted the design stays in reset and the CRC is of a design held in reset
(the check still completes).

Interpreting the CRC. After a design that was already running, the result equals the
reference only for designs whose reset initialises ALL their state; run straight after
configuration it parallels the reference simulation (which is how the tests run it).

Firmware and `clock_hz: 0`. With no project clock an at-speed check is meaningless: firmware must
not start one. If it does, `busy` stays high (it waits for a clock that never comes) and `abort`
recovers it, one system clock later; nothing then ever runs on the project side.
"""

from __future__ import annotations

from migen import Cat, Constant, If, Module, Signal
from migen.genlib.cdc import MultiReg

from tt_harness.compare.recorder import crc32_update
from tt_harness.core.stimulus import RESET_CLOCKS

MIN_CLOCKS_BEFORE_RELEASE = RESET_CLOCKS + 3  # edges after A before the sequence can complete
UNWIND_CLOCKS = 8


class AtSpeedCheck(Module):
    def __init__(self, ui_in: int = 0) -> None:
        if not 0 <= ui_in <= 0xFF:
            raise ValueError(f"ui_in must be 0..255, got {ui_in}")
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
        gen = Signal(reset_less=True)
        run = Signal()
        pend = Signal()
        n_req = Signal(32, reset_less=True)
        self.n_hold = n_hold = Signal(32, reset_less=True)
        gen_p_s = Signal()  # the project's my_gen, in sys
        fin_s = Signal()
        caught_up = Signal()
        self.comb += [
            caught_up.eq(gen_p_s == gen),
            self.busy.eq(run | pend),
        ]
        self.sync += [
            If(
                self.start & ~run & ~pend,
                pend.eq(1),
                n_req.eq(self.clocks),
                self.done.eq(0),
            ),
            If(
                pend & ~run & caught_up,
                gen.eq(~gen),
                run.eq(1),
                pend.eq(0),
                n_hold.eq(n_req),
            ),
            If(run & caught_up & fin_s, run.eq(0), self.done.eq(1)),
            If(self.abort, run.eq(0), pend.eq(0), self.done.eq(0)),
        ]

        # ---- project ----
        sp = self.sync.project
        run_s = Signal()
        gen_s = Signal()
        my_gen = Signal(reset_less=True)
        fin = Signal(reset_less=True)
        self.specials += [
            MultiReg(run, run_s, odomain="project", n=2),
            MultiReg(gen, gen_s, odomain="project", n=2),
            MultiReg(my_gen, gen_p_s, odomain="sys", n=2),
            MultiReg(fin, fin_s, odomain="sys", n=2),
        ]
        running = Signal(reset_less=True)
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
        complete = Signal()
        unwind = Signal()
        idle = Signal()
        self.comb += [
            unwind.eq(~run_s | (gen_s != my_gen)),
            idle.eq(~running & ~fin & ~fin_pending),
            complete.eq(running & fin_data & (cnt >= MIN_CLOCKS_BEFORE_RELEASE)),
            self.override.eq(ovr),
            self.override_rst.eq(rstq),
            self.override_ui_in.eq(Constant(ui_in, 8)),
            self.crc.eq(crc_out),
        ]
        sp += [
            samp.eq(self.uo_out),
            samp_v.eq(win),
            If(samp_v, crc_acc.eq(Cat(*crc32_update(crc_acc, samp)))),
            If(
                running,
                cnt.eq(cnt + 1),
                If(cnt == RESET_CLOCKS - 1, rstq.eq(0)),
                If((cnt == 2) & (left != 0), win.eq(1)),
                If((cnt == 2) & (left == 0), fin_data.eq(1)),
                If(win & (left == 1), win.eq(0)),
                If(win, left.eq(left - 1)),
                If(samp_v & ~win, fin_data.eq(1)),
            ),
            If(
                complete,
                crc_out.eq(~crc_acc),
                ovr.eq(0),
                rstq.eq(0),
                running.eq(0),
                fin_data.eq(0),
                fin_pending.eq(1),
            ),
            If(fin_pending, fin.eq(1), fin_pending.eq(0)),
            # Unwind: the request went away or a new launch arrived. Nothing is reported.
            If(
                (running | fin | fin_pending) & unwind,
                ovr.eq(0),
                rstq.eq(0),
                running.eq(0),
                fin_data.eq(0),
                fin_pending.eq(0),
                fin.eq(0),
                win.eq(0),
            ),
            # Idle: follow `gen`, and begin when a check is requested.
            If(
                idle,
                my_gen.eq(gen_s),
                If(
                    run_s,
                    running.eq(1),
                    cnt.eq(0),
                    left.eq(n_hold),
                    win.eq(0),
                    fin_data.eq(0),
                    crc_acc.eq(0xFFFFFFFF),
                    ovr.eq(1),
                    rstq.eq(1),
                ),
            ),
        ]
