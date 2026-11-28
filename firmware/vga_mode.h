// SPDX-License-Identifier: Apache-2.0
//
// Classify the measured VGA timing against tt_harness/core/vga_modes.h and work out the
// capture settings: the C form of tt_harness.core.vga.classify (same table, same order,
// same rules), plus the decimation that fits the picture in the framebuffer.

#ifndef TT_FIRMWARE_VGA_MODE_H
#define TT_FIRMWARE_VGA_MODE_H

#include <stdint.h>

#include "text.h"

struct vga_timing {
    uint32_t clocks_per_line;
    uint32_t lines_per_frame;
    uint32_t hsync_width;
    uint32_t vsync_lines;
};

struct vga_capture {
    const char *name;  // the table's name, such as "640x480@60"
    uint32_t width, height;
    uint32_t divider;
    uint32_t h_offset;  // project clocks from the hsync leading edge to the first pixel
    uint32_t v_offset;  // lines from the vsync leading edge to the first row
    uint32_t h_step, v_step;  // store every n-th pixel and row: 1 to 3
    uint32_t nominal_clock_hz;  // the row's project clock
};

#define VGA_MAX_STEP 3

// 1 and the capture for a known mode; 0 if the timing is not one (classify gives None).
int vga_classify(const struct vga_timing *m, uint32_t fb_width, uint32_t fb_height,
                 struct vga_capture *out);

// The refresh rate as Python's f"{clock_hz / (clocks_per_line * lines_per_frame):.1f}"
// (the nominal one of the mode when clock_hz is 0).
void vga_refresh_text(struct text *t, uint32_t clock_hz, const struct vga_timing *m,
                      const struct vga_capture *c);

#endif  // TT_FIRMWARE_VGA_MODE_H
