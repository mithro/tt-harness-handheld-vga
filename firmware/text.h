// SPDX-License-Identifier: Apache-2.0
//
// A bounded text builder: appends to a caller's buffer, never past its end, always
// NUL-terminated. `full` is set when anything did not fit. No allocation, no libc printf
// (the firmware has none), so the host build and the VexRiscv build print the same bytes.

#ifndef TT_FIRMWARE_TEXT_H
#define TT_FIRMWARE_TEXT_H

#include <stddef.h>
#include <stdint.h>

struct text {
    char *buf;
    size_t cap;  // bytes in buf, including the terminating NUL
    size_t len;  // bytes written, not counting the NUL
    int full;    // something did not fit
};

void text_init(struct text *t, char *buf, size_t cap);
void text_char(struct text *t, char c);
void text_mem(struct text *t, const char *s, size_t n);
void text_str(struct text *t, const char *s);
void text_u32(struct text *t, uint32_t v);
void text_i32(struct text *t, int32_t v);
// `digits` hexadecimal digits (lower case), with no prefix.
void text_hex(struct text *t, uint32_t v, int digits);

#endif  // TT_FIRMWARE_TEXT_H
