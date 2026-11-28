# SPDX-License-Identifier: Apache-2.0
"""Clock-domain-crossing building blocks used by `ProjectSlot` and `AtSpeedCheck`.

Two blocks, both the textbook structures:

`AsyncClearFlop`: one flip-flop with an asynchronous clear (Xilinx FDCE). The output is 0
from the moment `clear` goes high, with or without a clock, and follows `d` on rising clock
edges while `clear` is low. Two of them in a chain are the standard "asynchronous assert,
synchronous release" reset synchroniser (see `slot.py`).

`MultiBitCrossing`: a multi-bit value from one clock domain to another with a two-phase
(toggle) request/acknowledge handshake, so the receiving domain only ever sees a value that
was held stable in the sending domain.

Assumptions (all hold for the harness; each is repeated where it matters):

1. The two clocks are unrelated, but neither is faster than about 1000x the other. Either
   clock may stop at any time and for any length, and nothing in one domain waits for the
   other. What is required of a stopped clock is only what a stopped clock gives: its
   registers hold. A stopped receiver delivers nothing until it runs again (the sender goes
   on and the newest value follows); a stopped sender (`rep_x` in `atspeed.py`, whose sender
   is the project clock) leaves `hold` and `req` still, so a transfer in flight is still
   captured whole and its acknowledge simply waits to be seen.
2. `MultiReg` (two flip-flops in the destination domain, no logic between them, marked
   no-retiming; the tools constrain them as `ASYNC_REG`) resolves metastability within one
   destination clock. This is the standard two-flop synchroniser.
3. The `hold` bus below is read by the receiving domain without synchronisers. That is
   safe because `hold` changes only at the sending edge at which `req` toggles, and the
   receiving domain reads it no earlier than two of its own clocks after that edge (`req`
   passes two flip-flops first). Place and route must therefore keep the delay of `hold`
   bits to the capture register below about one receiving-domain clock period (a
   `set_max_delay -datapath_only` constraint; done in the later build task, not here).

Constraints the FPGA build must apply
-------------------------------------

For the build task (also `docs/cdc-constraints.md`); `hold`, `dst`, `req`, `ack_o` are the
registers of each `MultiBitCrossing`, `rst_req` and the reset flops are in `slot.py`, the gate
is `tt_harness/input_gate.py`:

- `set_max_delay -datapath_only` from each `hold` register to its `dst` register:
  - the sys -> project buses (`ctl_x`, `buttons_x`, `cmd_x`): at most the project period
    (15.15 ns at 66 MHz);
  - `rep_x` (project -> sys): at most 20 ns.
- The same bound from each `req`, `ack_o`, single-bit synchroniser source and `LOCKED` to its
  first synchroniser flop.
- `rst_req` to the `FDCE` `CLR` pins: max-delay, or a false path on those pins only.
- Never `set_clock_groups` or a blanket false path: they override max-delay.
- `ASYNC_REG` on every synchroniser pair and on both reset flops.
- No `SRL16` / `SRLC32` on any crossing, in either flow.
- The checks to run:
  - the gate on the openXC7 netlist with the `BUFGCE` output net as `expected`;
  - `vivado_tcl` on the routed design;
  - two negative builds per flow that must be rejected: `pin_ena` from a `sys` register, and
    a LUT on `rst_req` -> `CLR`;
  - the cross-domain path report lists only the held buses and the synchroniser inputs.
"""

from __future__ import annotations

from migen import ClockSignal, If, Instance, Module, Signal, wrap
from migen.fhdl.specials import SPECIAL_INPUT, SPECIAL_OUTPUT, Special
from migen.fhdl.verilog import _printexpr as verilog_printexpr
from migen.genlib.cdc import MultiReg


class AsyncClearFlop(Special):
    """One flip-flop in clock domain `cd`: `q` is 0 while `clear` is high, asynchronously,
    and on a rising edge with `clear` low becomes `d`. Power-up value 0.

    In a synthesis build it lowers to a Xilinx `FDCE` (`CE` tied high, `CLR` = `clear`),
    marked `async_reg`, which both openXC7 (nextpnr-xilinx) and Vivado accept. With
    `behavioural=True` (simulation builds, Verilator) it is emitted as
    `always @(posedge clk or posedge clear)`, which has the same behaviour. A Migen
    simulation supplies its own lowering (`tests/slot_sim.py`), as the repository's other
    asynchronous special (`AsyncSetFlag`) does.
    """

    def __init__(self, cd: str, clear, d, q: Signal | None = None, behavioural: bool = False):
        Special.__init__(self)
        self.cd = cd
        self.clk = ClockSignal(cd)
        self.clear = wrap(clear)
        self.d = wrap(d)
        self.q = Signal() if q is None else q
        self.behavioural = behavioural

    def iter_expressions(self):
        yield self, "clk", SPECIAL_INPUT
        yield self, "clear", SPECIAL_INPUT
        yield self, "d", SPECIAL_INPUT
        yield self, "q", SPECIAL_OUTPUT

    @staticmethod
    def lower(flop):
        if flop.behavioural:
            return None  # emit_verilog
        return _XilinxFdce(flop)

    @staticmethod
    def emit_verilog(flop, ns, add_data_file):
        def pe(e):
            return verilog_printexpr(ns, e)[0]

        state = pe(flop.q) + "_afr"
        return (
            f"reg {state} = 1'd0;\n"
            f"always @(posedge {pe(flop.clk)} or posedge {pe(flop.clear)}) begin\n"
            f"\tif ({pe(flop.clear)})\n\t\t{state} <= 1'd0;\n"
            f"\telse\n\t\t{state} <= {pe(flop.d)};\n"
            f"end\n"
            f"assign {pe(flop.q)} = {state};\n\n"
        )


class _XilinxFdce(Module):
    def __init__(self, flop: AsyncClearFlop):
        self.specials += Instance(
            "FDCE",
            attr={"async_reg"},
            p_INIT=0,
            i_C=flop.clk,
            i_CE=1,
            i_CLR=flop.clear,
            i_D=flop.d,
            o_Q=flop.q,
        )


class MultiBitCrossing(Module):
    """A `width`-bit value from `idomain` to `odomain` through a toggle handshake.

    Ports: `src` (input, `idomain`: any signal of that domain), `dst` (output, a register of
    `odomain`), `valid` (output, `odomain`: high for one `odomain` clock in the cycle after
    `dst` took a new value), `busy` (output, `idomain`: a transfer is in flight, i.e. the
    last value taken from `src` has not yet been delivered and acknowledged). Either direction
    works (`idomain="project", odomain="sys"` carries a value into `sys`); the tests run both.

    Protocol (`req` and `ack` are single toggle flip-flops, one in each domain)::

        idomain:  idle and src != hold  ->  hold <= src, req <= ~req           (edge T0)
        odomain:  req --MultiReg--> req_s;   req_s != ack  ->  dst <= hold, ack <= req_s
        idomain:  ack --MultiReg--> ack_s;   busy = (req != ack_s)

    `hold` changes only at T0, when no transfer is in flight, and not again until the
    acknowledge has come back, so the receiving domain reads a stable bus. A write to `src`
    never waits (nothing here blocks the sender; it just takes the next transfer): if `src`
    changes several times during a transfer only its value at the start of the next
    transfer is sent, so the newest value wins and every value delivered is one that `src`
    really held at an `idomain` clock edge, in order, never a mixture of two. Only
    a `src` that changes and changes back within one `idomain` clock can be missed (and that
    is not a value that was "written").

    Reset. `hold`, `req`, `ack` and `dst` are never reset (a reset in one domain only would
    leave the two toggles disagreeing); they power up at `reset`, so `dst` starts at
    `reset`. A `src` that differs from `reset` at power-up is simply the first transfer.

    Worst case, with Ts the `idomain` period and Tp the `odomain` period (both clocks
    running), from the `idomain` edge at which `src` takes its new value to the edge at
    which `dst` holds it:

    - idle: at most Ts (pick-up) + 3 Tp (two synchroniser flops, then the capture edge);
    - a transfer already in flight: that one completes (at most 3 Tp after it started),
      its acknowledge returns (at most 3 Ts: two synchroniser flops, then the edge that
      starts the next transfer), then 3 Tp for the new one: at most 6 Tp + 3 Ts.

    With the `odomain` clock stopped nothing is delivered and `busy` stays high; `src` keeps
    being accepted into nothing (it is simply read later). After the clock restarts the
    transfer in flight arrives within 3 Tp, and the newest value within a further
    3 Ts + 3 Tp.
    """

    def __init__(self, width: int, reset: int = 0, idomain: str = "sys", odomain: str = "project"):
        self.src = Signal(width, reset=reset)
        self.dst = Signal(width, reset=reset, reset_less=True)
        self.busy = Signal()

        hold = Signal(width, reset=reset, reset_less=True)
        req = Signal(reset_less=True)
        ack_o = Signal(reset_less=True)  # in odomain
        req_s = Signal()  # req in odomain
        ack_s = Signal()  # ack_o in idomain
        self.hold, self.req, self.ack_o, self.req_s, self.ack_s = hold, req, ack_o, req_s, ack_s

        self.specials += [
            MultiReg(req, req_s, odomain=odomain, n=2),
            MultiReg(ack_o, ack_s, odomain=idomain, n=2),
        ]
        self.comb += self.busy.eq(req != ack_s)
        si = getattr(self.sync, idomain)
        so = getattr(self.sync, odomain)
        si += If(~self.busy & (self.src != hold), hold.eq(self.src), req.eq(~req))
        self.valid = Signal()
        so += [
            ack_o.eq(req_s),
            If(req_s != ack_o, self.dst.eq(hold)),
            self.valid.eq(req_s != ack_o),
        ]
