# SPDX-License-Identifier: Apache-2.0
"""At-speed check: run the project at full speed under the reference stimulus and CRC its outputs.

The CPU side (`sys`) writes `clocks` (N) and pulses `start`; the check runs entirely in the
project clock domain, never stopping or slowing the clock, and `done` rises with `crc`, the
zlib CRC-32 of the first N bytes of the reference dump, byte k being `uo_out` after the k-th
rising edge counted from 0 = the first rising edge at which the design sees `rst_n` low
(`tt_harness/compare/recorder.py`, `tt_harness/reference.py`). So `crc == zlib.crc32(dump[:N])`.

Ports. `sys`: `start` (one-cycle pulse; ignored while `busy`), `clocks` (N; hold it
stable until `done` -- it is also latched at `start`), `busy`, `done` (level: set when the
check finishes, cleared by `start`), `crc` (valid while `done`). `project`: `uo_out` (input),
`override`, `override_rst`, `override_ui_in` (outputs, direct flip-flop outputs; see
`ProjectSlot.attach`). `ui_in` of the constructor is the reference's fixed `ui_in`; `uio_in`
is the slot's constant 0, which is what every pool design's reference uses. N = 0 gives the
CRC of nothing (0).

Crossings (everything else is in `project`):

- `start`: a toggle flip-flop in `sys` (`start_tog`), through a two-flop `MultiReg`, edge
  detected in `project`. A toggle, not a pulse, so it survives any clock ratio; `start` while
  `busy` is ignored (otherwise two toggles could cancel). `n_hold` (N) is a `sys` register
  loaded at the same edge as the toggle and stable until the next accepted `start`; the project
  reads it directly at least two project clocks later (the argument of `cdc.py`, item 3).
- `done`: a toggle flip-flop in `project` (`fin_tog`), through a two-flop `MultiReg`, edge
  detected in `sys`, which then sets `done` and clears `busy`. `crc` is the `project` register
  `crc_out`, read by `sys` with no synchroniser: this is safe because `crc_out` is written once
  per check, one project clock BEFORE `fin_tog` flips, and not again until the next check
  completes, so by the time `sys` has seen `fin_tog` (two flops plus the edge detector, at least
  3 system clocks later) every bit of `crc_out` has been stable for longer than any
  synchroniser's resolution time, and `sys` reads it only while `done` is high (`start` clears
  `done` before the next check can change it). A skew between the 32 bits is harmless for
  the same reason. A path constraint (max delay, as for `hold` in `cdc.py`) belongs to the
  later build task.

With the project clock stopped nothing happens: `busy` stays high, the `sys` side never waits,
and the check runs when the clock does. `start` is ignored until it finishes (no abort).

What the check does, in project clocks. Let A be the clock edge at which the project sees
the start request (`start_s != start_d`). Edge A + j is the j-th edge after it::

    A        ovr, rstq := 1   (override on; synchronous reset request)
    A+1      pin_ena := 1, pin_ui_in := reference ui_in   (slot's final registers)
             FF1 := 0                                      (slot's reset chain, D = ~rstq)
    A+2      FF2 := 0, so rst_n at the pin is 0 from here
    A+3      clock 0: the first rising edge the design sees with rst_n low
    ...      rstq stays 1 through A+10, so FF1 is 0 for the edges A+1..A+10 and the pin for
             A+2..A+11 hence rst_n is low at exactly the edges A+3..A+12 = clocks 0..9
             (`RESET_CLOCKS` = 10), and high at A+13 = clock 10.
    A+3+k+1  `samp` registers uo_out: the value it holds is the one that was on the output
             just BEFORE this edge, i.e. after edge A+3+k = byte k.
    A+3+k+2  `crc_acc` absorbs byte k (`samp_v` high).
    A+4+N    the last byte (k = N-1) is absorbed.
    then     `crc_out := ~crc_acc` (final XOR), `ovr := 0` (the slot's normal controls apply again
             at the next edge), and one project clock later `fin_tog` flips.

The check is held (reset sequence and all) for at least 13 clocks even when N is small. It
leaves the design where the stimulus took it (ena 1, reference `ui_in`, running), and the
software resets it afterwards. The reset from the check is synchronous (it goes through D of
the slot's first reset flip-flop), so the two reset sources need no logic on the pin; if
software holds the reset request asserted the design stays in reset and the CRC is of a design
held in reset (the check still completes).

Interpreting the CRC. After a design that was already running, the result equals the
reference only for designs whose reset initialises ALL their state (the check resets the
design but the reference starts from power-up state, which the simulation takes as 0); run
straight after configuration it parallels the reference simulation (which is how the tests run
it).
"""

from __future__ import annotations

from migen import Cat, Constant, If, Module, Signal
from migen.genlib.cdc import MultiReg

from tt_harness.compare.recorder import crc32_update
from tt_harness.core.stimulus import RESET_CLOCKS

MIN_CLOCKS_BEFORE_RELEASE = RESET_CLOCKS + 3  # edges after A before the sequence can complete


class AtSpeedCheck(Module):
    def __init__(self, ui_in: int = 0) -> None:
        if not 0 <= ui_in <= 0xFF:
            raise ValueError(f"ui_in must be 0..255, got {ui_in}")
        # sys side
        self.start = Signal()
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
        start_tog = Signal(reset_less=True)
        self.n_hold = n_hold = Signal(32, reset_less=True)
        fin_s = Signal()
        fin_d = Signal(reset_less=True)
        self.sync += [
            fin_d.eq(fin_s),
            If(
                self.start & ~self.busy,
                self.busy.eq(1),
                self.done.eq(0),
                start_tog.eq(~start_tog),
                n_hold.eq(self.clocks),
            ),
            If(fin_s != fin_d, self.busy.eq(0), self.done.eq(1)),
        ]

        # ---- project ----
        sp = self.sync.project
        start_s = Signal()
        start_d = Signal(reset_less=True)
        fin_tog = Signal(reset_less=True)
        self.specials += [
            MultiReg(start_tog, start_s, odomain="project", n=2),
            MultiReg(fin_tog, fin_s, odomain="sys", n=2),
        ]
        start_p = Signal()
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
        self.comb += [
            start_p.eq(start_s != start_d),
            complete.eq(running & fin_data & (cnt >= MIN_CLOCKS_BEFORE_RELEASE)),
            self.override.eq(ovr),
            self.override_rst.eq(rstq),
            self.override_ui_in.eq(Constant(ui_in, 8)),
            self.crc.eq(crc_out),
        ]
        sp += [
            start_d.eq(start_s),
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
            If(fin_pending, fin_tog.eq(~fin_tog), fin_pending.eq(0)),
            If(
                start_p,
                running.eq(1),
                cnt.eq(0),
                left.eq(n_hold),
                win.eq(0),
                fin_data.eq(0),
                fin_pending.eq(0),
                crc_acc.eq(0xFFFFFFFF),
                ovr.eq(1),
                rstq.eq(1),
            ),
        ]
