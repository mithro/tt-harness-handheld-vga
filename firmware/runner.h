// SPDX-License-Identifier: Apache-2.0
//
// The demo board's runner (docs/demo-runner.md, "What the runner does for one entry"), as a
// state machine driven by the time: `runner_tick` makes every write that is due by `now_ms`,
// each at its nominal time (the runner's own waits from the selection, never `now_ms`), so
// a late tick delays writes but never moves the schedule. The writes are the ones
// tt_harness.demo.model.run lists; each is also logged as a `state` console line.

#ifndef TT_FIRMWARE_RUNNER_H
#define TT_FIRMWARE_RUNNER_H

#include <stdint.h>

#include "demo_parse.h"
#include "hal.h"

#define RUNNER_RESET_MS 2  // the 2 ms sleep of a reset pulse

enum runner_phase { PHASE_IDLE, PHASE_RUN, PHASE_WAIT, PHASE_RESET };

struct runner {
    const struct demo *demo;
    const struct hal *hal;
    int started;
    uint32_t entry;          // index of the selected project
    uint32_t event_ms;       // nominal time of the write being made
    int duration_armed;      // only with more than one project
    uint32_t duration_ms;    // when the duration timer fires
    // The step player of the selected entry.
    uint8_t phase;           // enum runner_phase
    uint16_t pos;            // next step
    uint32_t step_ms;        // nominal time of the player's next action
    int have_report;         // a gamepad step has run since the selection
    uint32_t report;         // its word
    // A reset pulse of the previous entry cut by the duration timer: its release.
    int orphan_release;
    uint32_t orphan_ms;
};

void runner_init(struct runner *r, const struct demo *demo, const struct hal *hal);
// Selects the first entry on the first call (its time is the selection time, time 0).
void runner_tick(struct runner *r, uint32_t now_ms);

#endif  // TT_FIRMWARE_RUNNER_H
