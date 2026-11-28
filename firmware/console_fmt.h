// SPDX-License-Identifier: Apache-2.0
//
// Console lines that can be longer than any line buffer, written in pieces to a sink (the
// UART ring on the SoC, stdout in the host tests): nothing is cut.

#ifndef TT_FIRMWARE_CONSOLE_FMT_H
#define TT_FIRMWARE_CONSOLE_FMT_H

#include <stddef.h>
#include <stdint.h>

// Takes the next n bytes of the console's output.
typedef void (*console_sink)(const char *s, size_t n);

// "error msg=", the message with `\` as `\\`, newline as `\n`, carriage return as `\r` and
// other bytes below 0x20 and 0x7f as `\xNN` (lower-case hex), then a newline.
void console_error_msg(console_sink out, const char *message, uint32_t n);

#endif  // TT_FIRMWARE_CONSOLE_FMT_H
