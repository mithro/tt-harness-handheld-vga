# Spike A: the compare build over the whole test pool, 2026-11-24

Spike A asks whether a Tiny Tapeout VGA design can be checked on a real FPGA, clock for
clock, against a simulation. The compare build is a CPU-less bitstream that steps the
design's clock, samples its 8-bit `uo_out` after every clock and sends the bytes over the
board's UART in a checksummed frame; the host compares them byte for byte with a reference
simulation. This document is the run of all eight designs of the first test pool
(`docs/test-designs.md`) through simulation, through both toolchains' builds for the
Digilent Arty A7-35T, and on a real board.

Everything here was run on 2026-11-24 from branch `task-9-pool`. Logs and result files are
under `tmp/`, `work/` and `build/`, which are git-ignored and exist only on the machine
that ran this; each section names its files, and the figures that matter are copied into
this document.

## How it was run

| Step | Command | Results | Log |
|---|---|---|---|
| Simulation | `uv run tt-harness pool-sim --clocks 1300000 --jobs 8` | `work/pool/<shuttle>/<macro>/result.json`, `work/pool/summary.json` | `tmp/pool-sim.log` |
| Builds | `uv run python scripts/pool_run.py build` | `work/pool/<shuttle>/<macro>/build-<toolchain>.json`; build directories `build/compare/arty-<toolchain>/<shuttle>/<macro>/` with `build.log` | `tmp/pool-build.log` |
| Hardware | `uv run python scripts/pool_run.py hw --host ps1.fpgas.online --port 10722 --uart /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0 --known-hosts tmp/ps1/known_hosts` | `work/pool/<shuttle>/<macro>/hw-<toolchain>.json`, the recording `hw-<toolchain>.bin` and its measurements `hw-<toolchain>.bin.json` | `tmp/pool-hw.log`, and per run `hw-<toolchain>.log` |
| Tables | `uv run python scripts/pool_report.py` | the tables below | |

- `scripts/pool_run.py build` calls `tt_harness.build.build_compare`, the function behind
  `tt-harness build-compare`, with the timing gate, `--clocks 6000000`, 1,000,000 baud and
  16 cores, one build at a time. A build that fails is recorded with its error and is not
  retried.
- `scripts/pool_run.py hw` runs `tt-harness run-compare <id> --toolchain <t> --clocks
  6000000 ...` once per design and toolchain, one at a time, reusing the bitstreams just
  built. A design and toolchain without a successful build is not run: a failed build
  leaves no bitstream.
- Tools: Verilator 5.055 devel, Yosys 0.71+77, nextpnr-xilinx 0.8.2 (openXC7), Vivado
  2025.2. Part `xc7a35tcsg324-1`.
- Inputs: the manifest in the `tinytapeout-vga-videos` checkout (`TT_VIDEOS_REPO` default)
  and the shared reference dumps under `tt-harness-fpga-vga/work/reference/`
  (`TT_REFERENCE_DIR` default), 6,000,000 clocks each, all with `ui_in` 0 and `uio_in` 0.

The pool, with the commit each design was fetched at and the SHA-256 of its 6,000,000-byte
reference dump (`dump_sha256` of `reference.json`, first 16 digits):

| Design | Source commit | Reference clock | Reference dump sha256 |
|---|---|---|---|
| `tt07/tt_um_tinytapeout_dvd_screensaver` | `3c59c2f36659` | 25.175 MHz | `d5e84988be6ced9b` |
| `tt08/tt_um_tinytapeout_logo_screensaver` | `d4bb3d3c16c3` | 25.175 MHz | `8441410733644362` |
| `ttihp26a/tt_um_urish_rings` | `d8bf0a3e1c04` | 25.175 MHz (declares 0) | `891f3909ea810c2b` |
| `ttihp0p3/tt_um_algofoogle_fun_vga_clock` | `7caa0f8f96be` | 25.000 MHz | `3b34b5de07ed2ced` |
| `ttihp25a/tt_um_algofoogle_vga` | `c8de87dc2639` | 25.000 MHz | `d44493fdb06ea3a0` |
| `ttsky26a/tt_um_algofoogle_dottee` | `4bf41a170cc7` | 25.175 MHz | `551346282de7fc83` |
| `ttsky25a/tt_um_gamepad_pmod_demo` | `416ad7e62bab` | 25.175 MHz | `62094f679696ad8e` |
| `ttihp25a/tt_um_2048_vga_game` | `dc1a6c10d62d` | 25.175 MHz | `d2f5152014cee6b2` |

The reference clock rate plays no part in the comparison: both the reference and the
compare build count clocks, and nothing here runs a design at that rate.

## Simulation

`pool.run_sim` fetches a design, pre-screens it with Yosys, converts the compare top
(`tt_harness.compare.top.CompareTop`) to Verilog and simulates it with the design's own
sources in Verilator (`tt_harness.compare.sim`, the simulation `tests/test_compare_top.py`
uses). The bytes on the simulated UART are decoded as a frame (magic, length, CRC-32) and
the samples compared with the first 1,300,000 clocks of the shared reference, about three
640x480 frames.

**All eight are `exact`.**

| Design | Verdict | First differing clock | Latches | Comb. loops | Derived clocks | Flagged | Read as SystemVerilog | Reference | Clocks |
|---|---|---|---|---|---|---|---|---|---|
| `tt07/tt_um_tinytapeout_dvd_screensaver` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `tt08/tt_um_tinytapeout_logo_screensaver` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `ttihp26a/tt_um_urish_rings` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `ttihp0p3/tt_um_algofoogle_fun_vga_clock` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `ttihp25a/tt_um_algofoogle_vga` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `ttsky26a/tt_um_algofoogle_dottee` | **exact** | none | 0 | 0 | 1 | yes | yes | shared | 1300000 |
| `ttsky25a/tt_um_gamepad_pmod_demo` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |
| `ttihp25a/tt_um_2048_vga_game` | **exact** | none | 0 | 0 | 0 | no | no | shared | 1300000 |

- The pre-screen flags one design, `tt_um_algofoogle_dottee`: one clock made in its own
  logic (its vsync output `uo_out[3]` clocks flip-flops). Its plain Verilog parse fails
  (`p2 >>= 1;` in a `.v` file) and it is read as SystemVerilog. Had it differed, the verdict
  would have been `nondeterministic`, not `mismatch`. It did not differ.
- Each simulation took 22 to 25 s, eight at once (`tmp/pool-sim.log`).
- What this shows: the compare top's stepping, sampling and framing reproduce the reference
  in a zero-delay simulation for every pool design. It says nothing about timing or about
  either toolchain's netlist.
- `tt_um_gamepad_pmod_demo` and `tt_um_2048_vga_game` show one static picture with idle
  inputs, and the compare stimulus holds the inputs fixed, so their `exact` proves only that
  still image, here and on hardware.

## What "timing met" means

A build is accepted only if every clock and clock pair in its timing report is constrained
and met (`tt_harness.build.check`). What that covers differs between the toolchains, and in
neither case is it the design at its real clock rate.

The compare build has a 100 MHz system clock (`sys`) and a stepped project clock (`pclk`),
a register toggled by the recorder and put on a BUFG. With a settle time of S = 2 system
cycles, `pclk`'s shortest real period is 2 x (S + 1) x 10 ns = 60 ns (16.67 MHz), and that
is the period it is constrained at. On the board `pclk` steps once per UART byte, 100 kHz
at 1,000,000 baud. A sys-side change has S system cycles (20 ns) to reach the next project
clock edge, and `uo_out` has S system cycles from a project clock edge to the sample.

- **Vivado**: setup and hold, with the clock network's real insertion delays, for `sys` at
  10 ns, for `pclk` at the stepped clock's 60 ns period (not at the project's 25 MHz), and
  for the paths between them over the 20 ns the recorder really allows (multicycle paths of
  S system cycles, both ways). Clocks a project makes from its own flip-flops are generated
  clocks of `pclk` with the requirements `tt_harness/build.py` derives edge by edge. This is
  a sign-off of the compare build as a circuit; it is not a statement that the project
  would work at 25 MHz.
- **openXC7**: setup only. nextpnr-xilinx 0.8.2 does no hold analysis at all. Within a clock
  it checks setup against the constraint (`sys` 100 MHz, `pclk` and project clocks
  16.67 MHz, doubled when a clock is used on both edges, because it times falling-edge
  cells as rising). Between clocks it has no requirement of its own: it reports the largest
  data-path delay, without the clocks' insertion delays, and the harness checks that figure
  against a budget of its own, the 20 ns less one system period set aside for the
  unmeasured difference in clock arrival, so 10 ns between `sys` and `pclk`. That budget
  assumes the clocks arrive within 10 ns of each other. Clocks a project makes are on
  ordinary routing, with a skew the tool does not report.

The limitations each successful build reports, verbatim (`BuildResult.limitations`):

- every openXC7 build: "openXC7: nextpnr-xilinx performs no hold analysis"
- every openXC7 build: "openXC7: cross-domain checks are harness budgets on nextpnr's
  data-path delays, assuming clock-insertion skew under 10 ns (one system period)"
- an openXC7 build of a design that makes a clock: "openXC7: project clocks are on local
  routing with unmeasured skew"
- the per-design entries are in the table under "Builds" below.

## Known gaps

Known, and not fixed by this task:

1. **Gated or multiplexed stepped clock in Vivado.** Vivado carries `pclk` through a
   project's own combinational gate or multiplexer by itself, so flip-flops clocked that way
   are timed as plain `pclk`, with no project clock reported and no flag in the result.
2. **A design that never uses `rst_n` or its inputs as data is refused.** Such a design has
   no path from `sys` to `pclk`, and the gate requires that pair: the build fails with
   "unconstrained: clock domain(s) sys->pclk absent from the timing report".
3. **Bare "met" for paths of a clock that is not signed off.** The result line of a clock
   pair involving a project clock with `signed_off` false prints "met" without saying that
   it rests on an assumption; only the clock's own line and `limitations` say so.
4. **Inputs are held fixed.** The compare stimulus holds `ui_in` and `uio_in` at the
   reference's values (0 and 0 for the whole pool) after a 10-clock reset. Input-driven
   behaviour and the gamepad are not exercised; `tt_um_gamepad_pmod_demo` and
   `tt_um_2048_vga_game` are checked as one still picture.
5. **`uio` outputs are not recorded on hardware.** The frame carries `uo_out` only; audio on
   `uio[7]` (`tt_um_algofoogle_dottee`) is not compared.
6. **Nothing runs the project at its real clock speed.** The project clock is stepped at
   100 kHz on the board and timed at 16.67 MHz at most.
7. **One board, one run per bitstream.** See "Hardware" for the count.
