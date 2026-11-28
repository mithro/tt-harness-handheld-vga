// SPDX-License-Identifier: Apache-2.0
//
// The hal on the CSRs of tt_harness/soc/core.py (docs/registers.md). The slot's controls
// are levels: each write is in place at the pins a few project clocks later, and a release
// of rst_n carries the ena, ui_in and gamepad_enable written before it.

#include "hal_litex.h"

#include <generated/csr.h>
#include <generated/soc.h>

static void litex_set_ena(int on) { slot_ena_write(on ? 1 : 0); }

// The slot is built for one clock (the MMCM setting of demo.json's clock_hz, checked at
// boot): any non-zero rate runs it, 0 stops it.
static void litex_set_clock(uint32_t hz) { slot_clock_enable_write(hz ? 1 : 0); }

static void litex_set_rst_n(int level) { slot_rst_n_write(level ? 1 : 0); }

static void litex_set_ui_in(uint8_t value) { slot_ui_in_write(value); }

// The emulator sends `word` as a frame every 1/600 s from now on; it drives ui_in[6:4]
// from the first report (before it the lines are the base ui_in's, kept low by demo.json).
static void litex_set_gamepad(uint32_t word) {
    gamepad_buttons_write(word);
    gamepad_enable_write(1);
}

static void litex_log(const char *line) {
    size_t n = 0;
    while (line[n]) n++;
    console_line(line, n);
}

const struct hal hal_litex = {
    .set_ena = litex_set_ena,
    .set_clock = litex_set_clock,
    .set_rst_n = litex_set_rst_n,
    .set_ui_in = litex_set_ui_in,
    .set_gamepad = litex_set_gamepad,
    .log = litex_log,
};

static uint32_t last_count;
static uint64_t cycles;

void clock_init(void) {
    timer0_en_write(0);
    timer0_reload_write(0xffffffffu);
    timer0_load_write(0xffffffffu);
    timer0_en_write(1);
    timer0_update_value_write(1);
    last_count = timer0_value_read();
    cycles = 0;
}

uint32_t clock_ms(void) {
    timer0_update_value_write(1);
    uint32_t now = timer0_value_read();
    cycles += (uint32_t)(last_count - now);  // the timer counts down
    last_count = now;
    return (uint32_t)(cycles / (CONFIG_CLOCK_FREQUENCY / 1000));
}

// The console: lines go into a ring and drain to the UART from the main loop
// (console_poll), so a log line does not hold up the slot writes after it (115200 baud is
// about 11.5 bytes per ms). A full ring waits for the UART: lines are never dropped.
static char ring[CONSOLE_RING];
static uint32_t head, tail;  // free-running; head - tail bytes are queued

void console_poll(void) {
    while (tail != head && !uart_txfull_read()) {
        uart_rxtx_write((unsigned char)ring[tail % CONSOLE_RING]);
        tail++;
    }
}

static void put(char c) {
    while (head - tail == CONSOLE_RING) console_poll();
    ring[head % CONSOLE_RING] = c;
    head++;
}

void console_write(const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) put(s[i]);
    console_poll();
}

void console_line(const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) put(s[i]);
    put('\n');
    console_poll();
}

void console_flush(void) {
    while (tail != head) console_poll();
}

// A line built in a fixed buffer. Every such line has a bounded length that fits
// (firmware.py bounds the project name and the probes); if one ever did not, it says so.
void console_text(const struct text *t) {
    console_line(t->buf, t->len);
    if (t->full) {
        static const char cut[] = "error source=console msg=the line above was cut";
        console_line(cut, sizeof cut - 1);
    }
}
