# SPDX-License-Identifier: Apache-2.0
"""The reference stimulus: the inputs a Tiny Tapeout project sees while being recorded.

Every output is glitch-free: a constant, or the output of a register. A project may use any
input asynchronously (`rst_n` as an asynchronous reset is common), and the output of
combinational logic can glitch when its inputs change. `rst_n` was once the comparison
`clock_index >= reset_clocks` itself; a glitch on it cleared a design's asynchronously reset
counter on hardware (docs/results/2026-11-spike-a-compare-build.md, "After Task 9b").
"""

from __future__ import annotations

from migen import If, Module, Signal

# Reset clocks the reference simulation and the compare build both use (`rst_n` low).
RESET_CLOCKS = 10


class ReferenceStimulus(Module):
    """Fixed project inputs with `rst_n` low for the first `reset_clocks` project clocks.

    `clock_index` is the index of the project clock whose rising edge is next (or is in
    progress), and `advance` is high in the system cycle at whose end it increments (the
    recorder's `clock_index` and `advance`). `rst_n` is a system-domain register updated on
    that same edge with `clock_index + 1 >= reset_clocks`, so it changes in the very cycle
    `clock_index` does and is 0 exactly while `clock_index < reset_clocks`: project clocks
    0..reset_clocks-1 are reset clocks. Its reset value is 0 (1 if `reset_clocks` is 0).
    `ena` is always 1; `ui_in` and `uio_in` are the constructor's constants.
    """

    def __init__(self, ui_in: int, uio_in: int, reset_clocks: int = RESET_CLOCKS) -> None:
        for name, value in (("ui_in", ui_in), ("uio_in", uio_in)):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} must be 0..255, got {value}")
        if not 0 <= reset_clocks < 2**32:
            raise ValueError(f"reset_clocks must be 0..2**32-1, got {reset_clocks}")

        self.clock_index = Signal(32)
        self.advance = Signal()
        self.ena = Signal()
        self.rst_n = Signal(reset=1 if reset_clocks == 0 else 0)
        self.ui_in = Signal(8)
        self.uio_in = Signal(8)

        self.comb += [
            self.ena.eq(1),
            self.ui_in.eq(ui_in),
            self.uio_in.eq(uio_in),
        ]
        # The only logic is in front of the register; nothing combinational follows it.
        self.sync += If(self.advance, self.rst_n.eq(self.clock_index + 1 >= reset_clocks))
