// SPDX-License-Identifier: Apache-2.0
//
// Verilator harness for tests/test_gamepad.py: the gamepad emulator wired to the real
// gamepad_pmod_dual receiver, both on one clock.
//
//     Vtop < script
//
// Reads commands from standard input, one per line, and answers `show` on standard output:
//
//     reset N        rst high for N clocks, then low (restarts the emulator and the receiver)
//     buttons HEX    the emulator's `buttons` input (24 bits)
//     run N          N clocks
//     frames K       clocks until the emulator's `frame` has pulsed K more times
//     show           prints "pressed0 pressed1 present latch clock data" (hex, hex, hex, 0/1 x3)
//
// Exit status: 0 end of script; 2 bad command; 3 `frames` took over 20 million clocks.

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>

#include "Vtop.h"
#include "verilated.h"

namespace {

constexpr uint64_t kFrameLimitClocks = 20'000'000;

void tick(Vtop& top) {
    top.cd_sys_clk = 0;
    top.eval();
    top.cd_sys_clk = 1;
    top.eval();
}

}  // namespace

int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> ctx{new VerilatedContext};
    ctx->commandArgs(argc, argv);
    const auto top = std::make_unique<Vtop>(ctx.get());
    top->cd_sys_clk = 0;
    top->rst = 0;
    top->buttons = 0;
    top->eval();

    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        if (!(in >> cmd)) continue;
        if (cmd == "reset") {
            uint64_t n = 0;
            in >> n;
            top->rst = 1;
            for (uint64_t i = 0; i < n; i++) tick(*top);
            top->rst = 0;
            top->eval();
        } else if (cmd == "buttons") {
            std::string hex;
            in >> hex;
            top->buttons = std::strtoul(hex.c_str(), nullptr, 16);
            top->eval();
        } else if (cmd == "run") {
            uint64_t n = 0;
            in >> n;
            for (uint64_t i = 0; i < n; i++) tick(*top);
        } else if (cmd == "frames") {
            uint64_t k = 0;
            in >> k;
            for (uint64_t clocks = 0; k > 0; clocks++) {
                if (clocks >= kFrameLimitClocks) {
                    std::fprintf(stderr, "gamepad_tb: no frame pulse within %lu clocks\n",
                                 static_cast<unsigned long>(kFrameLimitClocks));
                    return 3;
                }
                tick(*top);
                if (top->frame) k--;
            }
        } else if (cmd == "show") {
            std::printf("%x %x %x %d %d %d\n", static_cast<unsigned>(top->pressed0),
                        static_cast<unsigned>(top->pressed1), static_cast<unsigned>(top->present),
                        static_cast<int>(top->latch), static_cast<int>(top->clock),
                        static_cast<int>(top->data));
        } else {
            std::fprintf(stderr, "gamepad_tb: bad command: %s\n", line.c_str());
            return 2;
        }
    }
    std::fflush(stdout);
    top->final();
    return 0;
}
