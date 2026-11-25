# SPDX-License-Identifier: Apache-2.0
"""Brightness of a virtual seven-segment display: the duty cycle of each `uo_out` bit.

On the demo board `uo_out` drives a seven-segment display (segments a-g on bits 0-6, the
decimal point on bit 7). Designs that multiplex or dim segments (PWM, time-sliced digits)
look different from a still image of one instant, so the harness measures how long each
segment is lit over a display frame.

`SevenSegSampler(window_clocks_log2)` runs in the clock domain it is instantiated in (the
design's clock; nothing depends on its frequency). Time is divided into back-to-back windows
of `W = 2**window_clocks_log2` clocks, the first starting at configuration. The caller picks
the window to be about one display frame, e.g. 2**19 clocks = 20.8 ms at 25.175 MHz.

For each of the 8 bits, the number of clocks `c` (0..W) at which the input was high during a
window is turned into an 8-bit `duty`:

    duty = floor((255 * c + W / 2) / W)

that is `c / W * 255` rounded to nearest, halves up. So never high reads 0, high at every
clock reads 255 (c = W), and half the window reads 128 (127.5 rounds up); the mapping is
monotonic, and the error is at most 0.5 of a count. It is computed as `(c << 8) - c + W/2`
shifted right by `window_clocks_log2`: no multiplier.

Timing: the last clock of a window is the clock at which the input is last counted. On the
next clock all eight `duty` registers load the new values and `window` is high for that one
clock, so a reader that acts on `window` sees all eight values from the same window, and the
values hold unchanged until the following `window`. Before the first window ends `duty` is
all zero. Registers: 8 counters of `window_clocks_log2 + 1` bits, a position counter of
`window_clocks_log2` bits, 8 x 8 duty bits and `window`; about 10 * log2 + 73 flip-flops
(e.g. 98 for a 2**19 window with the bits counted separately).
"""

from __future__ import annotations

from migen import Array, If, Module, Signal


class SevenSegSampler(Module):
    """Inputs: `uo_out` (8 bits). Outputs: `duty` (8 x 8 bits, bit 0 first) and `window`."""

    def __init__(self, window_clocks_log2: int) -> None:
        if type(window_clocks_log2) is not int or window_clocks_log2 < 1:
            raise ValueError(
                f"window_clocks_log2 must be an integer of at least 1: {window_clocks_log2!r}"
            )
        self.window_clocks_log2 = window_clocks_log2
        n = window_clocks_log2
        self.uo_out = Signal(8)
        self.duty = Array(Signal(8, name=f"duty{i}") for i in range(8))
        self.window = Signal()

        position = Signal(n)
        last = Signal()
        self.comb += last.eq(position == (1 << n) - 1)
        self.sync += position.eq(position + 1)  # wraps at 2**n: the window length
        self.sync += self.window.eq(last)

        for i in range(8):
            count = Signal(n + 1, name=f"count{i}")
            total = Signal(n + 1, name=f"total{i}")
            scaled = Signal(n + 9, name=f"scaled{i}")
            self.comb += [
                total.eq(count + self.uo_out[i]),  # includes the clock being counted now
                scaled.eq((total << 8) - total + (1 << (n - 1))),
            ]
            self.sync += If(
                last,
                self.duty[i].eq(scaled[n:]),
                count.eq(0),
            ).Else(count.eq(total))
