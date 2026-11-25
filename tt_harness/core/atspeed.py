# SPDX-License-Identifier: Apache-2.0
"""At-speed check: run the project at full speed under the reference stimulus and CRC its outputs.

The CPU side (`sys`) writes `clocks` (N), pulses `start`, and `done` rises with `crc`, the zlib
CRC-32 of the first N bytes of the reference dump, byte k being `uo_out` after the k-th rising
edge counted from 0 = the first rising edge at which the design sees `rst_n` low
(`tt_harness/compare/recorder.py`, `tt_harness/reference.py`). So `crc == zlib.crc32(dump[:N])`.
The check runs entirely in the project clock domain and never stops or slows the clock.

Ports. `sys`: `start` (one-cycle pulse, accepted at any time: it cancels a check in progress
and starts a new one; N = `clocks` is sampled at the pulse), `abort` (one-cycle pulse; wins
over a `start` in the same cycle), `busy` (a start was given and neither finished nor was
aborted), `done` (level: the most recent start finished; cleared by `start` and `abort`),
`crc` (valid while `done`). `project`: `uo_out` (input), `override`, `override_rst`,
`override_ui_in` (outputs, direct flip-flop outputs; see `ProjectSlot.attach`). Constructor
`ui_in` is the reference's fixed `ui_in`; `uio_in` is the slot's constant 0, which is what every
pool design's reference uses. N = 0 gives the CRC of nothing (0).

The only things that cross between the domains are two `MultiBitCrossing` handshakes
(`cdc.py`: whole values, newest wins, the sender never waits):

- command word, `sys` -> `project`: `{seq (16), run (1), n (32)}`. `seq` is a `reset_less` system
  register, incremented by every `start` (so it never returns to an old value on a system
  reset); `run` is 1 from a `start` until an `abort`, a system reset or the result; `n` is N.
- report word, `project` -> `sys`: `{my_seq (16), phase (2), crc (32)}` with phase 0 idle, 1
  active, 2 finished (`fin`); the project register word is written whenever it changes.

Protocol::

    start   seq += 1, run := 1, n := clocks, busy := 1, done := 0
    abort   run := 0, busy := 0, done := 0                     (next sys clock)
    sys reset                                                  run, busy, done := 0 (seq kept)
    report delivered with run, my_seq == seq and phase == fin: done := 1, busy := 0, crc := its crc

    project, acting only on the delivered command register `cmd`:
      IDLE   (override off, reset chain back to normal):
             cmd.run and cmd.seq != last_seq  ->  begin: my_seq = last_seq = cmd.seq, N = cmd.n
      RUN    cmd.seq != my_seq or not cmd.run ->  unwind;   N + 4 clocks counted ->  finish
      finish write crc and phase fin, unwind
      UNWIND override off, 3 clocks for the reset chain to be back, then IDLE

Safety, signal by signal. There is nothing to order between separately synchronised bits: each
word is captured whole.

- A check begins only from IDLE and only for a command with `run` set and a `seq` it has not
  begun (`last_seq`): a command delivered twice cannot begin twice, an aborted command (`run` 0)
  never begins, and the check is in IDLE only after its override is off and the reset chain is
  back, so consecutive checks are always separated by the full release by the project's own
  sequencing. A newer command delivered during an unwind is picked up at IDLE.
- With the project clock stopped nothing is delivered; when it runs again the NEWEST command is
  delivered, but a word already IN FLIGHT when the clock stopped is delivered first (the
  handshake's `hold` may not change during a transfer). So start-abort-start reports exactly the
  last start's result, but the first start may begin and is cut when the newer word arrives (one
  well-formed cut block); start-then-abort (clock stopped before the pickup) delivers the
  start first and then `run = 0`: it may begin and is cut, and it never reports; a start that was
  not aborted runs when the clock resumes (the documented choice); a check in progress when the
  clock stops resumes where it was. If the abort is given before the start word was picked up
  by the handshake (it leaves `hold` only when the previous transfer is acknowledged), only the
  newest word is ever delivered.
- `done` needs `run`, the reported `my_seq == seq` and phase `fin` in ONE delivered word, and takes
  `crc` from that same word: a report left over from before an abort, a restart or a system reset
  has an older `my_seq` and is ignored, so a `done` always belongs to the most recent `start`.
  `seq` is 16 bits and increments per start, so a collision needs 65536 starts with the
  project clock stopped.
- The sender of either word never waits: `abort` and `busy`/`done` do not depend on the project.

Latency (Ts = system period, Tp = project period; the handshake figures are those of
`MultiBitCrossing`). Start to begin: at most Ts + 3 Tp until the command is in `cmd`, plus one
project clock for the IDLE edge: `A` <= Ts + 4 Tp (if a previous word is in flight: <= 6 Tp + 3 Ts).
Abort (or a newer start) to the unwind edge `u`: the same, <= 7 Tp + 4 Ts at worst. The pins are
then normal by `u + 2`, i.e. `UNWIND_CLOCKS` = 3 project clocks after the command is delivered.

What the check does, in project clocks. Let A be the clock edge at which IDLE begins a check::

    A        reset requested, override requested
    A+1      FF1 low (slot reset chain); `override` register on
    A+2      FF2 low: rst_n at the pin is 0; pin_ena = 1 and pin_ui_in = reference ui_in
             (the override and the reset take the same two flip-flops to the pins, so they
             appear on the same edge, and any prefix of a check is "override on with reset low")
    A+3      clock 0: the first rising edge the design sees with rst_n low
    ...      rst_n is low at exactly the edges A+3..A+12 = clocks 0..9 (`RESET_CLOCKS`) and
             high at A+13 = clock 10
    A+4+k    `samp` registers uo_out: the value just BEFORE this edge, i.e. after edge A+3+k
             = byte k
    A+5+k    `crc_acc` absorbs byte k
    A+4+N    the last byte (k = N-1) is absorbed
    then     `crc_out := ~crc_acc` (final XOR), phase fin, unwind

The check is held for at least 13 clocks even when N is small. When it ends (finished or cut)
the override and the reset request go away on the same edge u; the pins are back at the slot's
normal values together at u + 2 (no intermediate on/off/on, no lag), the reset chain is then
fully back, IDLE is entered after u + 3 and the next begin acts at u + 4 or later: the pins are
normal for at least `MIN_GAP_CLOCKS` = 4 samples between two checks (normal after u + 2, the next
override reaches the pins after A + 2 >= u + 6). A check cut short is still one well-formed block:
reset low from the first clock of the override.

The design's INTERNAL state stays where the stimulus left it (running) and software resets it
afterwards. If software holds the slot's reset request asserted the design stays in reset and the
CRC is of a design held in reset. After a design that was already running, the result equals
the reference only for designs whose reset initialises ALL their state; run straight after
configuration it parallels the reference simulation (which is how the tests run it).

Using the at-speed check (firmware-facing; `docs/registers.md` will need this in the SoC task):

- Start a check only while `clock_running` reads 1 (with the project clock stopped a start waits
  for it, and `clock_hz: 0` never runs it); recover a stuck one with `abort`.
- A `start` cancels any earlier start: the last one wins, and only its result is reported.
- An `abort` (any time, clock running or stopped) clears `busy` and `done` within one system clock
  but can leave ONE cut block at the pins: the override on with the design held in reset (reset
  low for the whole block), then released. With the clock running the block ends within about
  `cut_samples` below; with the clock stopped when the abort is given, the start word already in
  flight begins the check on resume and the `run = 0` word cuts it: the block is about
  3 + ceil(3 Ts / Tp) project clocks long (measured 3 to 6 at 2.5 to 66 MHz against a 50 MHz
  `sys`). No `done` is reported for it. The design is left in an unknown state, so firmware must
  reset the design after any abort (and after a finished check: the stimulus is not the
  software's).
- With the check cut at any point the pins are `0{1..10}` (reset low from the first override
  clock) and never malformed; a check not cut is `0{10}1+`.
- `seq` is 16 bits and incremented by every start: 65,536 starts with none seen by the project
  (clock stopped) would make a start look like the last one it began, and it would be ignored until
  the next start or abort.

Not proven here, for the build task: the held bus (`hold` in `cdc.py`) of each of the two
handshakes, in both directions, is read by the other domain without synchronisers on the argument
that it is stable, and needs a maximum-delay constraint of less than one destination-clock period
(20 ns at 50 MHz). Nothing else in this module needs a constraint.

Firmware and `clock_hz: 0`. With no project clock an at-speed check is meaningless: firmware must
not start one. If it does, `busy` stays high and `abort` recovers it, one system clock later;
nothing then ever runs on the project side.
"""

from __future__ import annotations

from migen import Cat, Constant, If, Module, Signal

from tt_harness.compare.recorder import crc32_update
from tt_harness.core.cdc import MultiBitCrossing
from tt_harness.core.stimulus import RESET_CLOCKS

MIN_CLOCKS_BEFORE_RELEASE = RESET_CLOCKS + 3  # edges after A before the sequence can finish
UNWIND_CLOCKS = 3  # project clocks from the command being delivered to the pins being normal
MIN_GAP_CLOCKS = 4
SEQ_BITS = 16
PHASE_IDLE, PHASE_ACTIVE, PHASE_FIN = 0, 1, 2

S_IDLE, S_RUN, S_UNW = range(3)


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

        # ---- the two handshakes ----
        self.cmd_x = cmd_x = MultiBitCrossing(SEQ_BITS + 1 + 32)
        self.rep_x = rep_x = MultiBitCrossing(SEQ_BITS + 2 + 32, idomain="project", odomain="sys")
        self.submodules += [cmd_x, rep_x]

        # ---- sys ----
        seq = Signal(SEQ_BITS, reset_less=True)
        run = Signal()
        n = Signal(32, reset_less=True)
        self.comb += cmd_x.src.eq(Cat(seq, run, n))
        rep_seq, rep_phase, rep_crc = (
            rep_x.dst[:SEQ_BITS],
            rep_x.dst[SEQ_BITS : SEQ_BITS + 2],
            rep_x.dst[SEQ_BITS + 2 :],
        )
        self.sync += [
            # `crc` needs no latch of its own: it is taken from the same delivered word as the
            # `fin` that sets `done`, and a new `start` clears `done` before any later report
            # (whose `my_seq` differs, so it is ignored) can arrive: `crc` is only valid while `done`.
            If(
                rep_x.valid & run & (rep_seq == seq) & (rep_phase == PHASE_FIN),
                self.done.eq(1),
                self.busy.eq(0),
                self.crc.eq(rep_crc),
            ),
            If(self.abort, run.eq(0), self.busy.eq(0), self.done.eq(0)).Elif(
                self.start,
                seq.eq(seq + 1),
                run.eq(1),
                n.eq(self.clocks),
                self.busy.eq(1),
                self.done.eq(0),
            ),
        ]

        # ---- project ----
        sp = self.sync.project
        cmd_seq, cmd_run, cmd_n = (
            cmd_x.dst[:SEQ_BITS],
            cmd_x.dst[SEQ_BITS],
            cmd_x.dst[SEQ_BITS + 1 :],
        )
        state = Signal(2, reset_less=True)
        tmr = Signal(2, reset_less=True)
        my_seq = Signal(SEQ_BITS, reset_less=True)
        last_seq = Signal(SEQ_BITS, reset_less=True)
        phase = Signal(2, reset_less=True)
        cnt = Signal(32, reset_less=True)
        left = Signal(32, reset_less=True)
        win = Signal(reset_less=True)
        samp = Signal(8, reset_less=True)
        samp_v = Signal(reset_less=True)
        crc_acc = Signal(32, reset_less=True)
        crc_out = Signal(32, reset_less=True)
        fin_data = Signal(reset_less=True)
        ovr = Signal(reset_less=True)
        ovr_d = Signal(reset_less=True)
        rstq = Signal(reset_less=True)
        complete = Signal()
        stale = Signal()
        self.comb += [
            stale.eq((cmd_seq != my_seq) | ~cmd_run),
            complete.eq(fin_data & (cnt >= MIN_CLOCKS_BEFORE_RELEASE)),
            self.override.eq(ovr_d),
            self.override_rst.eq(rstq),
            self.override_ui_in.eq(Constant(ui_in, 8)),
            rep_x.src.eq(Cat(my_seq, phase, crc_out)),
        ]

        def unwind():
            return [state.eq(S_UNW), tmr.eq(2), ovr.eq(0), rstq.eq(0), fin_data.eq(0), win.eq(0)]

        sp += [
            ovr_d.eq(ovr),
            samp.eq(self.uo_out),
            samp_v.eq(win),
            If(samp_v, crc_acc.eq(Cat(*crc32_update(crc_acc, samp)))),
            If(
                state == S_IDLE,
                If(
                    cmd_run & (cmd_seq != last_seq),
                    state.eq(S_RUN),
                    my_seq.eq(cmd_seq),
                    last_seq.eq(cmd_seq),
                    left.eq(cmd_n),
                    cnt.eq(0),
                    win.eq(0),
                    fin_data.eq(0),
                    crc_acc.eq(0xFFFFFFFF),
                    ovr.eq(1),
                    rstq.eq(1),
                    phase.eq(PHASE_ACTIVE),
                ),
            )
            .Elif(
                state == S_RUN,
                If(stale, *unwind(), phase.eq(PHASE_IDLE)).Else(
                    cnt.eq(cnt + 1),
                    If(cnt == RESET_CLOCKS - 1, rstq.eq(0)),
                    If((cnt == 2) & (left != 0), win.eq(1)),
                    If((cnt == 2) & (left == 0), fin_data.eq(1)),
                    If(win & (left == 1), win.eq(0)),
                    If(win, left.eq(left - 1)),
                    If(samp_v & ~win, fin_data.eq(1)),
                    If(complete, crc_out.eq(~crc_acc), phase.eq(PHASE_FIN), *unwind()),
                ),
            )
            .Elif(
                state == S_UNW,
                If(tmr != 0, tmr.eq(tmr - 1)).Else(state.eq(S_IDLE)),
            ),
        ]
