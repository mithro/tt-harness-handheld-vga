// SPDX-License-Identifier: Apache-2.0
//
// Verilator harness for the demo build's SoC (tt_harness/soc/sim.py).
//
//     Vtop --sys-hz S --project-hz P --baud B --max-cycles M
//
// Drives two unrelated clocks: sys_clk at S Hz and project_clk at P Hz (P = 0: the
// project clock never toggles, as with clock_hz 0). Edges are scheduled on an exact
// picosecond grid, edge k of a clock at floor(k * 1e12 / (2 f)), so the long-run rate is
// exact. sys_rst is held high for the first 16 system cycles. The UART line serial_tx
// is decoded as 8N1 at B baud, sampled in the middle of each bit in system cycles, and
// every received byte is written to standard output at once (unbuffered), so a reader
// can stop the simulation when it has seen enough. serial_rx is held idle (1).
//
// Exit status: 2 bad arguments; 3 the cycle limit was reached; 4 framing error (a stop
// bit that is 0). The simulation otherwise runs until it is killed.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <unistd.h>

#include "Vtop.h"
#include "verilated.h"

namespace {

constexpr uint64_t kResetCycles = 16;
constexpr uint64_t kPsPerSecond = 1000000000000ULL;

[[noreturn]] void usage(const char* why) {
    std::fprintf(stderr,
                 "soc_main: %s\n"
                 "usage: Vtop --sys-hz S --project-hz P --baud B --max-cycles M\n",
                 why);
    std::exit(2);
}

uint64_t parse_u64(const char* name, const char* text) {
    char* end = nullptr;
    errno = 0;
    const unsigned long long v = std::strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || text[0] == '-') {
        std::fprintf(stderr, "soc_main: bad value for %s: %s\n", name, text);
        std::exit(2);
    }
    return v;
}

// Time of the k-th edge (rising and falling counted) of a clock of f Hz, in ps.
uint64_t edge_ps(uint64_t k, uint64_t f) {
    return static_cast<uint64_t>((static_cast<unsigned __int128>(k) * kPsPerSecond) / (2 * f));
}

struct Uart {
    double bit_cycles;
    bool busy = false;
    uint64_t start = 0;  // cycle of the falling edge of the start bit
    int bit = 0;         // next bit to sample: 0 start, 1..8 data, 9 stop
    unsigned value = 0;
    int prev = 1;

    // Called once per system cycle with the line level. Returns the byte received, -1 for
    // none, -2 for a framing error.
    int step(uint64_t cycle, int tx) {
        int out = -1;
        if (!busy) {
            if (prev == 1 && tx == 0) {
                busy = true;
                start = cycle;
                bit = 0;
                value = 0;
            }
        } else {
            const uint64_t at = start + static_cast<uint64_t>((bit + 0.5) * bit_cycles);
            if (cycle >= at) {
                if (bit == 0) {
                    if (tx != 0) busy = false;  // a glitch, not a start bit
                } else if (bit <= 8) {
                    value |= static_cast<unsigned>(tx) << (bit - 1);
                } else {
                    busy = false;
                    out = tx ? static_cast<int>(value) : -2;
                }
                bit++;
            }
        }
        prev = tx;
        return out;
    }
};

}  // namespace

int main(int argc, char** argv) {
    uint64_t sys_hz = 0, project_hz = 0, baud = 0, max_cycles = 0;
    bool have[4] = {false, false, false, false};
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (a.rfind("+verilator", 0) == 0) continue;
        if (i + 1 >= argc) usage(("missing value for " + a).c_str());
        const char* v = argv[++i];
        if (a == "--sys-hz") {
            sys_hz = parse_u64("--sys-hz", v);
            have[0] = true;
        } else if (a == "--project-hz") {
            project_hz = parse_u64("--project-hz", v);
            have[1] = true;
        } else if (a == "--baud") {
            baud = parse_u64("--baud", v);
            have[2] = true;
        } else if (a == "--max-cycles") {
            max_cycles = parse_u64("--max-cycles", v);
            have[3] = true;
        } else {
            usage(("unknown argument " + a).c_str());
        }
    }
    if (!(have[0] && have[1] && have[2] && have[3])) usage("all four options are required");
    if (sys_hz == 0 || baud == 0 || baud * 4 > sys_hz) usage("need 0 < 4 * baud <= sys-hz");

    auto context = std::make_unique<VerilatedContext>();
    context->commandArgs(argc, argv);
    auto top = std::make_unique<Vtop>(context.get());

    Uart uart{static_cast<double>(sys_hz) / static_cast<double>(baud)};
    uint64_t sys_k = 1, proj_k = 1;  // index of each clock's next edge
    uint64_t cycle = 0;
    top->sys_clk = 0;
    top->project_clk = 0;
    top->sys_rst = 1;
    top->serial_rx = 1;
    top->eval();

    while (!context->gotFinish()) {
        const uint64_t t_sys = edge_ps(sys_k, sys_hz);
        const uint64_t t_proj = project_hz ? edge_ps(proj_k, project_hz) : UINT64_MAX;
        const uint64_t now = t_sys < t_proj ? t_sys : t_proj;
        bool sys_rose = false;
        if (t_sys == now) {
            top->sys_clk = !top->sys_clk;
            sys_rose = top->sys_clk;
            sys_k++;
        }
        if (t_proj == now) {
            top->project_clk = !top->project_clk;
            proj_k++;
        }
        context->time(now);
        top->eval();
        if (!sys_rose) continue;

        cycle++;
        if (cycle == kResetCycles) top->sys_rst = 0;
        const int got = uart.step(cycle, top->serial_tx);
        if (got == -2) {
            std::fprintf(stderr, "soc_main: framing error at cycle %llu\n",
                         static_cast<unsigned long long>(cycle));
            return 4;
        }
        if (got >= 0) {
            const unsigned char byte = static_cast<unsigned char>(got);
            if (write(1, &byte, 1) != 1) {
                std::fprintf(stderr, "soc_main: cannot write: %s\n", std::strerror(errno));
                return 5;
            }
        }
        if (cycle >= max_cycles) {
            std::fprintf(stderr, "soc_main: cycle limit %llu reached\n",
                         static_cast<unsigned long long>(max_cycles));
            return 3;
        }
    }
    top->final();
    return 0;
}
