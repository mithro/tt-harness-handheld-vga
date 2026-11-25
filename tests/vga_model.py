# SPDX-License-Identifier: Apache-2.0
"""A generator of the per-clock `uo_out` sequence of a Tiny VGA design, for the VGA tests.

Test code: simple and obviously right. It knows nothing of `tt_harness.core.vga` (not its
pinout helpers, not its mode table); every test states its own numbers.

The design is modelled the way a Tiny Tapeout design is written: an x counter 0..h_total-1
and a y counter 0..v_total-1 that advances when x wraps. A line is

    x = 0 .. h_active-1                     picture
    then h_front clocks, h_sync clocks of hsync, h_back clocks

and a frame likewise vertically, with the vsync pulse after v_front lines. At divider D the
design's clock is D times the pixel clock: each pixel is held for D clocks.

`vsync_phase` says where within a line the vsync level changes: "line_start" (x = 0, as a
counter that compares y does) or "hsync" (at the first clock of the hsync pulse, so both
sync edges coincide). `dirty` makes the clocks of a divided pixel other than the middle one
(clock D // 2) carry a wrong colour, to show which clock a capture samples.

Pin assignment (written out here, from the Tiny VGA Pmod and the vgacap stream map):
uo[0]=R1 uo[1]=G1 uo[2]=B1 uo[3]=vsync uo[4]=R0 uo[5]=G0 uo[6]=B0 uo[7]=hsync.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

Picture = Callable[[int, int], int]  # (x, y) -> 6-bit colour {r1 r0 g1 g0 b1 b0}


def encode(colour: int, hsync_pin: int, vsync_pin: int) -> int:
    """The `uo_out` byte for a 6-bit colour and the levels of the two sync pins."""
    r1, r0 = (colour >> 5) & 1, (colour >> 4) & 1
    g1, g0 = (colour >> 3) & 1, (colour >> 2) & 1
    b1, b0 = (colour >> 1) & 1, colour & 1
    return (
        (r1 << 0)
        | (g1 << 1)
        | (b1 << 2)
        | (vsync_pin << 3)
        | (r0 << 4)
        | (g0 << 5)
        | (b0 << 6)
        | (hsync_pin << 7)
    )


def default_picture(x: int, y: int) -> int:
    """A deterministic picture with no black pixel: colours 1..63."""
    return (x * 5 + y * 3) % 63 + 1


@dataclass(frozen=True)
class VgaModel:
    h_active: int
    h_front: int
    h_sync: int
    h_back: int
    v_active: int
    v_front: int
    v_sync: int
    v_back: int
    hsync_positive: bool = False
    vsync_positive: bool = False
    divider: int = 1
    vsync_phase: str = "line_start"
    dirty: bool = False
    picture: Picture = default_picture

    @property
    def h_total(self) -> int:
        return self.h_active + self.h_front + self.h_sync + self.h_back

    @property
    def v_total(self) -> int:
        return self.v_active + self.v_front + self.v_sync + self.v_back

    @property
    def clocks_per_line(self) -> int:
        return self.h_total * self.divider

    @property
    def clocks_per_frame(self) -> int:
        return self.clocks_per_line * self.v_total

    def _hsync_active(self, x: int) -> bool:
        start = self.h_active + self.h_front
        return start <= x < start + self.h_sync

    def _vsync_active(self, x: int, y: int) -> bool:
        if self.vsync_phase == "hsync" and x < self.h_active + self.h_front:
            y = (y - 1) % self.v_total
        start = self.v_active + self.v_front
        return start <= y < start + self.v_sync

    def pixel(self, x: int, y: int, sub: int = 0) -> int:
        """`uo_out` at design pixel (x, y), clock `sub` of its `divider` clocks."""
        hs = self._hsync_active(x)
        vs = self._vsync_active(x, y)
        colour = 0
        if x < self.h_active and y < self.v_active:
            colour = self.picture(x, y)
            if self.dirty and self.divider > 1 and sub != self.divider // 2:
                colour = 0x3F ^ colour
        hs_pin = int(hs == self.hsync_positive)
        vs_pin = int(vs == self.vsync_positive)
        return encode(colour, hs_pin, vs_pin)

    def line(self, y: int) -> list[int]:
        return [self.pixel(x, y, sub) for x in range(self.h_total) for sub in range(self.divider)]

    def stream(self, clocks: int, skip: int = 0) -> Iterator[int]:
        """`clocks` consecutive `uo_out` values, starting `skip` clocks into frame 0."""
        lines = [self.line(y) for y in range(self.v_total)]
        flat = [v for line in lines for v in line]
        for i in range(clocks):
            yield flat[(skip + i) % len(flat)]
