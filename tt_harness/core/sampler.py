# SPDX-License-Identifier: Apache-2.0
"""Sampler for a Tiny Tapeout project's `uo_out`."""

from __future__ import annotations

from migen import If, Module, Signal


class UoSampler(Module):
    """Registers `uo` when `strobe` is high.

    In the cycle after a strobe, `data` holds the sampled value and `valid` is high for that
    one cycle. `count` is the number of samples taken so far (wrapping at 2**32).
    """

    def __init__(self, width: int = 8) -> None:
        if width < 1:
            raise ValueError(f"width must be at least 1, got {width}")
        self.uo = Signal(width)
        self.strobe = Signal()
        self.data = Signal(width)
        self.valid = Signal()
        self.count = Signal(32)

        self.sync += [
            self.valid.eq(self.strobe),
            If(
                self.strobe,
                self.data.eq(self.uo),
                self.count.eq(self.count + 1),
            ),
        ]
