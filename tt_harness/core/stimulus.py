# SPDX-License-Identifier: Apache-2.0
"""The reference stimulus: the inputs a Tiny Tapeout project sees while being recorded."""

from __future__ import annotations

from migen import Module, Signal

# Reset clocks the reference simulation and the compare build both use (`rst_n` low).
RESET_CLOCKS = 10


class ReferenceStimulus(Module):
    """Fixed project inputs with `rst_n` low for the first `reset_clocks` project clocks.

    `clock_index` is the index of the project clock whose rising edge is next (or is in
    progress); `rst_n` is 0 while `clock_index < reset_clocks`, so project clocks
    0..reset_clocks-1 are reset clocks. `ena` is always 1; `ui_in` and `uio_in` are the
    constructor's constants. All outputs are combinational.
    """

    def __init__(self, ui_in: int, uio_in: int, reset_clocks: int = RESET_CLOCKS) -> None:
        for name, value in (("ui_in", ui_in), ("uio_in", uio_in)):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} must be 0..255, got {value}")
        if not 0 <= reset_clocks < 2**32:
            raise ValueError(f"reset_clocks must be 0..2**32-1, got {reset_clocks}")

        self.clock_index = Signal(32)
        self.ena = Signal()
        self.rst_n = Signal()
        self.ui_in = Signal(8)
        self.uio_in = Signal(8)

        self.comb += [
            self.ena.eq(1),
            self.rst_n.eq(self.clock_index >= reset_clocks),
            self.ui_in.eq(ui_in),
            self.uio_in.eq(uio_in),
        ]
