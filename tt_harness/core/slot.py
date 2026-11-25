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

- `ena`, `gamepad_enable`: two-flop `MultiReg`, then (`ena`) the final register or
  (`gamepad_enable`) the mux select in front of the final register.
- reset request (`rst_req`, the inverse of `rst_n`; a `sys` flop whose value after a `sys`
  reset is "asserted"): the asynchronous clear of the reset chain (below). The clear pin
  sees a glitch-free flip-flop output.
- `ui_in` (8 bits), `gamepad_buttons` (24 bits): `MultiBitCrossing` (toggle handshake,
  `hold` register stable while the request crosses; see `cdc.py`, which also gives the
  worst-case latency).
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
`project`, both cleared by the request, with `d` of the first from the at-speed check::

    rst_req (sys flop) --+------------------------------+  async clear (CLR) of both
                         v                              v
    ~override_rst -----------> [ FF1 D ]  Q --> [ FF2 D ]  Q --> project.rst_n (pin)
                                    ^ project clock       ^ project clock

Assertion works with the project clock stopped: the pin goes low as soon as the request
does. Release is synchronous: after the request is released the pin goes high just after the
second project edge (a request released within the setup/recovery window of an edge may take
one more). `rst_n` is therefore never changed except by the output of a project-clock
flip-flop or by the clear of that flip-flop, which only a glitch-free `sys` flip-flop can
drive. The chain starts at 0 (design in reset) on power-up. The at-speed check asserts reset
synchronously through `d` of FF1 (it overrides nothing else on this path), so both reset
sources combine without any logic on the pin.

The `ui_in[6:4]` mux and the gamepad
------------------------------------

With `gamepad_enable` (synchronised into `project`) set, bits 6:4 of the project's `ui_in`
are the gamepad emulator's `data` (bit 6), `clock` (bit 5) and `latch` (bit 4); otherwise,
and for the other five bits always, they are the crossed `ui_in`. The mux is in FRONT of the
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

from migen import ClockDomain, ClockDomainsRenamer, Constant, If, Instance, Module, Mux, Signal
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
        self.clock = None
        self.gamepad = None

        self.clock_domains.cd_project = cd = ClockDomain("project", reset_less=True)
        sd = self.sync.project

        # sys registers that are the sources of the crossings.
        ena_x = Signal()
        gp_x = Signal()
        ce_x = Signal()
        self.rst_req = rst_req = Signal(reset=1)
        self.sync += [
            ena_x.eq(self.ena),
            gp_x.eq(self.gamepad_enable),
            ce_x.eq(self.clock_enable),
            rst_req.eq(~self.rst_n),
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

        # Single-bit levels into project.
        ena_s, gp_s = Signal(), Signal()
        self.specials += [
            MultiReg(ena_x, ena_s, odomain="project", n=2),
            MultiReg(gp_x, gp_s, odomain="project", n=2),
        ]

        # Multi-bit values into project: handshake.
        self.submodules.ui_x = MultiBitCrossing(8)
        self.submodules.buttons_x = MultiBitCrossing(24)
        self.comb += [self.ui_x.src.eq(self.ui_in), self.buttons_x.src.eq(self.gamepad_buttons)]
        ui_cap = self.ui_x.dst

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
        self.comb += rst_d.eq(~self.override_rst)
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
