// SPDX-License-Identifier: Apache-2.0
//
// Verilator harness for the compare build's system simulation
// (tt_harness/compare/sim.py).
//
//     Vtop --divisor D --max-cycles M --out frame.bin
//
// Drives compare_top's system clock, holds rst high for the first few cycles,
// then decodes the 8N1 UART on tx (D system cycles per bit, sampled in the
// middle of each bit) and writes every received byte to the output file. It
// stops once done has been high for two byte times, by which point the last
// stop bit has long been sampled; a byte still being received then is an
// error. Exit status: 0 done; 2 bad arguments; 3 cycle limit reached before
// done; 4 framing error; 5 output or $finish error.

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "Vtop.h"
#include "verilated.h"

namespace {

constexpr uint64_t kResetCycles = 4;

[[noreturn]] void usage(const char* why) {
    std::fprintf(stderr,
                 "uart_capture: %s\n"
                 "usage: Vtop --divisor D --max-cycles M --out FILE\n",
                 why);
    std::exit(2);
}

uint64_t parse_u64(const char* name, const char* text, uint64_t min) {
    char* end = nullptr;
    errno = 0;
    const unsigned long long v = std::strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || text[0] == '-' || v < min) {
        std::fprintf(stderr, "uart_capture: bad value for %s: %s\n", name, text);
        std::exit(2);
    }
    return v;
}

}  // namespace

int main(int argc, char** argv) {
    uint64_t divisor = 0, max_cycles = 0;
    std::string out;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (i + 1 >= argc) usage(("missing value for " + a).c_str());
        const char* v = argv[++i];
        if (a == "--divisor") divisor = parse_u64("--divisor", v, 2);
        else if (a == "--max-cycles") max_cycles = parse_u64("--max-cycles", v, 1);
        else if (a == "--out") out = v;
        else usage(("unknown argument " + a).c_str());
    }
    if (divisor == 0 || max_cycles == 0 || out.empty())
        usage("all of --divisor, --max-cycles and --out are required");

    // No commandArgs: Verilator's +verilator+ options must not alter the run.
    auto ctx = std::make_unique<VerilatedContext>();
    auto top = std::make_unique<Vtop>(ctx.get());

    std::vector<uint8_t> received;
    bool receiving = false;
    uint64_t start = 0;     // cycle of the start bit's first low sample
    unsigned value = 0;
    int prev_tx = 1;
    uint64_t done_since = 0;
    bool done_seen = false;
    const uint64_t linger = 20 * divisor;  // two byte times after done

    top->clk = 0;
    top->rst = 1;
    top->eval();
    uint64_t cycle = 0;
    for (; cycle < max_cycles; cycle++) {
        if (cycle == kResetCycles) top->rst = 0;
        top->clk = 1;
        top->eval();
        ctx->timeInc(1);
        top->clk = 0;
        top->eval();
        ctx->timeInc(1);
        if (ctx->gotFinish()) {
            std::fprintf(stderr, "uart_capture: design called $finish at cycle %llu\n",
                         static_cast<unsigned long long>(cycle));
            return 5;
        }
        if (cycle < kResetCycles) continue;

        const int tx = top->tx & 1;
        if (!receiving) {
            if (prev_tx == 1 && tx == 0) {
                receiving = true;
                start = cycle;
                value = 0;
            }
        } else {
            const uint64_t offset = cycle - start;
            if (offset >= divisor / 2 && (offset - divisor / 2) % divisor == 0) {
                const uint64_t bit = (offset - divisor / 2) / divisor;  // 0 start, 1-8 data, 9 stop
                if (bit == 0 && tx != 0) {
                    std::fprintf(stderr, "uart_capture: glitch, start bit high at cycle %llu\n",
                                 static_cast<unsigned long long>(cycle));
                    return 4;
                } else if (bit >= 1 && bit <= 8) {
                    value |= static_cast<unsigned>(tx) << (bit - 1);
                } else if (bit == 9) {
                    if (tx != 1) {
                        std::fprintf(stderr,
                                     "uart_capture: framing error, stop bit low at cycle %llu "
                                     "(byte %zu)\n",
                                     static_cast<unsigned long long>(cycle), received.size());
                        return 4;
                    }
                    received.push_back(static_cast<uint8_t>(value));
                    receiving = false;
                }
            }
        }
        prev_tx = tx;

        if (top->done && !done_seen) {
            done_seen = true;
            done_since = cycle;
        }
        if (done_seen && cycle - done_since >= linger) break;
    }
    top->final();
    if (!done_seen || cycle >= max_cycles) {
        std::fprintf(stderr,
                     "uart_capture: cycle limit %llu reached before done (%zu bytes received)\n",
                     static_cast<unsigned long long>(max_cycles), received.size());
        return 3;
    }
    if (receiving) {
        std::fprintf(stderr, "uart_capture: a byte was still arriving %llu cycles after done\n",
                     static_cast<unsigned long long>(linger));
        return 4;
    }

    std::FILE* f = std::fopen(out.c_str(), "wb");
    if (!f) {
        std::fprintf(stderr, "uart_capture: cannot open %s: %s\n", out.c_str(), std::strerror(errno));
        return 5;
    }
    if (std::fwrite(received.data(), 1, received.size(), f) != received.size() ||
        std::fclose(f) != 0) {
        std::fprintf(stderr, "uart_capture: writing %s failed: %s\n", out.c_str(),
                     std::strerror(errno));
        return 5;
    }
    std::printf("uart_capture: %zu bytes, done at cycle %llu\n", received.size(),
                static_cast<unsigned long long>(done_since));
    return 0;
}
