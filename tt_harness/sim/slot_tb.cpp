// SPDX-License-Identifier: Apache-2.0
//
// Verilator harness for tests/test_atspeed.py: a ProjectSlot (sim_clock) and an AtSpeedCheck
// around a real Tiny Tapeout project, driven by TWO unrelated clocks.
//
//     Vtop --sys-ps P --proj-ps Q --proj-off O < script
//
// `sys` has period P ps (even) and its first rising edge at P/2; the project clock has period
// Q ps (even) and its first rising edge at O ps. Time is in picoseconds; both clocks toggle at
// their own half periods, and edges that fall on the same instant are applied together.
//
// Commands, one per line (answers on standard output):
//
//     set NAME HEX     NAME one of rst_n ena ui_in clocks gamepad_enable clock_enable
//     proj 0|1         the project clock runs (1) or is stopped low (0)
//     run PS           advance PS picoseconds
//     pulse_start      start high for exactly one sys rising edge, from now
//     wait_done PS     run until `done` (at most PS picoseconds); prints "done T crc busy"
//     show             prints "busy done crc rst_n_pin locked clock_running time"
//
// Exit status: 0 end of script; 2 bad command.

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>

#include "Vtop.h"
#include "verilated.h"

namespace {

struct Clocks {
    uint64_t sys_half, proj_half;
    uint64_t next_sys, next_proj;
    bool proj_run = true;
    uint64_t now = 0;
};

// Advance to `target` ps, applying every clock toggle on the way.
void advance(Vtop& top, Clocks& c, uint64_t target, bool stop_on_done = false) {
    for (;;) {
        const uint64_t t_proj = c.proj_run ? c.next_proj : UINT64_MAX;
        const uint64_t t = c.next_sys < t_proj ? c.next_sys : t_proj;
        if (t > target) break;
        c.now = t;
        if (c.next_sys == t) {
            top.sys_clk = !top.sys_clk;
            c.next_sys += c.sys_half;
        }
        if (c.proj_run && c.next_proj == t) {
            top.project_clk = !top.project_clk;
            c.next_proj += c.proj_half;
        }
        top.eval();
        if (stop_on_done && top.done) return;
    }
    c.now = target;
}

}  // namespace

int main(int argc, char** argv) {
    uint64_t sys_ps = 20000, proj_ps = 39722, proj_off = 3333;
    for (int i = 1; i + 1 < argc; i += 2) {
        const std::string a = argv[i];
        const uint64_t v = std::strtoull(argv[i + 1], nullptr, 0);
        if (a == "--sys-ps") sys_ps = v;
        else if (a == "--proj-ps") proj_ps = v;
        else if (a == "--proj-off") proj_off = v;
        else { std::fprintf(stderr, "slot_tb: unknown argument %s\n", a.c_str()); return 2; }
    }
    const std::unique_ptr<VerilatedContext> ctx{new VerilatedContext};
    const auto top = std::make_unique<Vtop>(ctx.get());
    Clocks c;
    c.sys_half = sys_ps / 2;
    c.proj_half = proj_ps / 2;
    c.next_sys = c.sys_half;
    c.next_proj = proj_off;
    top->sys_clk = 0;
    top->project_clk = 0;
    top->sys_rst = 1;  // a few sys clocks of reset at the start
    top->rst_n = 0;
    top->ena = 0;
    top->ui_in = 0;
    top->clocks = 0;
    top->gamepad_enable = 0;
    top->clock_enable = 1;
    top->start = 0;
    top->eval();
    advance(*top, c, 4 * sys_ps);
    top->sys_rst = 0;
    top->eval();

    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        if (!(in >> cmd)) continue;
        if (cmd == "set") {
            std::string name, hex;
            in >> name >> hex;
            const uint64_t v = std::strtoull(hex.c_str(), nullptr, 16);
            if (name == "rst_n") top->rst_n = v;
            else if (name == "ena") top->ena = v;
            else if (name == "ui_in") top->ui_in = v;
            else if (name == "clocks") top->clocks = v;
            else if (name == "gamepad_enable") top->gamepad_enable = v;
            else if (name == "clock_enable") top->clock_enable = v;
            else { std::fprintf(stderr, "slot_tb: bad name %s\n", name.c_str()); return 2; }
            top->eval();
        } else if (cmd == "proj") {
            int v = 0;
            in >> v;
            if (v && !c.proj_run) {
                // restart: the next toggle is one half period away
                c.next_proj = c.now + c.proj_half;
            }
            c.proj_run = v != 0;
            if (!v) { top->project_clk = 0; top->eval(); }
        } else if (cmd == "run") {
            uint64_t ps = 0;
            in >> ps;
            advance(*top, c, c.now + ps);
        } else if (cmd == "pulse_start") {
            top->start = 1;
            top->eval();
            // Past the next sys rising edge, then low again.
            while (true) {
                const bool before = top->sys_clk;
                advance(*top, c, c.next_sys);
                if (!before && top->sys_clk) break;
            }
            top->start = 0;
            top->eval();
        } else if (cmd == "wait_done") {
            uint64_t ps = 0;
            in >> ps;
            const uint64_t t0 = c.now;
            advance(*top, c, c.now + ps, true);
            std::printf("%d %llu %08x %d\n", static_cast<int>(top->done),
                        static_cast<unsigned long long>(c.now - t0),
                        static_cast<unsigned>(top->crc), static_cast<int>(top->busy));
        } else if (cmd == "show") {
            std::printf("%d %d %08x %d %d %d %llu\n", static_cast<int>(top->busy),
                        static_cast<int>(top->done), static_cast<unsigned>(top->crc),
                        static_cast<int>(top->pin_rst_n), static_cast<int>(top->locked),
                        static_cast<int>(top->clock_running),
                        static_cast<unsigned long long>(c.now));
        } else {
            std::fprintf(stderr, "slot_tb: bad command: %s\n", line.c_str());
            return 2;
        }
        std::fflush(stdout);
    }
    top->final();
    return 0;
}
