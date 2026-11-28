// SPDX-License-Identifier: Apache-2.0
//
// The runner of docs/demo-runner.md. One entry is selected at a time; selecting it writes,
// all at its selection time: ena 1, the clock, ui_in, then the first run of its steps (up
// to the first wait), and arms the duration timer when there is more than one entry. The
// step player: `set` writes ui_in (the new base), `gamepad` sends a report, `wait_ms` N > 0
// waits N ms, 0 does not wait. Past the last step the pass is over: rst_n 0, 2 ms, rst_n 1,
// the last report again if one was sent since the selection, and the steps start again,
// ui_in not rewritten. With no steps nothing is written after the selection, ever.
//
// Writes due at the same time are made in this order: the step player's, then the duration
// timer's (the model puts `next` last). A reset pulse that the duration timer cuts is still
// released at its time (the board's sleep returns after the timer callback), and nothing
// else of that pass runs.

#include "runner.h"

#include "text.h"

static int due(uint32_t at, uint32_t now) { return (int32_t)(now - at) >= 0; }

static void log_event(struct runner *r, const char *kind, uint32_t value, int hex) {
    char buf[64];
    struct text t;
    text_init(&t, buf, sizeof buf);
    text_str(&t, "state t_ms=");
    text_u32(&t, r->event_ms);
    text_char(&t, ' ');
    text_str(&t, kind);
    text_char(&t, '=');
    if (hex) {
        text_str(&t, "0x");
        text_hex(&t, value, 6);
    } else {
        text_u32(&t, value);
    }
    r->hal->log(buf);
}

static void w_ena(struct runner *r) {
    r->hal->set_ena(1);
    log_event(r, "ena", 1, 0);
}

static void w_clock(struct runner *r, uint32_t hz) {
    r->hal->set_clock(hz);
    log_event(r, "clock_hz", hz, 0);
}

static void w_ui_in(struct runner *r, uint8_t v) {
    r->hal->set_ui_in(v);
    log_event(r, "ui_in", v, 0);
}

static void w_rst_n(struct runner *r, int level) {
    r->hal->set_rst_n(level);
    log_event(r, "rst_n", (uint32_t)level, 0);
}

static void w_gamepad(struct runner *r, uint32_t word) {
    r->hal->set_gamepad(word);
    log_event(r, "gamepad", word, 1);
}

static const struct project *current(const struct runner *r) {
    return &r->demo->projects[r->entry];
}

// Run steps from r->pos at time t until a wait, or the end of the pass (the reset begins).
static void run_steps(struct runner *r, uint32_t t) {
    const struct project *p = current(r);
    const struct step *steps = &r->demo->steps[p->first_step];
    r->event_ms = t;
    for (;;) {
        if (r->pos >= p->step_count) {
            r->orphan_release = 0;  // the reset line is this entry's now
            w_rst_n(r, 0);
            r->phase = PHASE_RESET;
            r->step_ms = t + RUNNER_RESET_MS;
            return;
        }
        const struct step *s = &steps[r->pos++];
        if (s->kind == STEP_SET) {
            w_ui_in(r, (uint8_t)s->value);
        } else if (s->kind == STEP_GAMEPAD) {
            r->report = s->value;
            r->have_report = 1;
            w_gamepad(r, s->value);
        } else if (s->value > 0) {
            r->phase = PHASE_WAIT;
            r->step_ms = t + s->value;
            return;
        }
    }
}

// The step player's action that is due at r->step_ms.
static void step_action(struct runner *r) {
    uint32_t t = r->step_ms;
    r->event_ms = t;
    if (r->phase == PHASE_RESET) {
        w_rst_n(r, 1);
        if (r->have_report) w_gamepad(r, r->report);
        r->pos = 0;
    }
    r->phase = PHASE_RUN;
    run_steps(r, t);
}

static void select_entry(struct runner *r, uint32_t index, uint32_t t) {
    if (r->started && r->phase == PHASE_RESET) {
        r->orphan_release = 1;
        r->orphan_ms = r->step_ms;
    }
    r->started = 1;
    r->entry = index;
    r->event_ms = t;
    const struct project *p = current(r);
    char buf[96];
    struct text line;
    text_init(&line, buf, sizeof buf);
    text_str(&line, "state t_ms=");
    text_u32(&line, t);
    text_str(&line, " entry=");
    text_u32(&line, index);
    text_str(&line, " address=");
    text_u32(&line, p->address);
    text_str(&line, " subtile=");
    text_u32(&line, p->subtile);
    r->hal->log(buf);
    w_ena(r);
    w_clock(r, p->clock_hz);
    w_ui_in(r, p->ui_in);
    r->pos = 0;
    r->have_report = 0;
    r->phase = PHASE_IDLE;
    if (p->step_count) {
        r->phase = PHASE_RUN;
        run_steps(r, t);
    }
    r->duration_armed = r->demo->project_count > 1;
    r->duration_ms = t + p->duration_ms;
}

void runner_init(struct runner *r, const struct demo *demo, const struct hal *hal) {
    r->demo = demo;
    r->hal = hal;
    r->started = 0;
    r->entry = 0;
    r->event_ms = 0;
    r->duration_armed = 0;
    r->phase = PHASE_IDLE;
    r->pos = 0;
    r->have_report = 0;
    r->orphan_release = 0;
}

void runner_tick(struct runner *r, uint32_t now_ms) {
    if (!r->started) select_entry(r, 0, now_ms);
    for (;;) {
        int player = r->phase == PHASE_WAIT || r->phase == PHASE_RESET;
        // the earliest due action; at one time: orphan release, player, duration timer
        if (r->orphan_release && due(r->orphan_ms, now_ms) &&
            (!player || due(r->orphan_ms, r->step_ms)) &&
            (!r->duration_armed || due(r->orphan_ms, r->duration_ms))) {
            r->orphan_release = 0;
            r->event_ms = r->orphan_ms;
            w_rst_n(r, 1);
            continue;
        }
        if (player && due(r->step_ms, now_ms) &&
            (!r->duration_armed || due(r->step_ms, r->duration_ms))) {
            step_action(r);
            continue;
        }
        if (r->duration_armed && due(r->duration_ms, now_ms)) {
            select_entry(r, (r->entry + 1) % r->demo->project_count, r->duration_ms);
            continue;
        }
        return;
    }
}
