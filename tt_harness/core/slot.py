# SPDX-License-Identifier: Apache-2.0
"""The project slot: the ONLY place where the `sys` and `project` clock domains meet.

`ProjectSlot` owns the Tiny Tapeout project instance and its clock (clock domain `project`),
and carries the CPU's control values into that domain. Everything else in the demo build is
in one domain: the CPU side in `sys` (50 MHz), the VGA / audio / seven-segment / gamepad
blocks in `project`.

Rule for everything driven into the project. Every signal the harness drives into the
project, other than its clock, is a constant or the DIRECT output of a flip-flop clocked
by the PROJECT clock, with nothing combinational after it (a design may use any input
asynchronously, and a glitch on `rst_n` once cleared a design's state on hardware: see
`tt_harness/compare/inputs.py`). A `sys` register driving a project input directly would
also be a crossing into the design's own flip-flops, which is how metastability gets inside
a design. `tt_harness.compare.inputs.project_inputs` checks the rule on the Migen fragment
(including the domain) and `tt_harness.input_gate` on the implemented netlist.

Crossings (every signal that passes between the domains; see also `cdc.py`)
---------------------------------------------------------------------------

sys -> project, each source a direct `sys` flip-flop owned by the slot:

- the control word `{release, ena, gamepad_enable, ui_in}` (11 bits, `ctl_x`): ONE
  `MultiBitCrossing` (toggle handshake, `hold` register stable while the request crosses;
  see `cdc.py`, which also gives the worst-case latency), so the project always holds the
  four fields of one word ("The control word" below). No control crosses as a separate bit.
- `gamepad_buttons` (24 bits, `buttons_x`): its own `MultiBitCrossing`.
- reset request (`rst_req`; a `sys` flop whose value after a `sys` reset is "asserted"): the
  asynchronous clear of the reset chain (below). The clear pin sees a glitch-free flip-flop
  output.
- `clock_enable`: into `ProjectClock.enable`, which synchronises it into the MMCM's own
  clock domain (`ProjectClock` docstring).

project -> sys:

- `locked` (MMCM LOCKED, asynchronous): two-flop `MultiReg`. (Simulation builds have no
  MMCM: `locked` is then a `project` flop that becomes 1 at the first project clock.)
- `clock_running`: a toggle flip-flop in `project` that flips every 4 project clocks, through
  a two-flop `MultiReg` into `sys`, where an edge of it reloads a timeout of
  `CLOCK_RUNNING_TIMEOUT` (512) system clocks that counts down; `clock_running` is "the
  timer is not zero". The toggle (not a level, not every clock) cannot alias against
  the system clock: at 50 MHz a flip every 4 clocks (80 ns) is 4 system clocks long.
  Threshold: the toggle flips every `ALIVE_DIVIDE` = 4 project clocks and the timer reloads
  to `CLOCK_RUNNING_TIMEOUT` = 512 system clocks at each flip seen, so `clock_running`
  stays 1 iff 4 / f_project < 512 / f_sys (less about 2 system clocks of synchroniser
  latency): with `sys` 50 MHz, f_project > 4 / (512 x 20 ns) = 0.39 MHz. A running clock
  slower than that reads 0 between flips (flickers), and 0 with no clock at all; 1 MHz to
  50 MHz read 1. A design whose `clock_hz` is below the threshold would read "not running":
  a later task will check the demo files against it. It reads 0 within 512 system
  clocks (about 10 us) of the clock stopping.
- the `MultiBitCrossing` acknowledge toggles (inside `cdc.py`).

Nothing brings `uo_out`, `uio_out` or `uio_oe` into `sys`: they are exposed raw in
`project` for the blocks (VGA, audio, seven-segment, at-speed check) that live there.

Reset: asynchronous assert, synchronous release
-----------------------------------------------

The project's `rst_n` is the output of the second of two `AsyncClearFlop`s (FDCE) in
`project`, both cleared by the request, with `d` of the first the release bit of the
delivered control word (a `project` register) gated by the at-speed check::

    rst_req (sys flop) ---------+---------------------+  async clear (CLR) of both
                                v                     v
    release & ~override_rst --> [ FF1 D ]  Q --> [ FF2 D ]  Q --> project.rst_n (pin)
    (project registers)              ^ project clock       ^ project clock

Assertion works with the project clock stopped: the pin goes low as soon as the request
does, one system clock after `rst_n` is written 0. Release is synchronous: the pin goes high
just after the second project edge at which the delivered release bit is 1 and the request is
low. `rst_n` is therefore never changed except by the output of a project-clock flip-flop or
by the clear of that flip-flop, which only a glitch-free `sys` flip-flop can drive. The chain
starts at 0 (design in reset) on power-up. The at-speed check asserts reset synchronously
through `d` of FF1 (it overrides nothing else on this path), so the reset sources combine
without any logic on the pin.

The control word: the order of writes is kept at the pins
---------------------------------------------------------

Firmware writes `ui_in` and `ena` (and `gamepad_enable`) and then releases `rst_n`; a design
may read its `ui_in` straps in its first clocks out of reset. So the release travels WITH the
values: `{release, ena, gamepad_enable, ui_in}` cross as one word, the delivered release bit
is FF1's `d`, and `pin_ena` / `pin_ui_in` are registered from the same delivered word. On
the first project clock at which the design sees `rst_n` high, `ena` and `ui_in` therefore
hold the last values written before the release was written (or newer ones), at any clock
ratio and with the clock stopped during the writes: the pins take the word's values one
clock after it is delivered and `rst_n` two clocks after (`tests/test_slot_order.py`).

The release bit sent is `~rst_req`, and the request is a set/clear flop in `sys`::

    rst_n written 0                                       -> rst_req := 1   (next sys clock)
    rst_n is 1, no transfer in flight and the last word
    picked up had release 0 (so the project holds 0)      -> rst_req := 0

so the request (the clear pins) is released only once the project is known to hold release
0, and a word with release 1 is picked up only after that, with the values of that moment.
Without this a release bit delivered earlier would still be in the project during a short
reset pulse, and `rst_n` would rise again after two clocks with the old `ui_in`. Consequences:

- Release latency with nothing in flight: the request falls one system clock after the
  write, the word is picked up one system clock later, and the pin rises at the 5th project
  edge after that (two synchroniser flops, the capture, FF1, FF2): 3 project clocks and one
  system clock more than a bare two-flop release. With a transfer in flight, or after a reset
  pulse given to a running design (whose release-0 word must first be delivered and
  acknowledged), up to about 3 project and 4 system clocks more.
- A reset pulse of any length, however short, holds the design in reset until that round
  trip is done: about 8 project clocks or more.
- With the project clock stopped a reset stays asserted until the clock runs (it could not
  be released without a clock anyway); nothing in `sys` waits.
- A write of `ena` or `ui_in` AFTER the release is its own later word.

Not part of the word: `gamepad_buttons` (its own crossing, read by the emulator at the start
of each frame: button values are not ordered against the release) and, when `gamepad_enable`
is set, bits 6:4 of the project's `ui_in`, which then come from the emulator in `project` and
not from the word. `gamepad_enable` itself is in the word, so the mux select is in place at
the release.

The `ui_in[6:4]` mux and the gamepad
------------------------------------

With `gamepad_enable` (a field of the delivered control word) set, bits 6:4 of the project's `ui_in`
are the gamepad emulator's `data` (bit 6), `clock` (bit 5) and `latch` (bit 4); otherwise,
and for the other five bits always, they are the word's `ui_in`. The mux is in FRONT of the
final `project` register, so the pin is still a direct flip-flop output; this adds one
project clock of delay to the gamepad signals (they arrive one clock after the emulator's
own outputs), which a receiver does not care about (it needs levels to last a slot of 4 or
more clocks). The emulator runs free in `project` from the first clock; a design reset
mid-frame can see one wrong gamepad report, as it would with the real Pmod, and the slot
does not align reset release to gamepad frames (see `gamepad.py`).

Overrides (used by `AtSpeedCheck`)
----------------------------------

`override`, `override_rst`, `override_ui_in` (project domain) take over `ena` (forced 1),
`ui_in` (forced to `override_ui_in`, gamepad mux off) and, through FF1's `d`, `rst_n`. They
act in FRONT of the final registers, so the pins stay direct flip-flop outputs. `attach`
wires an `AtSpeedCheck` to them; unattached they are 0. When the check ends (finished,
aborted or unwound) the override is released and the pins revert, one project clock later
for `ena` and `ui_in` and two more for `rst_n`, to the values the normal controls give: the
design's INTERNAL state stays where the stimulus left it.

`uio_in` is the constant 0.

Simulation: `sim_clock=True` instantiates no `ProjectClock` and leaves the clock of domain
`project` for the test bench to drive; reset cells are emitted in their behavioural form so
Verilator and Migen's Verilog output work. The real clock path (MMCM) is exercised by builds.
`clock_hz == 0` (clock stopped, as on the demo board): no MMCM, the project clock is the
constant 0, `locked` and `clock_running` read 0 and every system-side operation still
completes (nothing in `sys` ever waits for a project-domain event); no gamepad emulator is
built (it has no clock to be timed by), so the gamepad lines are 0. `top_module=None`
instantiates no project and leaves the project's pins and outputs as plain signals
(`pin_*`, `uo_out`, `uio_out`, `uio_oe`) for a test bench.

Ports are in the docstring above and in the attribute names: `ena`, `rst_n` (1 = released),
`ui_in`, `gamepad_enable`, `gamepad_buttons`, `clock_enable` (inputs, `sys`, any `sys`
signal, e.g. a CSR); `locked`, `clock_running` (outputs, `sys`); `uo_out`, `uio_out`,
`uio_oe`, `pin_*` (`project`).
"""

from __future__ import annotations

from migen import (
    Cat,
    ClockDomain,
    ClockDomainsRenamer,
    Constant,
    If,
    Instance,
    Module,
    Mux,
    Signal,
)
from migen.genlib.cdc import MultiReg

from tt_harness.core.cdc import AsyncClearFlop, MultiBitCrossing
from tt_harness.core.clocking import ProjectClock
from tt_harness.core.gamepad import FRAME_RATE_HZ, GamepadEmulator

PROJECT_INSTANCE = "project"
# System clocks without a seen edge of the project clock's alive toggle before
# `clock_running` reads 0. The toggle flips every 4 project clocks, so this is a project
# clock of at least about 0.4 MHz at 50 MHz `sys` (a toggle half-period of 4 / 0.4 MHz = 10 us
# is 512 system clocks). 1 MHz (200 system clocks per flip) and up read 1.
CLOCK_RUNNING_TIMEOUT = 512
ALIVE_DIVIDE = 4  # project clocks per flip of the alive toggle
# The control word (`ctl_x`): bit positions.
CTL_RELEASE, CTL_ENA, CTL_GAMEPAD, CTL_UI_IN = 0, 1, 2, 3
CTL_BITS = CTL_UI_IN + 8


class ProjectSlot(Module):
    def __init__(
        self,
        top_module: str | None,
        clk_in: Signal,
        clk_in_hz: int,
        clock_hz: int,
        *,
        platform=None,
        sim_clock: bool = False,
        gamepad_frame_rate_hz: int = FRAME_RATE_HZ,
    ) -> None:
        if clock_hz < 0:
            raise ValueError(f"clock_hz must not be negative, got {clock_hz}")
        # sys-domain ports.
        self.ena = Signal()
        self.rst_n = Signal()
        self.ui_in = Signal(8)
        self.gamepad_enable = Signal()
        self.gamepad_buttons = Signal(24)
        self.clock_enable = Signal()
        self.locked = Signal()
        self.clock_running = Signal()
        # project-domain: the design's raw outputs and the pins driven into it.
        self.uo_out = Signal(8)
        self.uio_out = Signal(8)
        self.uio_oe = Signal(8)
        self.pin_rst_n = Signal()
        self.pin_ena = Signal()
        self.pin_ui_in = Signal(8)
        # project-domain overrides (AtSpeedCheck).
        self.override = Signal()
        self.override_rst = Signal()
        self.override_ui_in = Signal(8)
        self._attached = False
        # Build facts, for the SoC's `info` registers.
        self.clock_hz = clock_hz
        self.sim_clock = sim_clock
        self.clock = None
        self.gamepad = None

        self.clock_domains.cd_project = cd = ClockDomain("project", reset_less=True)
        sd = self.sync.project

        # The control word and the gamepad buttons: one handshake each.
        self.submodules.ctl_x = ctl_x = MultiBitCrossing(CTL_BITS)
        self.submodules.buttons_x = MultiBitCrossing(24)

        # sys registers that are the sources of the other crossings. The reset request is
        # set by `rst_n` low at once, and cleared only when the control word the project
        # holds is known to have its release bit 0 (the last word picked up has it 0 and has
        # been acknowledged). A word with the release bit 1 is only ever picked up while the
        # request is low, so after the release was written, with whatever `ena`,
        # `gamepad_enable` and `ui_in` are then.
        ce_x = Signal()
        self.rst_req = rst_req = Signal(reset=1)
        self.sync += [
            ce_x.eq(self.clock_enable),
            If(~self.rst_n, rst_req.eq(1)).Elif(
                ~ctl_x.busy & ~ctl_x.hold[CTL_RELEASE], rst_req.eq(0)
            ),
        ]
        self.comb += [
            ctl_x.src.eq(Cat(~rst_req, self.ena, self.gamepad_enable, self.ui_in)),
            self.buttons_x.src.eq(self.gamepad_buttons),
        ]

        # The clock, and `locked`.
        if sim_clock:
            # The bench drives cd.clk. No MMCM: `locked` is "the project clock has run".
            locked_p = Signal(reset_less=True)
            sd += locked_p.eq(1)
            self.specials += MultiReg(locked_p, self.locked, odomain="sys", n=2)
        elif clock_hz == 0:
            self.comb += [cd.clk.eq(0), self.locked.eq(0)]
        else:
            self.submodules.clock = pc = ProjectClock(platform, clk_in, clk_in_hz, clock_hz)
            self.comb += [cd.clk.eq(pc.clk), pc.enable.eq(ce_x)]
            self.specials += MultiReg(pc.locked, self.locked, odomain="sys", n=2)

        # The delivered control word (project registers, all four fields from one word).
        release_s = ctl_x.dst[CTL_RELEASE]
        ena_s = ctl_x.dst[CTL_ENA]
        gp_s = ctl_x.dst[CTL_GAMEPAD]
        ui_cap = ctl_x.dst[CTL_UI_IN:]

        # The gamepad emulator, free-running in project.
        if clock_hz > 0:
            emu = GamepadEmulator(clock_hz, gamepad_frame_rate_hz)
            self.submodules.gamepad = ClockDomainsRenamer("project")(emu)
            self.comb += emu.buttons.eq(self.buttons_x.dst)
            gp_lines = (emu.latch, emu.clock, emu.data)
        else:
            gp_lines = (Constant(0), Constant(0), Constant(0))

        # Mux and override, in front of the final registers.
        gp_active = Signal()
        ui_next = Signal(8)
        self.comb += [
            gp_active.eq(gp_s & ~self.override),
            ui_next[0:4].eq(ui_cap[0:4]),
            ui_next[4].eq(Mux(gp_active, gp_lines[0], ui_cap[4])),
            ui_next[5].eq(Mux(gp_active, gp_lines[1], ui_cap[5])),
            ui_next[6].eq(Mux(gp_active, gp_lines[2], ui_cap[6])),
            ui_next[7].eq(ui_cap[7]),
        ]
        sd += [
            self.pin_ena.eq(Mux(self.override, 1, ena_s)),
            self.pin_ui_in.eq(Mux(self.override, self.override_ui_in, ui_next)),
        ]

        # Reset chain.
        rst_d = Signal()
        self.comb += rst_d.eq(release_s & ~self.override_rst)
        ff1 = AsyncClearFlop("project", rst_req, rst_d, behavioural=sim_clock)
        ff2 = AsyncClearFlop("project", rst_req, ff1.q, q=self.pin_rst_n, behavioural=sim_clock)
        self.specials += [ff1, ff2]

        # clock_running.
        divider = Signal(2, reset_less=True)
        alive = Signal(reset_less=True)
        sd += [divider.eq(divider + 1), If(divider == ALIVE_DIVIDE - 1, alive.eq(~alive))]
        alive_s = Signal()
        alive_d = Signal(reset_less=True)
        timer = Signal(max=CLOCK_RUNNING_TIMEOUT + 1)
        self.specials += MultiReg(alive, alive_s, odomain="sys", n=2)
        self.sync += [
            alive_d.eq(alive_s),
            If(alive_s != alive_d, timer.eq(CLOCK_RUNNING_TIMEOUT)).Elif(
                timer != 0, timer.eq(timer - 1)
            ),
        ]
        self.comb += self.clock_running.eq(timer != 0)

        for sig in (self.pin_rst_n, self.pin_ena, self.pin_ui_in):
            sig.attr.add("keep")

        # The project.
        if top_module is not None:
            self.specials += Instance(
                top_module,
                name=PROJECT_INSTANCE,
                i_clk=cd.clk,
                i_rst_n=self.pin_rst_n,
                i_ena=self.pin_ena,
                i_ui_in=self.pin_ui_in,
                i_uio_in=Constant(0, 8),
                o_uo_out=self.uo_out,
                o_uio_out=self.uio_out,
                o_uio_oe=self.uio_oe,
            )

    def attach(self, check) -> None:
        """Wire an `AtSpeedCheck` to the overrides and give it `uo_out`."""
        self._attached = True
        self.comb += [
            self.override.eq(check.override),
            self.override_rst.eq(check.override_rst),
            self.override_ui_in.eq(check.override_ui_in),
            check.uo_out.eq(self.uo_out),
        ]

    def do_finalize(self) -> None:
        if not self._attached:
            self.comb += [
                self.override.eq(0),
                self.override_rst.eq(0),
                self.override_ui_in.eq(0),
            ]
