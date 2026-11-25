# SPDX-License-Identifier: Apache-2.0
"""The compare build's top: stepping recorder, reference stimulus and the project.

The project's top module is instantiated by name as a black box; its sources are added to
the simulation or the FPGA build separately. `pclk` (a system-domain register in the
recorder) clocks the project, either directly (simulation) or through a clock buffer that the
board layer supplies as `clock_buffer` (a global buffer on an FPGA).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from migen import ClockDomain, Instance, Module, Signal
from migen.fhdl import verilog
from migen.fhdl.specials import Special

from tt_harness.compare.recorder import DEFAULT_SETTLE, StepRecorder
from tt_harness.core.stimulus import RESET_CLOCKS, ReferenceStimulus
from tt_harness.manifest import Project

MODULE_NAME = "compare_top"
PROJECT_INSTANCE = "project"

ClockBuffer = Callable[[Signal, Signal], Special]


class CompareTop(Module):
    """Records `clocks` project clocks of `project` and sends them as one frame on `tx`.

    Ports: `clk` and `rst` (the system clock domain, `sys_clk_freq` Hz, synchronous reset),
    output `tx` (UART at `baud`, 8N1) and output `done` (the whole frame has been sent).
    `clock_buffer(pclk, project_clk)` returns the special that drives the project's clock
    from the recorder's `pclk`; without it the two are wired together.

    `pclk_period_cycles` is the shortest project clock period, in system cycles.
    """

    def __init__(
        self,
        project: Project,
        sys_clk_freq: int,
        baud: int,
        clocks: int,
        settle: int = DEFAULT_SETTLE,
        reset_clocks: int = RESET_CLOCKS,
        clock_buffer: ClockBuffer | None = None,
    ) -> None:
        self.clk = Signal()
        self.rst = Signal()
        self.tx = Signal()
        self.done = Signal()
        self.settle = settle
        self.pclk_period_cycles = 2 * (settle + 1)

        self.clock_domains.cd_sys = ClockDomain()
        self.comb += [self.cd_sys.clk.eq(self.clk), self.cd_sys.rst.eq(self.rst)]

        self.submodules.recorder = rec = StepRecorder(sys_clk_freq, baud, clocks, settle)
        self.submodules.stimulus = stim = ReferenceStimulus(
            project.ui_in, project.uio_in, reset_clocks
        )
        # Every signal into the project is a constant or a register output
        # (`tt_harness.compare.inputs`). `keep` on the register output makes the tools
        # preserve that net by name, so that the build can check what drives it in the
        # implemented netlist (`tt_harness.input_gate`). The constant inputs are not marked:
        # the gate recognises them from the generated top, and `keep` on a constant net
        # stops Vivado propagating the constant into the project (2048: 986 LUTs instead
        # of 207).
        stim.rst_n.attr.add("keep")
        self.pclk = rec.pclk
        self.project_clk = Signal(name_override="project_clk")
        if clock_buffer is None:
            self.comb += self.project_clk.eq(rec.pclk)
        else:
            self.specials += clock_buffer(rec.pclk, self.project_clk)

        uo_out = Signal(8)
        uio_out = Signal(8)
        uio_oe = Signal(8)
        self.specials += Instance(
            project.top_module,
            name=PROJECT_INSTANCE,
            i_clk=self.project_clk,
            i_rst_n=stim.rst_n,
            i_ena=stim.ena,
            i_ui_in=stim.ui_in,
            i_uio_in=stim.uio_in,
            o_uo_out=uo_out,
            o_uio_out=uio_out,
            o_uio_oe=uio_oe,
        )
        self.comb += [
            stim.clock_index.eq(rec.clock_index),
            stim.advance.eq(rec.advance),
            rec.uo.eq(uo_out),
            self.tx.eq(rec.tx),
            self.done.eq(rec.done),
        ]


def to_verilog(top: CompareTop, path: Path) -> Path:
    """Write `top` as the Verilog module `compare_top` (ports clk, rst, tx, done) to `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = verilog.convert(top, ios={top.clk, top.rst, top.tx, top.done}, name=MODULE_NAME)
    out.write(str(path))
    return path
