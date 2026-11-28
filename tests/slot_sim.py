# SPDX-License-Identifier: Apache-2.0
"""Migen-simulator harness for `ProjectSlot`: two unrelated clocks, a stoppable project clock,
and a model of the asynchronous-clear flip-flop.

Time unit: 1 ps. `sys` is 20 ns. The project clock's period and phase are the test's choice.

What this models and what it cannot. The simulator is a zero-delay, event-ordered model:
it shows the protocols are functionally right at every relative phase, and nothing about
metastability (a synchroniser's first flop is just a flop here). The structures that matter
for metastability are the standard ones and are named in `tt_harness/core/cdc.py`.

Stopping the project clock. Migen's simulator only produces free-running clocks, so "the
project clock is stopped" is modelled by clock-enable `ce_project` (`CEInserter`) on every
project-domain register, including the flops inside `MultiReg` and the asynchronous-clear
model, which are given gated lowerings here. With `ce_project` low no project-domain
register changes, which is what a stopped clock does. `ce_project` powers up high; a test
that starts with a stopped clock uses `gate(..., running=False)`.

Asynchronous clear. `AsyncClearFlop` is modelled with its clear taking effect combinationally
(in the same simulator step as the request) and its release held until the next project
clock edge. The "release held until an edge" memory is one flag in a fast `tick` domain
(2 ns) that notices a project edge by a toggle flip-flop, so the model's release follows
an edge by at most one tick. The real cell has no such delay; the Verilator tests use the
cell as Migen emits it (`behavioural=True`) with exact timing.
"""

from __future__ import annotations

from migen import CEInserter, Constant, If, Module, Mux, Signal
from migen.genlib.cdc import MultiReg
from migen.sim import run_simulation

from tt_harness.core.cdc import AsyncClearFlop

SYS_PERIOD = 20_000
TICK_PERIOD = 2_000


def edge_times(period: int, offset: int, until: int) -> list[int]:
    """Times of the rising edges of a project clock given as (period, offset): the first
    rising edge is at time `offset` (> 0) and then every `period`."""
    assert period % 2 == 0 and offset > 0
    return list(range(offset, until + 1, period))


def _lowerings(ce: Signal, late: tuple = ()):
    class _Model(Module):
        def __init__(self, f: AsyncClearFlop):
            qd = Signal(reset_less=True)
            tog = Signal(reset_less=True)
            armed = Signal(reset_less=True)
            tog_at = Signal(reset_less=True)
            sp = getattr(self.sync, f.cd)
            sp += If(
                ce,
                tog.eq(~tog),
                If(f.clear, qd.eq(0)).Else(qd.eq(f.d)),
            )
            self.sync.tick += If(f.clear, armed.eq(1), tog_at.eq(tog)).Elif(
                armed & (tog != tog_at), armed.eq(0)
            )
            self.comb += f.q.eq(Mux(f.clear | armed, 0, qd))

    class _Flop:
        @staticmethod
        def lower(f):
            return _Model(f)

    class _GatedMultiRegImpl(Module):
        def __init__(self, dr: MultiReg):
            # A synchroniser one project clock slower than the others models the worst skew
            # between two signals that changed on the same system edge (a first flop that
            # resolves late); `late` lists the signals whose crossing is made slower.
            stages = dr.n + (1 if any(dr.i is x for x in late) else 0)
            regs = [Signal(len(dr.i), reset_less=True) for _ in range(stages)]
            sd = getattr(self.sync, dr.odomain)
            src = dr.i
            for reg in regs:
                if dr.odomain == "project":
                    sd += If(ce, reg.eq(src))
                else:
                    sd += reg.eq(src)
                src = reg
            self.comb += dr.o.eq(src)

    class _MultiReg:
        @staticmethod
        def lower(dr):
            return _GatedMultiRegImpl(dr)

    return {AsyncClearFlop: _Flop, MultiReg: _MultiReg}


def gate(module: Module, running: bool = True) -> Module:
    """`module` with a clock enable on its project domain: `module.ce_project`, which
    powers up high (clock running) or, with `running=False`, low (clock stopped)."""
    m = CEInserter(["project"])(module)
    m.ce_project.reset = Constant(1 if running else 0)
    return m


def run(
    dut,
    sys_gens=(),
    project_gens=(),
    tick_gens=(),
    project: tuple[int, int] | None = (39_700, 5_050),
    vcd: str | None = None,
    late: tuple = (),
) -> None:
    """Run `dut` (made with `gate`) until every non-passive generator has finished.

    `late`: signals whose `MultiReg` into `project` gets one extra stage (skew injection).
    `project` is the project clock's (period, offset): its first rising edge is at time
    `offset` ps and it then rises every `period` ps (period even). None is a clock that
    never toggles at all (its domain has no clock in the simulation). Offsets that are
    not a multiple of 100 ps never coincide with a `sys` edge (periods are multiples of
    100 ps).
    """
    clocks = {"sys": SYS_PERIOD, "tick": TICK_PERIOD}
    gens = {"sys": list(sys_gens), "tick": list(tick_gens)}
    if project is not None:
        period, offset = project
        clocks["project"] = (period, period // 2 - offset)
        gens["project"] = list(project_gens)
    run_simulation(
        dut,
        gens,
        clocks=clocks,
        special_overrides=_lowerings(dut.ce_project, tuple(late)),
        vcd_name=vcd,
    )
