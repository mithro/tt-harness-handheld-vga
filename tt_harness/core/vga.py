# SPDX-License-Identifier: Apache-2.0
"""Measure, capture and buffer the VGA picture a Tiny Tapeout design drives on `uo_out`.

A Tiny Tapeout VGA design generates its own video timing from its own clock and drives the
"Tiny VGA" Pmod from its 8-bit output `uo_out`. The harness shows that picture on a screen it
controls, in three blocks (all in the design's clock domain, `project`, except the read side
of the framebuffer):

- `VgaTiming` measures the timing the design produces, from the sync pins alone;
- `VgaCapture` writes the visible picture into a write port, using offsets and a divider that
  software sets after `classify` has recognised the measured timing;
- `Framebuffer` holds it in block RAM and offers it to a reader in another clock domain.

Pinout of `uo_out` (the Tiny VGA Pmod): uo[0]=R1 uo[1]=G1 uo[2]=B1 uo[3]=vsync uo[4]=R0
uo[5]=G0 uo[6]=B0 uo[7]=hsync, R1/G1/B1 being the high bit of each 2-bit channel. Sources: the
vgacap signal map `TINYVGA_SIGNAL_MAP` in `tt_harness.host.frame` (signals hsync, vsync, R1, R0,
G1, G0, B1, B0 at bits 7, 3, 0, 4, 1, 5, 2, 6; vgacap `include/vgacap/stream.h`), and the
design `tt07/tt_um_tinytapeout_dvd_screensaver`, whose top level has `assign uo_out =
{hsync, B[0], G[0], R[0], vsync, B[1], G[1], R[1]};`. Pins are levels, uncorrected for
polarity.

Pixel format of the framebuffer: 6 bits per pixel, `{r[1:0], g[1:0], b[1:0]}`, red in the top
two bits (`pack`, `unpack`).

The mode table (`MODES`) is data: VESA 640x480@60 and @72, 800x600@60 and @72, 1024x768@60,
the 60 Hz modes of 640x480 and 800x600 also at divider 2 (a design clocked at twice its pixel
rate). `classify` matches measurements to it and gives the capture settings; `modes_header()`
emits the same table as the C header `vga_modes.h` for firmware.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from migen import READ_FIRST, Cat, If, Memory, Module, Mux, Signal, bits_for

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

    The vertical offset is the convention of `VgaCapture`'s docstring: the same as vgacap's
    and the reference renders'; the picture lands on the design's own rows one line low
    for most counter-based designs, and software may adjust `v_offset` by a line or two.
    Known limit: a hsync (or vsync) with exactly 50% duty reads as negative polarity
    (`VgaTiming` takes the shorter phase, a tie is negative); that is not a valid video mode.

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


# VgaCapture --------------------------------------------------------------------------------


class VgaCapture(Module):
    """Write the visible picture of a design into a `width` x `height` framebuffer.

    Inputs: `uo_out`; `hsync_positive`, `vsync_positive` (from `VgaTiming`); and the settings
    software computes from the classified mode:

    - `divider` (3 bits, 1..7; 0 is taken as 1): project clocks per pixel;
    - `h_offset` (12 bits): project clocks from the hsync leading edge to the first clock of
      the picture's first pixel (hsync width plus back porch; 0 is taken as 1);
    - `v_offset` (11 bits): lines from the vsync leading edge to the picture's first line
      (vsync lines plus back porch; 0 is taken as 1);
    - `h_step`, `v_step` (2 bits, 1..3; 0 is taken as 1): 1 stores every pixel and line, 2
      every second one (the quarter-size buffer), the first of each group.

    `blank` (input): see below. Outputs, all registers: the write port `we`, `addr`
    (`y * width + x`, row-major) and `data` (6 bits, `{r[1:0], g[1:0], b[1:0]}`, red in the
    top bits, `unpack` gives r, g, b), and `frame_done`. `addr` is meaningful only while `we`.

    Timing, in clocks, where clock e is the one in which `uo_out` first shows the hsync
    leading edge (the pin goes to its active level; the clock before it did not):

    - Pixel x of a stored row is sampled in clock `e + h_offset + x * h_step * divider +
      divider // 2`: the middle clock of its `divider` clocks (for divider 2 the second, 3 the
      second, 4 the third). A pixel is held for `divider` clocks by the design, so the
      other clocks are not used; a design that changes colour within a pixel is sampled
      mid-pixel. `we`, `addr` and `data` are high one clock after the sampled clock.
    - Vertical convention. Lines are counted from the vsync leading edge as the vgacap frame
      reconstruction does, and the reference renders (which this project compares with) do:
      the first hsync leading edge at or after the clock of that edge (one on the same clock
      counts) starts line 0, each later hsync leading edge the next line, and row y is stored
      from line `v_offset + y * v_step`, pixels counted from that line's own hsync leading
      edge. The picture top is therefore `v_offset` = vsync lines + back porch lines after
      the vsync edge, the way a standard monitor reads the mode. How that lands on the
      design's own rows depends on where the design puts its vsync edge in the line, which
      the sync pins alone cannot tell. For a design whose vsync edge precedes its hsync
      edge in the same line (the common tt-vga template: a counter-based design whose
      line starts in the picture, before the hsync pulse, and whose vsync changes at the
      line start) the hsync edge that starts "line k" is followed by the design's line
      k + 1: the design's own line 0 is not shown and one blank line appears at the bottom
      (the DVD screensaver: row 0 is its line 1). With vsync after the hsync edge in the line
      it is two lines. `v_offset` is a register: software may adjust it by a line or two.
    - Pixels with x >= width and rows with y >= height are not written, so a larger mode
      is cropped to its top left.
    - `frame_done` is high for one clock, the one after the write of address
      `width * height - 1`: the buffer's last pixel of its last row. It follows every frame
      whose picture covers the buffer.

    `blank`: sampled at each vsync leading edge; while it was high there, every pixel of
    that frame is written as zero (software drives it with `no_signal`). Also, when `blank`
    goes high with no clear done since it last was low, the whole buffer is cleared at once:
    `we` high for `width * height` clocks from address 0, data 0 (this is what blanks the
    screen when no sync pulse arrives, because then there is no frame boundary to wait for).
    Picture writes are held off from the start of a clear until the next vsync leading edge
    that comes after the clear has finished (a vsync edge during the clear does not count):
    the frame in progress is not completed (no `frame_done`) and is never continued from
    the middle of a row. So if sync returns, or blank is pulsed, during a clear, the buffer
    holds zeros (plus possibly nothing else), and the first complete picture is the second
    frame after the clear's end at the latest: within two frames of the sweep ending it is a
    clean whole picture, never a partial one. Nothing is written for a frame that has no
    vsync edge, nor for a line that has no hsync edge.

    One rule covers every case: `we` is never high with an `addr` outside
    `0 .. width * height - 1` (one guard on the next address, whatever state the rest is
    in), including for sizes that are not powers of two.

    Settings are used as they are at the clock they matter: change them between frames or
    accept one garbled frame. The first clock after reset is never an edge. About 89
    flip-flops for 640 x 480 (Yosys).
    """

    def __init__(self, width: int, height: int) -> None:
        for name, value in (("width", width), ("height", height)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer: {value!r}")
        self.width = width
        self.height = height
        n = width * height
        self.uo_out = Signal(8)
        self.hsync_positive = Signal()
        self.vsync_positive = Signal()
        self.divider = Signal(3)
        self.h_offset = Signal(12)
        self.v_offset = Signal(11)
        self.h_step = Signal(2)
        self.v_step = Signal(2)
        self.blank = Signal()
        self.we = Signal()
        self.addr = Signal(max=n)
        self.data = Signal(PIXEL_BITS)
        self.frame_done = Signal()

        r, g, b, hsync, vsync = decode(self.uo_out)
        sample = Cat(b, g, r)  # {r, g, b}: blue in the low bits

        primed = Signal()  # low only in the first clock after reset
        hsync_q, vsync_q = Signal(), Signal()
        h_edge, v_edge = Signal(), Signal()
        self.sync += [primed.eq(1), hsync_q.eq(hsync), vsync_q.eq(vsync)]
        self.comb += [
            h_edge.eq(primed & (hsync != hsync_q) & (hsync == self.hsync_positive)),
            v_edge.eq(primed & (vsync != vsync_q) & (vsync == self.vsync_positive)),
        ]

        # Settings with zero read as one.
        divider = Signal(3)
        h_step, v_step = Signal(2), Signal(2)
        h_off = Signal(12)
        v_off = Signal(11)
        pitch = Signal(5)  # clocks between stored pixels
        first_wait = Signal(13)  # clocks from the hsync edge to the first sample, less one
        self.comb += [
            divider.eq(Mux(self.divider == 0, 1, self.divider)),
            h_step.eq(Mux(self.h_step == 0, 1, self.h_step)),
            v_step.eq(Mux(self.v_step == 0, 1, self.v_step)),
            h_off.eq(Mux(self.h_offset == 0, 1, self.h_offset)),
            v_off.eq(Mux(self.v_offset == 0, 1, self.v_offset)),
            pitch.eq(divider * h_step),
            first_wait.eq(h_off + (divider >> 1) - 1),
        ]

        vwait = Signal(12)  # hsync edges until the line to store, plus one; 0: no more rows
        row_next = Signal(max=n + 1)  # address of the first pixel of the next row to store
        col_on = Signal()  # this line is being stored
        x = Signal(max=width)
        wait = Signal(13)  # clocks until the next sample
        blank_frame = Signal()
        sweeping, swept = Signal(), Signal()
        # What the line-start logic sees: a vsync edge on this clock restarts the frame, and
        # an hsync edge on the same clock is already line 0.
        vwait_now = Signal(12)
        row_now = Signal(max=n + 1)
        last_row = Signal()
        self.comb += [
            vwait_now.eq(Mux(v_edge, v_off + 1, vwait)),
            row_now.eq(Mux(v_edge, 0, row_next)),
            last_row.eq(row_now == n - width),
        ]

        hold = Signal()  # picture writes are suppressed until the next vsync edge (after a clear)
        sweep_start, line_load = Signal(), Signal()
        addr_next = Signal(bits_for(n))  # the address of the next clock: one place decides it
        self.comb += [
            sweep_start.eq(self.blank & ~swept & ~sweeping),
            line_load.eq(h_edge & (vwait_now == 1)),
            addr_next.eq(
                Mux(
                    sweep_start,
                    0,
                    Mux(
                        sweeping,
                        self.addr + 1,
                        Mux(line_load, row_now, Mux(self.we, self.addr + 1, self.addr)),
                    ),
                )
            ),
        ]

        self.sync += [
            self.we.eq(0),
            self.frame_done.eq(self.we & (self.addr == n - 1) & ~sweeping),
            self.addr.eq(addr_next),
            If(
                col_on,
                If(
                    wait == 0,
                    If(~hold, self.we.eq(1)),
                    self.data.eq(Mux(blank_frame, 0, sample)),
                    x.eq(x + 1),
                    wait.eq(pitch - 1),
                    If(x == width - 1, col_on.eq(0)),
                ).Else(wait.eq(wait - 1)),
            ),
            If(
                v_edge,
                vwait.eq(v_off + 1),
                row_next.eq(0),
                blank_frame.eq(self.blank),
                If(~sweeping, hold.eq(0)),
            ),
            If(
                h_edge,
                x.eq(0),
                wait.eq(first_wait),
                col_on.eq(0),
                If(
                    vwait_now == 1,
                    col_on.eq(1),
                    row_next.eq(row_now + width),
                    vwait.eq(Mux(last_row, 0, v_step)),
                ).Elif(vwait_now > 1, vwait.eq(vwait_now - 1)),
            ),
            # The clear sweep wins over the picture, and holds it off until the next frame.
            If(~self.blank, swept.eq(0)),
            If(
                sweep_start,
                sweeping.eq(1),
                hold.eq(1),
                self.we.eq(1),
                self.data.eq(0),
            ),
            If(
                sweeping,
                self.we.eq(1),
                self.data.eq(0),
                If(
                    self.addr == n - 1,
                    sweeping.eq(0),
                    swept.eq(1),
                ),
            ),
            # The one rule for the write strobe: never with an address outside the buffer,
            # whatever state the rest is in.
            If(addr_next >= n, self.we.eq(0)),
        ]


# Framebuffer -------------------------------------------------------------------------------


class Framebuffer(Module):
    """A `width` x `height` framebuffer of 6-bit pixels: write in `project`, read in another clock.

    A true dual-clock memory. The write port (`we`, `addr`, `data`) is in the `project` clock
    domain and is `VgaCapture`'s write port (wire them together). The read port (`rd_addr`,
    `rd_data`) is in the domain named by `read_domain`, one pixel per read clock: the pixel at
    the address presented before a read-clock edge is on `rd_data` after it (`rd_data` is a
    register: no asynchronous read), the form the tools map to block RAM. Yosys
    `synth_xilinx` on the generated Verilog gives 15 RAMB36E1 for 320 x 240 (460,800 bits)
    and 57 for 640 x 480 (1,843,200 bits), no distributed RAM and no other memory logic than
    a few multiplexers for the block outputs. 640 x 480 does not fit an Arty A7-35T (50
    RAMB36), which is why the Arty build uses 320 x 240 (15 of 50); the A7-100T has 135.

    There is no handshake and no double buffering: a reader may see a frame while it is being
    overwritten (tearing), and for a pixel being written at the very moment it is read (the
    two ports on the same address) the read data is undefined for that one read (Xilinx
    block RAM documents invalid data for such a collision); acceptable for a display. `VgaCapture.frame_done` marks the end of a frame for
    readers that want to start a scan there.
    """

    def __init__(self, width: int, height: int, read_domain: str) -> None:
        for name, value in (("width", width), ("height", height)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer: {value!r}")
        if type(read_domain) is not str or not read_domain:
            raise ValueError(f"read_domain must be a clock domain name: {read_domain!r}")
        self.width = width
        self.height = height
        self.read_domain = read_domain
        self.mem = Memory(PIXEL_BITS, width * height)
        self.specials += self.mem
        # (The write port's own read data is unused; the simulator needs the port to have one.)
        write = self.mem.get_port(write_capable=True, clock_domain="project")
        # READ_FIRST gives `data <= mem[addr]` registered in the read clock, the form the
        # tools map to block RAM with two clocks; the default (a registered address and an
        # array read after it) ends up as distributed RAM when the two clocks differ.
        read = self.mem.get_port(mode=READ_FIRST, clock_domain=read_domain)
        self.specials += write, read
        self.we, self.addr, self.data = write.we, write.adr, write.dat_w
        self.rd_addr, self.rd_data = read.adr, read.dat_r


if __name__ == "__main__":
    sys.stdout.write(modes_header())
