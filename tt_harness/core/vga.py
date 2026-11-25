# SPDX-License-Identifier: Apache-2.0
"""Measure, capture and buffer the VGA picture a Tiny Tapeout design drives on `uo_out`.

(The module documentation is completed once all blocks are in.)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from migen import Cat

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


if __name__ == "__main__":
    sys.stdout.write(modes_header())
