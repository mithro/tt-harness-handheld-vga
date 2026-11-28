// SPDX-License-Identifier: Apache-2.0
//
// demo.json, parsed and validated with the messages of tt_harness/demo/validate.py (the
// configurator's `validateDemo` with the board's Python `str()` of values), byte for byte.
// The format and the rules are docs/demo-runner.md, "The file".
//
// No allocation: every size is a compile-time limit below. A file that is valid but does
// not fit one is refused with a message starting "firmware limit: " that names the limit.

#ifndef TT_FIRMWARE_DEMO_PARSE_H
#define TT_FIRMWARE_DEMO_PARSE_H

#include <stddef.h>
#include <stdint.h>

#define DEMO_MAX_TOKENS 2048    // JSON tokens (jsmn), 16 bytes each
#define DEMO_MAX_PROJECTS 32    // entries of "projects"
#define DEMO_MAX_STEPS 512      // steps of all projects together
#define DEMO_MAX_DEPTH 32       // nesting of arrays and objects
#define DEMO_ERRORS_TEXT 4096   // bytes of all messages together
#define DEMO_SHUTTLE_MAX 64     // bytes of the shuttle name, with its NUL
#define DEMO_MAX_MS 2147483647u // duration_ms and wait_ms (the runner's clock is 32 bits)

enum step_kind { STEP_SET, STEP_WAIT, STEP_GAMEPAD };

struct step {
    uint8_t kind;    // enum step_kind
    uint32_t value;  // set: the byte; wait: ms; gamepad: the 24-bit word (gamepad_bits)
};

struct project {
    uint32_t address;
    uint32_t subtile;
    uint32_t duration_ms;
    uint32_t clock_hz;
    uint8_t ui_in;
    uint16_t first_step;  // index into demo.steps
    uint16_t step_count;
};

struct demo {
    uint32_t version;
    char shuttle[DEMO_SHUTTLE_MAX];
    uint32_t project_count;
    struct project projects[DEMO_MAX_PROJECTS];
    uint32_t step_count;
    struct step steps[DEMO_MAX_STEPS];
};

// Every message in `text`, in the order the validator gives them, each as its length (two
// bytes, low first), its bytes and a NUL (a message may hold a NUL: a value's str()).
struct errors {
    uint32_t count;
    uint32_t used;  // bytes of text in use
    int full;       // a message did not fit: the last one is the firmware-limit message
    char text[DEMO_ERRORS_TEXT];
};

// Parse and validate `len` bytes of `text` for the chip `shuttle`. Returns the number of
// messages in `errs` (0: `out` holds the demo). Messages: "not UTF-8: ..." (as
// tt_harness.soc.core.demo_entry), "not valid JSON: ..." (the detail after the colon is this
// parser's own, as validate.parse's is Python's), the validator's, or a firmware limit.
int demo_parse(const char *text, size_t len, const char *shuttle, struct demo *out,
               struct errors *errs);

// The i-th message (0-based) and its length in *len; NULL past the last.
const char *errors_get(const struct errors *errs, uint32_t i, uint32_t *len);

#endif  // TT_FIRMWARE_DEMO_PARSE_H
