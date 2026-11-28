// SPDX-License-Identifier: Apache-2.0

#include "text.h"

void text_init(struct text *t, char *buf, size_t cap) {
    t->buf = buf;
    t->cap = cap;
    t->len = 0;
    t->full = cap == 0;
    if (cap) buf[0] = '\0';
}

void text_char(struct text *t, char c) {
    if (t->len + 1 >= t->cap) {
        t->full = 1;
        return;
    }
    t->buf[t->len++] = c;
    t->buf[t->len] = '\0';
}

void text_mem(struct text *t, const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) text_char(t, s[i]);
}

void text_str(struct text *t, const char *s) {
    while (*s) text_char(t, *s++);
}

void text_u32(struct text *t, uint32_t v) {
    char digits[10];
    int n = 0;
    do {
        digits[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n) text_char(t, digits[--n]);
}

void text_i32(struct text *t, int32_t v) {
    if (v < 0) {
        text_char(t, '-');
        text_u32(t, (uint32_t)0 - (uint32_t)v);
    } else {
        text_u32(t, (uint32_t)v);
    }
}

void text_hex(struct text *t, uint32_t v, int digits) {
    static const char hex[] = "0123456789abcdef";
    for (int i = digits - 1; i >= 0; i--) text_char(t, hex[(v >> (4 * i)) & 0xf]);
}
