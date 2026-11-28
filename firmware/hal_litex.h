// SPDX-License-Identifier: Apache-2.0
//
// The hal on the SoC's CSRs (docs/registers.md), the UART console and the millisecond clock.

#ifndef TT_FIRMWARE_HAL_LITEX_H
#define TT_FIRMWARE_HAL_LITEX_H

#include <stddef.h>
#include <stdint.h>

#include "hal.h"
#include "text.h"

extern const struct hal hal_litex;

// Start the millisecond clock (timer0, free running at the system clock).
void clock_init(void);
// Milliseconds since clock_init. Call at least every 2**32 system clocks (85 s at 50 MHz).
uint32_t clock_ms(void);

// The console's ring of queued bytes (a power of two).
#define CONSOLE_RING 4096

// Queue one console line: the text and a newline. It waits only if the ring is full.
void console_line(const char *s, size_t n);
void console_text(const struct text *t);
// Send queued bytes while the UART has room; call it from every loop.
void console_poll(void);
// Wait until everything queued has gone to the UART.
void console_flush(void);

#endif  // TT_FIRMWARE_HAL_LITEX_H
