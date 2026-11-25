# SPDX-License-Identifier: Apache-2.0
"""Measure, capture and buffer the VGA picture a Tiny Tapeout design drives on `uo_out`.

(The module documentation is completed once all blocks are in.)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from migen import Cat, If, Module, Mux, Signal

PIXEL_BITS = 6

# Pinout of the Tiny VGA Pmod on `uo_out`: uo[0]=R1 uo[1]=G1 uo[2]=B1 uo[3]=vsync uo[4]=R0
# uo[5]=G0 uo[6]=B0 uo[7]=hsync.
BIT_R1, BIT_G1, BIT_B1, BIT_VSYNC, BIT_R0, BIT_G0, BIT_B0, BIT_HSYNC = 0, 1, 2, 3, 4, 5, 6, 7


def decode(uo_out):
    """`(r, g, b, hsync, vsync)` of a `uo_out` value: an int, or a Migen signal or expression.

    r, g and b are 2 bits (R1 is the high bit); hsync and vsync are the pin levels, as they
    are, not corrected for polarity.
    """
    if isinstance(uo_out, int):

        def bit(i: int) -> int:
            return (uo_out >> i) & 1

        return (
            bit(BIT_R1) << 1 | bit(BIT_R0),
            bit(BIT_G1) << 1 | bit(BIT_G0),
            bit(BIT_B1) << 1 | bit(BIT_B0),
            bit(BIT_HSYNC),
            bit(BIT_VSYNC),
        )
    return (
        Cat(uo_out[BIT_R0], uo_out[BIT_R1]),
        Cat(uo_out[BIT_G0], uo_out[BIT_G1]),
        Cat(uo_out[BIT_B0], uo_out[BIT_B1]),
        uo_out[BIT_HSYNC],
        uo_out[BIT_VSYNC],
    )


def pack(r: int, g: int, b: int) -> int:
    """The framebuffer pixel `{r[1:0], g[1:0], b[1:0]}`: red in the top two bits."""
    return (r & 3) << 4 | (g & 3) << 2 | (b & 3)


def unpack(pixel: int) -> tuple[int, int, int]:
    """`(r, g, b)`, each 0..3, of a framebuffer pixel."""
    return (pixel >> 4) & 3, (pixel >> 2) & 3, pixel & 3


# The mode table ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeSpec:
    """A standard video mode, in pixel clocks and lines, as the table of known modes.

    `dividers` lists the project-clock-to-pixel-clock ratios a design may run it at.
    """

    name: str
    width: int
    height: int
    h_front: int
    h_sync: int
    h_back: int
    v_front: int
    v_sync: int
    v_back: int
    pixel_clock_hz: int
    dividers: tuple[int, ...] = (1,)

    @property
    def h_total(self) -> int:
        return self.width + self.h_front + self.h_sync + self.h_back

    @property
    def v_total(self) -> int:
        return self.height + self.v_front + self.v_sync + self.v_back

    @property
    def refresh_hz(self) -> float:
        return self.pixel_clock_hz / (self.h_total * self.v_total)


# VESA timings. Extend by adding a row: `classify` and the C header follow.
MODES: tuple[ModeSpec, ...] = (
    ModeSpec("640x480@60", 640, 480, 16, 96, 48, 10, 2, 33, 25_175_000, (1, 2)),
    ModeSpec("640x480@72", 640, 480, 24, 40, 128, 9, 3, 28, 31_500_000),
    ModeSpec("800x600@60", 800, 600, 40, 128, 88, 1, 4, 23, 40_000_000, (1, 2)),
    ModeSpec("800x600@72", 800, 600, 56, 120, 64, 37, 6, 23, 50_000_000),
    ModeSpec("1024x768@60", 1024, 768, 24, 136, 160, 3, 6, 29, 65_000_000),
)


@dataclass(frozen=True)
class Mode:
    """A classified mode and the settings it implies for `VgaCapture`.

    `h_offset` is in project clocks, `v_offset` in lines; `width` and `height` are the mode's
    picture size in pixels (the framebuffer may be smaller: cropped or decimated).
    """

    name: str
    divider: int
    h_offset: int
    v_offset: int
    width: int
    height: int


def classify(
    clocks_per_line: int,
    lines_per_frame: int,
    hsync_width: int,
    vsync_lines: int,
    clock_hz: int,
) -> Mode | None:
    """Match measured timing (`VgaTiming`'s outputs) to a known mode; None if there is none.

    A table entry matches when `lines_per_frame` equals its line total and `clocks_per_line`
    is its pixel total times one of its allowed dividers. The offsets use the MEASURED sync
    widths plus the mode's standard back porch (times the divider horizontally), so a design
    whose sync pulse is a little unusual still lands on its picture:

        h_offset = hsync_width + h_back * divider        v_offset = vsync_lines + v_back

    A match is refused if there is no sync pulse or the picture would not fit in the line or
    the frame after those offsets.

    `clock_hz` (the project clock; 0 if unknown) only fills in the refresh rate in the mode's
    name, `clock_hz / (clocks_per_line * lines_per_frame)`. It never decides or vetoes a
    match: designs run VGA timing off slightly wrong clocks all the time.
    """
    if hsync_width < 1 or vsync_lines < 1:
        return None
    for spec in MODES:
        if lines_per_frame != spec.v_total:
            continue
        for divider in spec.dividers:
            if clocks_per_line != spec.h_total * divider:
                continue
            h_offset = hsync_width + spec.h_back * divider
            v_offset = vsync_lines + spec.v_back
            if h_offset + spec.width * divider > clocks_per_line:
                return None
            if v_offset + spec.height > lines_per_frame:
                return None
            if clock_hz > 0:
                refresh = f"{clock_hz / (clocks_per_line * lines_per_frame):.1f}Hz"
            else:
                refresh = f"{spec.refresh_hz:.1f}Hz"
            name = f"{spec.width}x{spec.height}@{refresh}"
            if divider > 1:
                name += f" (clock x{divider})"
            return Mode(name, divider, h_offset, v_offset, spec.width, spec.height)
    return None


def modes_header() -> str:
    """The C header of the mode table, committed as `tt_harness/core/vga_modes.h`."""
    rows = []
    for spec in MODES:
        for d in spec.dividers:
            rows.append(
                f'    {{"{spec.name}", {d}, {spec.width}, {spec.height}, '
                f"{spec.h_total * d}, {spec.v_total}, {spec.h_sync * d}, {spec.h_back * d}, "
                f"{spec.v_sync}, {spec.v_back}, {spec.pixel_clock_hz * d}}},"
            )
    return (
        "/* SPDX-License-Identifier: Apache-2.0 */\n"
        "/* Generated by tt_harness/core/vga.py (modes_header); do not edit.\n"
        " * Regenerate: uv run python -m tt_harness.core.vga > tt_harness/core/vga_modes.h\n"
        " *\n"
        " * One row per (mode, divider). A measurement matches a row when clocks_per_line and\n"
        " * lines_per_frame are equal to the row's. The capture offsets are then\n"
        " *     h_offset = measured_hsync_width + h_back        (project clocks)\n"
        " *     v_offset = measured_vsync_lines + v_back        (lines)\n"
        " * h_sync and h_back are the standard values in project clocks (already multiplied by\n"
        " * the divider); pixel_clock_hz is the nominal project clock for the row.\n"
        " */\n"
        "#ifndef TT_HARNESS_VGA_MODES_H\n"
        "#define TT_HARNESS_VGA_MODES_H\n"
        "\n"
        "#include <stdint.h>\n"
        "\n"
        "struct vga_mode {\n"
        "    const char *name;\n"
        "    uint16_t divider;\n"
        "    uint16_t width;\n"
        "    uint16_t height;\n"
        "    uint16_t clocks_per_line;\n"
        "    uint16_t lines_per_frame;\n"
        "    uint16_t h_sync;\n"
        "    uint16_t h_back;\n"
        "    uint16_t v_sync;\n"
        "    uint16_t v_back;\n"
        "    uint32_t pixel_clock_hz;\n"
        "};\n"
        "\n"
        "static const struct vga_mode vga_modes[] = {\n" + "\n".join(rows) + "\n};\n"
        "\n"
        f"#define VGA_MODE_COUNT {len(rows)}\n"
        "\n"
        "#endif /* TT_HARNESS_VGA_MODES_H */\n"
    )


# VgaTiming ---------------------------------------------------------------------------------

CLOCKS_MAX = (1 << 12) - 1  # saturation value of every clock count
LINES_MAX = (1 << 11) - 1  # saturation value of every line count
NO_SIGNAL_CLOCKS = 1 << 23  # 8,388,608 project clocks


def _count_up(counter, limit: int):
    """Statement: `counter` plus one, stopping at `limit` instead of wrapping."""
    return If(counter != limit, counter.eq(counter + 1))


def _smaller_of(a, b):
    return Mux(a < b, a, b)


class VgaTiming(Module):
    """Measure the timing a design's own sync signals have, in project clocks and lines.

    Input `uo_out` (8 bits, Tiny VGA pinout). Outputs, all registers, valid together:

    - `clocks_per_line` (12 bits): clocks from one hsync leading edge to the next;
    - `hsync_width` (12): clocks hsync is active;
    - `lines_per_frame` (11): hsync leading edges from one vsync leading edge to the next;
    - `vsync_lines` (11): lines vsync is active;
    - `hsync_positive`, `vsync_positive`: 1 if the sync pulse is the high level;
    - `frames` (16): publications so far, wrapping;
    - `locked`: the last two publications were identical in all six values above (and
      none of the four counts was zero);
    - `no_signal`: no vsync pulse within `no_signal_clocks`, and from reset until the first
      publication. While it is high the measurements are stale and must not be used.

    No mode is assumed. The measurement does not depend on knowing the polarity first, so
    it needs no trial and error: hsync is measured as two phases, the clocks it is high and
    the clocks it is low, per line, and vsync likewise as the lines it is high and low per
    frame. The sync pulse is the SHORTER phase (a tie reads as negative sync). A "line" is
    the period between hsync rising edges and a "frame" the period between vsync rising
    edges, whatever the polarity; the period is the same between any two like edges, so
    this equals the leading-edge-to-leading-edge definition. A hsync rising edge is counted
    towards the vsync phase the vsync pin has at that clock, so a vsync edge on the same
    clock as an hsync edge puts that line in the new phase.

    Convergence: the first vsync rising edge after reset only starts a frame (the frame
    before it is partial). The second publishes the first complete frame: all six values
    right, `frames` = 1, `no_signal` low, `locked` still low. The third publishes the next
    and `locked` rises if it is the same. That is, locked at the third vsync rising edge,
    at most three frame times after reset, for either polarity. Publication is the clock
    after the vsync rising edge; every output changes on that clock and no other.

    Per line the measurement used is that of the last complete line before the vsync edge.

    Saturation: the four counters of the phases (hsync high/low clocks, vsync high/low
    lines) stop at 4095 / 2047, so `clocks_per_line` and `lines_per_frame` read their
    maximum for anything longer, never a wrapped small number. A hsync that never changes
    keeps counting one phase to the maximum and then its line length reads 4095, with
    `hsync_width` 0 and, as no line ends, `lines_per_frame` 0; `classify` finds no mode
    there. A vsync that never changes gives no publication, and `no_signal`.

    The first clock after reset is never an edge (the inputs are not yet compared with
    anything). Registers: about 164 flip-flops.
    """

    def __init__(self, no_signal_clocks: int = NO_SIGNAL_CLOCKS) -> None:
        if type(no_signal_clocks) is not int or no_signal_clocks < 2:
            raise ValueError(
                f"no_signal_clocks must be an integer of at least 2: {no_signal_clocks!r}"
            )
        self.no_signal_clocks = no_signal_clocks
        self.uo_out = Signal(8)
        self.clocks_per_line = Signal(12)
        self.lines_per_frame = Signal(11)
        self.hsync_width = Signal(12)
        self.vsync_lines = Signal(11)
        self.hsync_positive = Signal()
        self.vsync_positive = Signal()
        self.frames = Signal(16)
        self.locked = Signal()
        self.no_signal = Signal(reset=1)

        _, _, _, hsync, vsync = decode(self.uo_out)
        primed = Signal()  # low only in the first clock after reset
        hsync_q, vsync_q = Signal(), Signal()
        h_rise, v_rise = Signal(), Signal()
        self.sync += [primed.eq(1), hsync_q.eq(hsync), vsync_q.eq(vsync)]
        self.comb += [
            h_rise.eq(primed & hsync & ~hsync_q),
            v_rise.eq(primed & vsync & ~vsync_q),
        ]

        # Per line: clocks high and low since the last hsync rising edge, and the last
        # complete line's.
        h_high, h_low = Signal(12), Signal(12)
        line_high, line_low = Signal(12), Signal(12)
        self.sync += [
            If(hsync, _count_up(h_high, CLOCKS_MAX)).Else(_count_up(h_low, CLOCKS_MAX)),
            # A phase that has reached the maximum: report it at once, no edge will come.
            If(
                h_rise | (h_high == CLOCKS_MAX) | (h_low == CLOCKS_MAX),
                line_high.eq(h_high),
                line_low.eq(h_low),
            ),
            If(h_rise, h_high.eq(1), h_low.eq(0)),
        ]

        # Per frame: hsync rising edges while vsync is high and low.
        v_high, v_low = Signal(11), Signal(11)
        self.sync += If(
            h_rise,
            If(vsync, _count_up(v_high, LINES_MAX)).Else(_count_up(v_low, LINES_MAX)),
        )

        # The numbers a publication would carry.
        line_total = Signal(13)
        frame_total = Signal(12)
        new_clocks, new_lines = Signal(12), Signal(11)
        new_hwidth, new_vlines = Signal(12), Signal(11)
        new_hpos, new_vpos = Signal(), Signal()
        self.comb += [
            line_total.eq(line_high + line_low),
            new_clocks.eq(Mux(line_total > CLOCKS_MAX, CLOCKS_MAX, line_total)),
            frame_total.eq(v_high + v_low),
            new_lines.eq(Mux(frame_total > LINES_MAX, LINES_MAX, frame_total)),
            new_hwidth.eq(_smaller_of(line_high, line_low)),
            new_vlines.eq(_smaller_of(v_high, v_low)),
            new_hpos.eq(line_high < line_low),
            new_vpos.eq(v_high < v_low),
        ]
        same = Signal()
        usable = Signal()
        self.comb += [
            same.eq(
                (new_clocks == self.clocks_per_line)
                & (new_lines == self.lines_per_frame)
                & (new_hwidth == self.hsync_width)
                & (new_vlines == self.vsync_lines)
                & (new_hpos == self.hsync_positive)
                & (new_vpos == self.vsync_positive)
            ),
            usable.eq((new_clocks != 0) & (new_lines != 0) & (new_hwidth != 0) & (new_vlines != 0)),
        ]

        armed = Signal()  # a vsync rising edge has started the frame being measured
        fresh = Signal()  # the values now published come from the previous frame
        idle = Signal(max=no_signal_clocks)
        self.sync += [
            If(
                idle == no_signal_clocks - 1,
                self.no_signal.eq(1),
                self.locked.eq(0),
                armed.eq(0),
                fresh.eq(0),
            ).Else(idle.eq(idle + 1)),
            If(
                v_rise,
                idle.eq(0),
                armed.eq(1),
                v_high.eq(h_rise),  # the line starting now is in the new frame's high phase
                v_low.eq(0),
                If(
                    armed,
                    self.clocks_per_line.eq(new_clocks),
                    self.lines_per_frame.eq(new_lines),
                    self.hsync_width.eq(new_hwidth),
                    self.vsync_lines.eq(new_vlines),
                    self.hsync_positive.eq(new_hpos),
                    self.vsync_positive.eq(new_vpos),
                    self.frames.eq(self.frames + 1),
                    self.no_signal.eq(0),
                    self.locked.eq(fresh & same & usable),
                    fresh.eq(1),
                ),
            ),
        ]


if __name__ == "__main__":
    sys.stdout.write(modes_header())
