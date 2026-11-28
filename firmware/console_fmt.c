// SPDX-License-Identifier: Apache-2.0

#include "console_fmt.h"

#define CHUNK 64

struct chunk {
    console_sink out;
    char buf[CHUNK];
    size_t n;
};

static void put(struct chunk *c, char ch) {
    if (c->n == CHUNK) {
        c->out(c->buf, c->n);
        c->n = 0;
    }
    c->buf[c->n++] = ch;
}

static void puts_(struct chunk *c, const char *s) {
    while (*s) put(c, *s++);
}

void console_error_msg(console_sink out, const char *message, uint32_t n) {
    static const char hex[] = "0123456789abcdef";
    struct chunk c;
    c.out = out;
    c.n = 0;
    puts_(&c, "error msg=");
    for (uint32_t i = 0; i < n; i++) {
        unsigned char b = (unsigned char)message[i];
        if (b == '\\') {
            puts_(&c, "\\\\");
        } else if (b == '\n') {
            puts_(&c, "\\n");
        } else if (b == '\r') {
            puts_(&c, "\\r");
        } else if (b < 0x20 || b == 0x7f) {
            puts_(&c, "\\x");
            put(&c, hex[b >> 4]);
            put(&c, hex[b & 0xf]);
        } else {
            put(&c, (char)b);
        }
    }
    put(&c, '\n');
    out(c.buf, c.n);
}
