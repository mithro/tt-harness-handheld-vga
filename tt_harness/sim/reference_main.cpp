// SPDX-License-Identifier: Apache-2.0
//
// Verilator harness for the local reference dump (tt_harness/reference.py).
//
//     Vtop --clocks N --reset-clocks R --ui-in U --uio-in V --out uo_out.bin
//
// Stimulus: ena=1 throughout; ui_in and uio_in hold U and V from before the
// first clock; rst_n is low for the rising edges of clocks 0 to R-1 and high
// from clock R. Byte i of the output is uo_out after the rising edge of clock
// i, clock 0 being the first reset clock: exactly N bytes.

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

[[noreturn]] void usage(const char* why) {
    std::fprintf(stderr,
                 "reference: %s\n"
                 "usage: Vtop --clocks N --reset-clocks R --ui-in U --uio-in V --out FILE\n",
                 why);
    std::exit(2);
}

uint64_t parse_u64(const char* name, const char* text, uint64_t max) {
    char* end = nullptr;
    errno = 0;
    const unsigned long long v = std::strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || text[0] == '-' || v > max) {
        std::fprintf(stderr, "reference: bad value for %s: %s\n", name, text);
        std::exit(2);
    }
    return v;
}

}  // namespace

int main(int argc, char** argv) {
    uint64_t clocks = 0, reset_clocks = 0, ui_in = 0, uio_in = 0;
    std::string out;
    bool have_clocks = false, have_reset = false, have_ui = false, have_uio = false;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        if (i + 1 >= argc) usage(("missing value for " + a).c_str());
        const char* v = argv[++i];
        if (a == "--clocks") { clocks = parse_u64("--clocks", v, UINT64_MAX); have_clocks = true; }
        else if (a == "--reset-clocks") { reset_clocks = parse_u64("--reset-clocks", v, UINT64_MAX); have_reset = true; }
        else if (a == "--ui-in") { ui_in = parse_u64("--ui-in", v, 0xff); have_ui = true; }
        else if (a == "--uio-in") { uio_in = parse_u64("--uio-in", v, 0xff); have_uio = true; }
        else if (a == "--out") out = v;
        else usage(("unknown argument " + a).c_str());
    }
    if (!have_clocks || !have_reset || !have_ui || !have_uio || out.empty())
        usage("all of --clocks, --reset-clocks, --ui-in, --uio-in and --out are required");

    std::FILE* f = std::fopen(out.c_str(), "wb");
    if (!f) {
        std::fprintf(stderr, "reference: cannot open %s: %s\n", out.c_str(), std::strerror(errno));
        return 1;
    }

    // No commandArgs: Verilator's +verilator+ options must not alter the stimulus.
    auto ctx = std::make_unique<VerilatedContext>();
    auto top = std::make_unique<Vtop>(ctx.get());

    top->ena = 1;
    top->rst_n = 0;
    top->ui_in = static_cast<uint8_t>(ui_in);
    top->uio_in = static_cast<uint8_t>(uio_in);
    top->clk = 0;
    top->eval();

    constexpr size_t kChunk = 1 << 20;
    std::vector<uint8_t> buf;
    buf.reserve(kChunk);
    for (uint64_t clock = 0; clock < clocks; clock++) {
        top->rst_n = clock >= reset_clocks ? 1 : 0;
        top->clk = 0;
        top->eval();
        ctx->timeInc(1);
        top->clk = 1;
        top->eval();
        ctx->timeInc(1);
        buf.push_back(static_cast<uint8_t>(top->uo_out));
        if (buf.size() == kChunk || clock + 1 == clocks) {
            if (std::fwrite(buf.data(), 1, buf.size(), f) != buf.size()) {
                std::fprintf(stderr, "reference: write to %s failed: %s\n", out.c_str(),
                             std::strerror(errno));
                std::fclose(f);
                return 1;
            }
            buf.clear();
        }
        if (ctx->gotFinish()) {
            std::fprintf(stderr, "reference: design called $finish at clock %llu\n",
                         static_cast<unsigned long long>(clock));
            std::fclose(f);
            return 1;
        }
    }
    top->final();
    if (std::fclose(f) != 0) {
        std::fprintf(stderr, "reference: closing %s failed: %s\n", out.c_str(), std::strerror(errno));
        return 1;
    }
    return 0;
}
