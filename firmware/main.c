// SPDX-License-Identifier: Apache-2.0
//
// The demo build's firmware: boot, check the embedded demo.json, run it with the demo
// board's runner, classify the VGA mode the design produces and program the capture, and
// report every event on the UART console, one `key=value` line each (docs/registers.md,
// "Console"). It runs from the ROM, in place of LiteX's BIOS.
//
// An invalid demo.json (or one this build cannot run) is a loud failure: every message is
// printed, the project is never enabled, and sevenseg_error holds the error pattern.

#include <generated/csr.h>
#include <generated/soc.h>

#include "console_fmt.h"
#include "demo_json.h"        // generated: the build's demo.json, byte for byte
#include "demo_parse.h"
#include "firmware_config.h"  // generated: project name, shuttle, framebuffer probes
#include "hal_litex.h"
#include "runner.h"
#include "text.h"
#include "vga_mode.h"

// The error pattern the demo board shows: an "E" (segments a, d, e, f, g), written to the
// harness's error display (the sevenseg_error register) when demo.json is refused.
#define SEVENSEG_ERROR_PATTERN 0x79
#define STATUS_SETTLE_MS 50  // a slot_status change must hold this long to be reported
#define FB_PROBE_MS 20       // the period of `fb` lines, when probes are configured

void isr(void);
void isr(void) {}  // no interrupts are enabled
void litex_startup_init(void);
void litex_startup_init(void) {}

static struct demo demo;
static struct errors errs;
static struct runner runner;
static char line_buf[512];

static struct text *line(void) {
    static struct text t;
    text_init(&t, line_buf, sizeof line_buf);
    return &t;
}

// The build's own refusals of a valid demo.json (tt_harness/soc/core.py, same messages).
static void check_build(uint32_t clock_hz) {
    if (demo.project_count != 1) {
        char buf[96];
        struct text m;
        text_init(&m, buf, sizeof buf);
        text_str(&m, "this build runs exactly one project; demo.json has ");
        text_u32(&m, demo.project_count);
        errors_add(&errs, buf);
        return;
    }
    if (demo.projects[0].clock_hz != clock_hz) {
        char buf[128];
        struct text m;
        text_init(&m, buf, sizeof buf);
        text_str(&m, "demo.json sets clock_hz ");
        text_u32(&m, demo.projects[0].clock_hz);
        text_str(&m, " but the slot's clock is ");
        text_u32(&m, clock_hz);
        text_str(&m, " Hz");
        errors_add(&errs, buf);
    }
}

static void error_display(void) {
    // The project is never enabled: disabled, in reset, clock stopped, its inputs as at
    // reset. The pattern goes to the harness's own display (sevenseg_error), not the project.
    gamepad_enable_write(0);
    slot_ena_write(0);
    slot_clock_enable_write(0);
    slot_rst_n_write(0);
    sevenseg_error_write(SEVENSEG_ERROR_PATTERN);
    struct text *t = line();
    text_str(t, "error display sevenseg_error=");
    text_u32(t, sevenseg_error_read());
    text_str(t, " ena=");
    text_u32(t, slot_ena_read());
    text_str(t, " rst_n=");
    text_u32(t, slot_rst_n_read());
    text_str(t, " clock_enable=");
    text_u32(t, slot_clock_enable_read());
    text_str(t, " ui_in=");
    text_u32(t, slot_ui_in_read());
    console_text(t);
    for (;;) console_poll();
}

// ---- clock and VGA reports --------------------------------------------------------------

static uint32_t clock_hz_requested;
static uint32_t status_shown = 0xffffffffu, status_seen = 0xffffffffu, status_since;

static void report_clock(uint32_t now) {
    uint32_t s = slot_status_read() & 3;
    if (s != status_seen) {
        status_seen = s;
        status_since = now;
    }
    if (s == status_shown) return;
    // the first status at once; a later change once it has held (clock_running flickers
    // for a slow clock)
    if (status_shown != 0xffffffffu && (now - status_since) < STATUS_SETTLE_MS) return;
    status_shown = s;
    uint64_t num = info_clock_hz_num_read();
    struct text *t = line();
    text_str(t, "clock t_ms=");
    text_u32(t, now);
    text_str(t, " hz=");
    text_u32(t, clock_hz_requested);
    text_str(t, " achieved_num=");
    text_u32(t, (uint32_t)num);  // below 2**32 for every clock the slot can make
    text_str(t, " achieved_den=");
    text_u32(t, info_clock_hz_den_read());
    text_str(t, " locked=");
    text_u32(t, s & 1);
    text_str(t, " running=");
    text_u32(t, s >> 1 & 1);
    console_text(t);
}

#define VGA_HSYNC_POSITIVE 1u
#define VGA_VSYNC_POSITIVE 2u
#define VGA_LOCKED 4u
#define VGA_NO_SIGNAL 8u

struct vga_state {
    uint32_t status;  // hsync_positive, vsync_positive, locked, no_signal
    struct vga_timing timing;
};

static struct vga_state vga_shown;
static int vga_shown_valid;

// One publication of the measurement (vga_frames unchanged around the reads); 0 if none held
// still for VGA_READ_TRIES reads: a design whose "sync" toggles far faster than video
// publishes all the time, and the main loop must go on (the console, the runner).
#define VGA_READ_TRIES 4

static int vga_read(struct vga_state *v) {
    for (int k = 0; k < VGA_READ_TRIES; k++) {
        uint32_t f = vga_frames_read();
        v->timing.clocks_per_line = vga_clocks_per_line_read();
        v->timing.lines_per_frame = vga_lines_per_frame_read();
        v->timing.hsync_width = vga_hsync_width_read();
        v->timing.vsync_lines = vga_vsync_lines_read();
        v->status = vga_status_read();
        if (vga_frames_read() == f) return 1;
    }
    return 0;
}

static int vga_same(const struct vga_state *a, const struct vga_state *b) {
    if ((a->status & VGA_NO_SIGNAL) && (b->status & VGA_NO_SIGNAL)) return 1;
    return a->status == b->status &&
           a->timing.clocks_per_line == b->timing.clocks_per_line &&
           a->timing.lines_per_frame == b->timing.lines_per_frame &&
           a->timing.hsync_width == b->timing.hsync_width &&
           a->timing.vsync_lines == b->timing.vsync_lines;
}

static void report_mode(uint32_t now) {
    struct vga_state v;
    if (!vga_read(&v)) return;  // not steady: no report this time
    if (!(slot_status_read() & 2)) v.status |= VGA_NO_SIGNAL;  // stopped clock: stale timing
    if (!(v.status & (VGA_LOCKED | VGA_NO_SIGNAL))) return;  // wait for a steady timing
    if (vga_shown_valid && vga_same(&v, &vga_shown)) return;
    vga_shown = v;
    vga_shown_valid = 1;
    struct text *t = line();
    text_str(t, "mode t_ms=");
    text_u32(t, now);
    if (v.status & VGA_NO_SIGNAL) {
        text_str(t, " status=no_signal");
        console_text(t);
        return;  // the capture blanks itself while no_signal
    }
    struct vga_capture c;
    int known = vga_classify(&v.timing, info_fb_width_read(), info_fb_height_read(), &c);
    text_str(t, known ? " status=ok name=" : " status=unknown");
    if (known) {
        text_str(t, c.name);
        text_str(t, " refresh_hz=");
        vga_refresh_text(t, clock_hz_requested, &v.timing, &c);
        text_str(t, " divider=");
        text_u32(t, c.divider);
        text_str(t, " h_offset=");
        text_u32(t, c.h_offset);
        text_str(t, " v_offset=");
        text_u32(t, c.v_offset);
        text_str(t, " h_step=");
        text_u32(t, c.h_step);
        text_str(t, " v_step=");
        text_u32(t, c.v_step);
    }
    text_str(t, " clocks_per_line=");
    text_u32(t, v.timing.clocks_per_line);
    text_str(t, " lines_per_frame=");
    text_u32(t, v.timing.lines_per_frame);
    text_str(t, " hsync_width=");
    text_u32(t, v.timing.hsync_width);
    text_str(t, " vsync_lines=");
    text_u32(t, v.timing.vsync_lines);
    text_str(t, " hsync_positive=");
    text_u32(t, v.status & VGA_HSYNC_POSITIVE);
    text_str(t, " vsync_positive=");
    text_u32(t, (v.status & VGA_VSYNC_POSITIVE) >> 1);
    console_text(t);
    if (known) {
        vga_divider_write(c.divider);
        vga_h_offset_write(c.h_offset);
        vga_v_offset_write(c.v_offset);
        vga_h_step_write(c.h_step);
        vga_v_step_write(c.v_step);
        vga_blank_write(0);
    } else {
        vga_blank_write(1);  // nothing to capture: a black picture, not a garbled one
    }
}

static uint32_t probe_next;

static void report_probes(uint32_t now) {
#if FB_PROBE_COUNT == 0
    (void)now;  // a build without probes prints no fb lines
#else
    if ((int32_t)(now - probe_next) < 0) return;
    probe_next = now + FB_PROBE_MS;
    struct text *t = line();
    text_str(t, "fb t_ms=");
    text_u32(t, now);
    text_str(t, " frames=");
    text_u32(t, vga_frames_read());
    text_str(t, " pixels=");
    for (uint32_t i = 0; i < FB_PROBE_COUNT; i++) {
        if (i) text_char(t, ',');
        vga_readback_addr_write(fb_probes[i]);
        text_u32(t, fb_probes[i]);
        text_char(t, ':');
        text_hex(t, vga_readback_data_read(), 2);
    }
    console_text(t);
#endif
}

// ---- boot -------------------------------------------------------------------------------

int main(void) {
    // power-up state of the slot: disabled, in reset, clock stopped, no gamepad
    slot_ena_write(0);
    slot_rst_n_write(0);
    slot_clock_enable_write(0);
    gamepad_enable_write(0);
    vga_blank_write(1);
    clock_init();
    clock_hz_requested = info_clock_hz_read();

    struct text *t = line();
    text_str(t, "boot project=");
    text_str(t, TT_PROJECT_NAME);
    text_str(t, " sys_hz=");
    text_u32(t, CONFIG_CLOCK_FREQUENCY);
    text_str(t, " clock_hz=");
    text_u32(t, clock_hz_requested);
    text_str(t, " fb_width=");
    text_u32(t, info_fb_width_read());
    text_str(t, " fb_height=");
    text_u32(t, info_fb_height_read());
    text_str(t, " demo_json_bytes=");
    text_u32(t, DEMO_JSON_LEN);
    console_text(t);

    if (demo_parse((const char *)demo_json, DEMO_JSON_LEN, TT_SHUTTLE, &demo, &errs) == 0) {
        check_build(clock_hz_requested);
    }
    if (errs.count) {
        t = line();
        text_str(t, "demo error count=");
        text_u32(t, errs.count);
        console_text(t);
        uint32_t len;
        for (uint32_t i = 0; i < errs.count; i++) {
            const char *m = errors_get(&errs, i, &len);
            console_error_msg(console_write, m, len);  // streamed: any length, never cut
        }
        error_display();
    }
    t = line();
    text_str(t, "demo ok projects=");
    text_u32(t, demo.project_count);
    text_str(t, " steps=");
    text_u32(t, demo.step_count);
    console_text(t);

    runner_init(&runner, &demo, &hal_litex);
    uint32_t now = clock_ms();
    runner_tick(&runner, now);  // the selection: ena, clock, ui_in, the first steps
    // What enabling a project does to its reset is the SDK's, not the runner's
    // (docs/demo-runner.md, open question 1): the slot powers up in reset, and the design
    // leaves it once, here, with everything the selection wrote in place at the release.
    slot_rst_n_write(1);
    t = line();
    text_str(t, "state t_ms=");
    text_u32(t, now);
    text_str(t, " rst_n=1 source=enable");
    console_text(t);

    probe_next = now;
    for (;;) {
        now = clock_ms();
        runner_tick(&runner, now);
        report_clock(now);
        report_mode(now);
        report_probes(now);
        console_poll();
    }
}
