// SPDX-License-Identifier: Apache-2.0

#include "vga_mode.h"

#include "vga_modes.h"

static uint32_t step_for(uint32_t size, uint32_t fb) {
    uint32_t s = fb ? (size + fb - 1) / fb : 1;
    if (s < 1) s = 1;
    return s > VGA_MAX_STEP ? VGA_MAX_STEP : s;
}

int vga_classify(const struct vga_timing *m, uint32_t fb_width, uint32_t fb_height,
                 struct vga_capture *out) {
    if (m->hsync_width < 1 || m->vsync_lines < 1) return 0;
    for (int i = 0; i < VGA_MODE_COUNT; i++) {
        const struct vga_mode *v = &vga_modes[i];
        if (m->lines_per_frame != v->lines_per_frame) continue;
        if (m->clocks_per_line != v->clocks_per_line) continue;
        // h_back is already in project clocks (times the divider)
        uint32_t h_offset = m->hsync_width + v->h_back;
        uint32_t v_offset = m->vsync_lines + v->v_back;
        if (h_offset + (uint32_t)v->width * v->divider > m->clocks_per_line) return 0;
        if (v_offset + v->height > m->lines_per_frame) return 0;
        out->name = v->name;
        out->nominal_clock_hz = v->pixel_clock_hz;
        out->width = v->width;
        out->height = v->height;
        out->divider = v->divider;
        out->h_offset = h_offset;
        out->v_offset = v_offset;
        out->h_step = step_for(v->width, fb_width);
        out->v_step = step_for(v->height, fb_height);
        return 1;
    }
    return 0;
}

void vga_refresh_text(struct text *t, uint32_t clock_hz, const struct vga_timing *m,
                      const struct vga_capture *c) {
    // the mode's nominal rate when the clock is unknown: its row's own clock over its totals
    uint64_t num = (uint64_t)(clock_hz ? clock_hz : c->nominal_clock_hz) * 10;
    uint64_t den = (uint64_t)m->clocks_per_line * m->lines_per_frame;
    // tenths, rounded half to even (the exact quotient; Python rounds the double, which
    // only differs on a tie that a double cannot hold exactly)
    uint64_t q = num / den, r = num % den;
    if (2 * r > den || (2 * r == den && (q & 1))) q++;
    text_u32(t, (uint32_t)(q / 10));
    text_char(t, '.');
    text_u32(t, (uint32_t)(q % 10));
}
