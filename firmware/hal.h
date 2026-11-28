// SPDX-License-Identifier: Apache-2.0
//
// The whole boundary between the runner's logic and the hardware. hal_litex.c implements it
// with the SoC's CSRs (docs/registers.md); tests/firmware/host_main.c with a test double
// that prints every call.

#ifndef TT_FIRMWARE_HAL_H
#define TT_FIRMWARE_HAL_H

#include <stdint.h>

struct hal {
    void (*set_ena)(int on);           // the project's ena
    void (*set_clock)(uint32_t hz);    // start the project clock at hz; 0 stops it
    void (*set_rst_n)(int level);      // 0: hold the project in reset; 1: release it
    void (*set_ui_in)(uint8_t value);  // the project's ui_in
    // Send this 24-bit word to the design's gamepad receiver (gamepad_bits of the
    // controllers): the emulator in the gateware sends its frame, level by level.
    void (*set_gamepad)(uint32_t word);
    void (*log)(const char *line);     // one console line, without its newline
};

#endif  // TT_FIRMWARE_HAL_H
