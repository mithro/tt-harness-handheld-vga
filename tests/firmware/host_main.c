// SPDX-License-Identifier: Apache-2.0
//
// The firmware's parser and runner on this machine, behind a test `hal` that prints every
// call (tests/test_firmware_host.py builds and drives it). demo.json comes on stdin.
//
//     host parse SHUTTLE                 "ok\n" and the parsed demo, one line a project and
//                                        one a step; or "errors\n" and every message, each
//                                        ended by a NUL byte (exit status 1)
//     host run SHUTTLE UNTIL_MS TICK_MS  the runner ticked at 0, TICK_MS, 2 * TICK_MS, ...
//                                        and at UNTIL_MS: every hal write as "T kind value"
//                                        (T: the runner's nominal time of the write) and
//                                        every logged line as "log LINE"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "demo_parse.h"
#include "hal.h"
#include "runner.h"

#define MAX_INPUT (1 << 20)

static char input[MAX_INPUT];
static struct demo demo;
static struct errors errs;
static struct runner runner;

static void write_event(const char *kind, unsigned long value, int hex) {
    printf(hex ? "%lu %s 0x%06lx\n" : "%lu %s %lu\n", (unsigned long)runner.event_ms, kind,
           value);
}

static void host_set_ena(int on) { write_event("ena", (unsigned long)on, 0); }
static void host_set_clock(uint32_t hz) { write_event("clock_hz", hz, 0); }
static void host_set_rst_n(int level) { write_event("rst_n", (unsigned long)level, 0); }
static void host_set_ui_in(uint8_t value) { write_event("ui_in", value, 0); }
static void host_set_gamepad(uint32_t word) { write_event("gamepad", word, 1); }
static void host_log(const char *line) { printf("log %s\n", line); }

static const struct hal host_hal = {
    .set_ena = host_set_ena,
    .set_clock = host_set_clock,
    .set_rst_n = host_set_rst_n,
    .set_ui_in = host_set_ui_in,
    .set_gamepad = host_set_gamepad,
    .log = host_log,
};

static size_t read_input(void) {
    size_t n = fread(input, 1, sizeof input, stdin);
    if (!feof(stdin)) {
        fprintf(stderr, "input longer than %d bytes\n", MAX_INPUT);
        exit(2);
    }
    return n;
}

static void print_demo(void) {
    printf("ok\nshuttle %s\nprojects %lu\n", demo.shuttle, (unsigned long)demo.project_count);
    for (uint32_t i = 0; i < demo.project_count; i++) {
        const struct project *p = &demo.projects[i];
        printf("project %lu %lu %lu %lu %u\n", (unsigned long)p->address,
               (unsigned long)p->subtile, (unsigned long)p->duration_ms,
               (unsigned long)p->clock_hz, p->ui_in);
        for (uint32_t k = 0; k < p->step_count; k++) {
            const struct step *s = &demo.steps[p->first_step + k];
            if (s->kind == STEP_SET) printf("set %lu\n", (unsigned long)s->value);
            else if (s->kind == STEP_WAIT) printf("wait_ms %lu\n", (unsigned long)s->value);
            else printf("gamepad 0x%06lx\n", (unsigned long)s->value);
        }
    }
}

static unsigned long number(const char *text) {
    char *end;
    unsigned long v = strtoul(text, &end, 10);
    if (*text == '\0' || *end != '\0') {
        fprintf(stderr, "not a number: %s\n", text);
        exit(2);
    }
    return v;
}

int main(int argc, char **argv) {
    if (argc == 3 && strcmp(argv[1], "parse") == 0) {
        size_t n = read_input();
        if (demo_parse(input, n, argv[2], &demo, &errs) == 0) {
            print_demo();
            return 0;
        }
        printf("errors\n");
        for (uint32_t i = 0; errors_get(&errs, i); i++) {
            const char *m = errors_get(&errs, i);
            fwrite(m, 1, strlen(m) + 1, stdout);
        }
        return 1;
    }
    if (argc == 5 && strcmp(argv[1], "run") == 0) {
        size_t n = read_input();
        unsigned long until = number(argv[3]), tick = number(argv[4]);
        if (tick == 0) {
            fprintf(stderr, "TICK_MS must be > 0\n");
            return 2;
        }
        if (demo_parse(input, n, argv[2], &demo, &errs) != 0) {
            fprintf(stderr, "demo.json refused: %s\n", errors_get(&errs, 0));
            return 2;
        }
        runner_init(&runner, &demo, &host_hal);
        for (unsigned long now = 0;; now += tick) {
            if (now > until) now = until;
            runner_tick(&runner, (uint32_t)now);
            if (now == until) break;
        }
        return 0;
    }
    fprintf(stderr,
            "usage: host parse SHUTTLE < demo.json\n"
            "       host run SHUTTLE UNTIL_MS TICK_MS < demo.json\n");
    return 2;
}
