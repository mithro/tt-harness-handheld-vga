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
