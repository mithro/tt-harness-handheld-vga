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

## Builds

Sixteen builds, `--clocks 6000000`, 1,000,000 baud, for the Arty A7-35T: **13 succeeded, 3
failed**. All eight designs build with Vivado. Five build with openXC7; the three that do
not are described under "The three openXC7 failures". Nothing was changed to make a build
pass. Log: `tmp/pool-build.log`; per build, `build.log` in the build directory.

Resource use is read from the tools' own reports. For openXC7: cell counts after Yosys
synthesis (`utilization-yosys.txt`) and the LUT and flip-flop sites nextpnr-xilinx placed
(`nextpnr.log`, "Device utilisation"; it counts 65,200 LUT sites and as many flip-flop
sites for this part, and a LUT site can hold two small LUTs or none of a route-through, so
the two LUT columns differ). For Vivado: `report_utilization` after routing
(`utilization.rpt`; 20,800 LUTs, 41,600 flip-flops, 8,150 slices). Every figure includes
the recorder (about 110 flip-flops in `sys`). No design uses block RAM or a latch.

### openXC7

| Design | Build | LUTs | FFs | CARRY4 | DSP48E1 | Placed LUT / FF sites | Time | Sources read as | Project clocks | `default_nettype` rewrites | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | ok | 568 | 163 | 33 | 0 | 805 / 163 | 27 s | verilog-2005 | none | none | `d0c073b22605` |
| `tinytapeout_logo_screensaver` | ok | 579 | 173 | 35 | 0 | 828 / 173 | 29 s | verilog-2005 | none | none | `4100d72c7c86` |
| `urish_rings` | **failed** (TimingError) | 219 | 141 | 35 | 0 | 501 / 141 | 14 s |
| `algofoogle_fun_vga_clock` | **failed** (BuildError) | 382 | 183 | 65 | 2 | 866 / 183 (from `build.log`) | 16 s |
| `algofoogle_vga` | **failed** (BuildError) | 314 | 178 | 49 | 2 | 672 / 178 (from `build.log`) | 15 s |
| `algofoogle_dottee` | ok | 488 | 179 | 66 | 0 | 955 / 179 | 24 s | systemverilog | `project.uo_out[3]` | none | `655644f2364c` |
| `gamepad_pmod_demo` | ok | 278 | 131 | 33 | 0 | 547 / 131 | 20 s | verilog-2005 | none | none | `c1bb7db7bb96` |
| `2048_vga_game` | ok | 1608 | 485 | 59 | 0 | 2092 / 485 | 91 s | verilog-2005 | none | none | `0fb9945848ad` |

### Vivado

| Design | Build | LUTs | FFs | CARRY4 | DSP48E1 | Occupied slices | Time | Sources read as | Project clocks | `default_nettype` rewrites | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | ok | 405 | 163 | 21 | 0 | 135 slices | 149 s | verilog-2005 | none | `palette.v` (1), `tt_um_tinytapeout_dvd_screensaver.v` (1), `vga_sync_generator.v` (1) | `54c2dd566861` |
| `tinytapeout_logo_screensaver` | ok | 440 | 173 | 21 | 0 | 141 slices | 150 s | verilog-2005 | none | `palette.v` (1), `tt_um_tinytapeout_logo_screensaver.v` (1), `vga_sync_generator.v` (1) | `aecb1bec4bea` |
| `urish_rings` | ok | 192 | 140 | 16 | 0 | 72 slices | 139 s | verilog-2005 | none | `project.v` (1) | `ed2c38f007e1` |
| `algofoogle_fun_vga_clock` | ok | 358 | 183 | 38 | 0 | 121 slices | 146 s | verilog-2005 | none | `mv_vga_clock.v` (1), `project.v` (1), `vga_sync.v` (1) | `e03c4d0a7ead` |
| `algofoogle_vga` | ok | 310 | 178 | 32 | 0 | 104 slices | 140 s | verilog-2005 | none | `mv_vga_clock.v` (1), `project.v` (1), `vga_sync.v` (1) | `d3d5464758b1` |
| `algofoogle_dottee` | ok | 424 | 179 | 32 | 0 | 134 slices | 145 s | systemverilog | `project/hvsync_gen/vsync_reg/Q` | `audio.v` (1), `hvsync_generator.v` (1), `project.v` (1) | `1d8be5909346` |
| `gamepad_pmod_demo` | ok | 177 | 131 | 8 | 0 | 60 slices | 116 s | verilog-2005 | none | none | `260288ab7209` |
| `2048_vga_game` | ok | 204 | 213 | 8 | 0 | 79 slices | 147 s | verilog-2005 | none | `button_debounce.v` (1), `debug_controller.v` (1), `draw_game.v` (1), `galois_lfsr.v` (1), `game_logic.v` (1), `game_row_push_merge.v` (1), `project.v` (1), `vga_sync_generator.v` (1), `welcome_screen.v` (1) | `8008231a31e4` |

- The parts are as the board layer names them: `xc7a35tcsg324-1` for openXC7 and
  `xc7a35ticsg324-1L` for Vivado.
- **`default_nettype` rewrites** (Vivado only): seven of the eight designs declare ports in
  a way Vivado rejects under `` `default_nettype none `` (Synth 8-6735), so Vivado read
  copies in which that directive, and nothing else, says `wire`. The number in brackets is
  the directives changed in the file. `tt_um_gamepad_pmod_demo` needed none. openXC7 always
  reads the fetched files unmodified.
- **Project clocks**: only `tt_um_algofoogle_dottee` makes one, and both toolchains find it
  (the vsync register; Yosys names the net `project.uo_out[3]`, Vivado the pin
  `project/hvsync_gen/vsync_reg/Q`). Both class it `register` ("a flip-flop clocked on pclk
  rising edges drives it"), signed off, with no user on its falling edge (openXC7: 12
  cells, all rising). The pre-screen's one derived clock agrees.
- No domain in any build is `half_cycle`, and none is not signed off.
- Vivado's netlists are smaller, and for `tt_um_2048_vga_game` much smaller (204 LUTs and
  213 flip-flops against 1608 and 485). The compare top ties `ui_in` and `uio_in` to
  constants and leaves `uio_out` unconnected, so logic that only inputs or `uio` could
  exercise can be removed; Vivado evidently removes far more of it than Yosys. This was
  not examined further. It does mean the two bitstreams of that design are not the same
  circuit, and that what hardware checks for it is the logic that matters with idle inputs.
- The bitstream hashes are of the files this run made. A rebuild does not give the same
  file: the same openXC7 build of `tt_um_tinytapeout_dvd_screensaver` made minutes apart
  gave `6f6a7bcd8a95…` and then `d0c073b22605…`. Whether that is a date in the header or a
  different placement was not looked into.

### The three openXC7 failures

**`ttihp26a/tt_um_urish_rings`: refused by the timing gate.**

```
timing not met in pclk->sys (requirement 10.000 ns, setup slack -0.680 ns)
```

- The path is from the design's `hpos` register through its own output logic to the
  recorder's sample register. `uo_out`'s colour bits are not registered: they come from
  `hpos`, `vpos` and `frame` through two subtractions, two absolute values, a compare and
  select, and two additions (`src/project.v` lines 45 to 64). nextpnr reports 10.68 ns for
  it, 4.0 ns of logic and 6.6 ns of routing, against the harness budget of 10 ns.
- It is not a placement accident: the same netlist placed with seeds 1 to 5 gives 10.68,
  10.70, 10.99, 10.61 and 10.40 ns (`tmp/rings_seeds.py`, logs in `tmp/rings-seeds/`; an
  investigation only, no bitstream was made).
- nextpnr itself exits 0: it has no requirement on a path between clocks, and the 10 ns is
  the harness's budget applied to its report afterwards (see "What "timing met" means").
  So nextpnr does not optimise this path, and the build's retry with other seeds, which
  runs only when nextpnr reports a clock below its constraint, did not happen.
- Vivado times the same path against the 20 ns the recorder really allows, clock insertion
  included, and meets it with 8.215 ns to spare. The design is slow at its outputs, the
  openXC7 budget is conservative by one system period, and together they miss by 0.68 ns.
- Consequence: no openXC7 bitstream, so no openXC7 hardware run of this design.

**`ttihp0p3/tt_um_algofoogle_fun_vga_clock` and `ttihp25a/tt_um_algofoogle_vga`:
nextpnr-xilinx cannot route them.** Both fail the same way:

```
place and route (nextpnr-xilinx) failed (exit 255): ERROR: Unrouteable $PACKER_GND_NET sink
$flatten\project.$<repo>/work/sources/ttihp0p3/tt_um_algofoogle_fun_vga_clock/7caa0f8f96be1c97d6b1ef31bdea5e1cb2df0de1/src/project.v:165$mul$420.CARRYCASCIN
(SITEWIRE/DSP48_X1Y38/CARRYCASCIN)
```

(`tt_um_algofoogle_vga`: `src/project.v:166$mul$417.CARRYCASCIN`, `DSP48_X0Y46`.)

- Both designs share one line, `wire signed [10:0] product = psubx*psubx + psuby*psuby;`
  with 5-bit operands. Yosys `synth_xilinx` maps each multiplication to a `DSP48E1` (two
  per design). nextpnr-xilinx 0.8.2 places them and then cannot route the constant 0 to a
  DSP48E1's `CARRYCASCIN` pin. They are the only pool designs with a DSP cell.
- Vivado builds both with no DSP: it makes the multipliers from LUTs and carry chains.
- Not tried, because it would change the build: Yosys's `synth_xilinx -nodsp`.
- A second thing seen in these two logs, before the router failed: the harness's clock
  scan listed the DSP's unused, tied-off clock pin as a clock made by the project,
  `tt-harness clock: domain project net '…$mul$420.CLK$const' 16.67 MHz (cells 1 rising 1
  falling 0 latches 0 intra no; …)`. A constant is not a clock. The build ended before the
  timing gate, so what the gate would have made of it is not known; it should be looked at
  together with the DSP question.
- Consequence: no openXC7 bitstream and no openXC7 hardware run of either design.

### Limitations reported per build

"Standing entries" counts the two every openXC7 build carries (quoted under
"What "timing met" means").

| Design | Toolchain | Standing entries | Further entries, verbatim |
|---|---|---|---|
| `tinytapeout_dvd_screensaver` | openXC7 | 2 | none |
| `tinytapeout_dvd_screensaver` | Vivado | 0 | none |
| `tinytapeout_logo_screensaver` | openXC7 | 2 | none |
| `tinytapeout_logo_screensaver` | Vivado | 0 | none |
| `urish_rings` | openXC7 | - | build failed (TimingError) |
| `urish_rings` | Vivado | 0 | none |
| `algofoogle_fun_vga_clock` | openXC7 | - | build failed (BuildError) |
| `algofoogle_fun_vga_clock` | Vivado | 0 | none |
| `algofoogle_vga` | openXC7 | - | build failed (BuildError) |
| `algofoogle_vga` | Vivado | 0 | none |
| `algofoogle_dottee` | openXC7 | 2 | "openXC7: project clocks are on local routing with unmeasured skew" |
| `algofoogle_dottee` | Vivado | 0 | none |
| `gamepad_pmod_demo` | openXC7 | 2 | none |
| `gamepad_pmod_demo` | Vivado | 0 | none |
| `2048_vga_game` | openXC7 | 2 | none |
| `2048_vga_game` | Vivado | 0 | none |

## Timing per domain

Every domain and clock pair of every successful build, as `BuildResult.timing` has it.
Read it with "What "timing met" means": the same word covers different checks in the two
toolchains.

- `kind`: `sys`, `pclk`, `project` (a clock the project makes) or `cross` (a clock pair).
- `root_kind`, for a project clock: `register`, `derived` or `unclassified`.
- "Requirement" for a clock pair is the setup requirement (Vivado) or the harness budget
  the data-path delay was checked against (openXC7). For a clock it is the requirement of
  its worst path.
- "Achieved MHz" is the rate the clock could run at with its worst setup path just closing.
  For `pclk` it is a figure for the compare build's stepped clock. It is some indication
  of the design's speed in this FPGA, and no more: the path from the design's outputs is to
  the recorder, not to pins, and the inputs are constants.
- openXC7 has no hold figures ("n/a"). Vivado's `(async_default)` row is its recovery and
  removal check of the reset from `sys` into a project-clock domain, for which the build
  reads no requirement.

### `tt07/tt_um_tinytapeout_dvd_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 16.67 | 106.00 | 60.000 | 50.554 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 10.000 | 9.090 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 221.68 | 10.000 | 5.489 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 10.000 | 4.320 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 96.14 | 60.000 | 49.598 | 0.234 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 14.954 | 1.180 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 211.42 | 10.000 | 5.270 | 0.187 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.909 | 8.024 | met |

### `tt08/tt_um_tinytapeout_logo_screensaver`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 16.67 | 96.28 | 60.000 | 49.602 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 10.000 | 9.080 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 203.87 | 10.000 | 5.095 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 10.000 | 4.390 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 89.03 | 60.000 | 48.768 | 0.192 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 15.246 | 1.053 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 232.56 | 10.000 | 5.700 | 0.168 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.283 | 8.317 | met |

### `ttihp26a/tt_um_urish_rings`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | no timing result: failed |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 189.90 | 60.000 | 54.734 | 0.231 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 8.215 | 1.025 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 214.68 | 10.000 | 5.342 | 0.093 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.057 | 9.391 | met |
| Vivado | `sys->pclk (async_default)` | cross | - | yes | no | - | - | n/a | 17.401 | 8.914 | met |

### `ttihp0p3/tt_um_algofoogle_fun_vga_clock`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | no timing result: failed |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 123.26 | 60.000 | 51.887 | 0.147 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 7.792 | 1.172 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 237.76 | 10.000 | 5.794 | 0.199 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.015 | 7.665 | met |

### `ttihp25a/tt_um_algofoogle_vga`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | no timing result: failed |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 149.34 | 60.000 | 53.304 | 0.191 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 8.685 | 1.131 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 239.75 | 10.000 | 5.829 | 0.161 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 15.571 | 8.340 | met |

### `ttsky26a/tt_um_algofoogle_dottee`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 16.67 | 73.37 | 60.000 | 46.358 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 10.000 | 9.020 | n/a | met |
| openXC7 | `project:project.uo_out[3]` | project | register | yes | no | 16.67 | 315.96 | 60.000 | 56.823 | n/a | met |
| openXC7 | `project:project.uo_out[3]->pclk` | cross | - | yes | no | - | - | 50.000 | 36.680 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 216.78 | 10.000 | 5.387 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 10.000 | 5.570 | n/a | met |
| openXC7 | `sys->project:project.uo_out[3]` | cross | - | yes | no | - | - | 10.000 | 5.240 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 72.90 | 60.000 | 46.283 | 0.167 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 15.699 | 1.010 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q` | project | register | yes | no | 8.33 | 442.67 | 120.000 | 117.741 | 0.292 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->pclk` | cross | - | yes | no | - | - | 60.000 | 44.255 | 0.523 | met |
| Vivado | `project:project/hvsync_gen/vsync_reg/Q->sys` | cross | - | yes | no | - | - | 20.000 | 15.087 | 1.399 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 237.47 | 10.000 | 5.789 | 0.121 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.758 | 8.448 | met |
| Vivado | `sys->project:project/hvsync_gen/vsync_reg/Q (async_default)` | cross | - | yes | no | - | - | n/a | 18.795 | 8.016 | met |

### `ttsky25a/tt_um_gamepad_pmod_demo`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 16.67 | 91.33 | 60.000 | 49.039 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 10.000 | 9.020 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 176.34 | 10.000 | 4.329 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 10.000 | 4.920 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 181.98 | 60.000 | 54.505 | 0.167 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 15.630 | 0.950 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 238.04 | 10.000 | 5.799 | 0.132 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 17.031 | 8.757 | met |

### `ttihp25a/tt_um_2048_vga_game`

| Toolchain | Domain | kind | root_kind | signed_off | half_cycle | Constraint MHz | Achieved MHz | Requirement ns | Setup slack ns | Hold slack ns | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openXC7 | `pclk` | pclk | - | yes | no | 16.67 | 73.64 | 60.000 | 46.408 | n/a | met |
| openXC7 | `pclk->sys` | cross | - | yes | no | - | - | 10.000 | 9.050 | n/a | met |
| openXC7 | `sys` | sys | - | yes | no | 100.00 | 197.59 | 10.000 | 4.939 | n/a | met |
| openXC7 | `sys->pclk` | cross | - | yes | no | - | - | 10.000 | 3.040 | n/a | met |
| Vivado | `pclk` | pclk | - | yes | no | 16.67 | 158.40 | 60.000 | 53.687 | 0.022 | met |
| Vivado | `pclk->sys` | cross | - | yes | no | - | - | 20.000 | 15.521 | 1.096 | met |
| Vivado | `sys` | sys | - | yes | no | 100.00 | 217.44 | 10.000 | 5.401 | 0.118 | met |
| Vivado | `sys->pclk` | cross | - | yes | no | - | - | 20.000 | 16.013 | 8.316 | met |

## Hardware

Every bitstream that was built was run on a real board: thirteen runs, one per design and
toolchain, 6,000,000 project clocks each at 1,000,000 baud. **All thirteen are `exact`**:
6,000,000 samples byte for byte equal to the shared reference, frame CRC-32 good. The
three openXC7 builds that failed have no bitstream and were not run.

- Board: Digilent Arty A7-35T on the public fpgas.online PS1 site, host `pi7`
  (`ps1.fpgas.online` port 10722), Digilent USB serial `210319A764F5`; the board of
  `docs/results/2026-11-first-hardware-run.md`. The host key matched the fingerprint
  recorded in `docs/arty-boards.md`, through a known-hosts file given to the runner.
- When: 2026-11-24, 08:15 to 08:35 UTC (18:45 to 19:05 local), one run after another in one
  session (`tmp/pool-hw.log`), then one repeat at 08:35 UTC.
- The runner's activity check passed before every run, and the board was looked at by hand
  before the session and after it: one other login, a `tmux` shell with no terminal
  activity since 2026-11-23 10:46 UTC, nothing holding the UART, no loader or serial tool
  running (`tmp/ps1/check-*.log`).

| Design | Toolchain | Verdict | First differing clock | Bytes received | Frame CRC | UART error counters | Payload sha256 against reference | Load s | Load to last byte s | Whole command s | Started (local) | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | openXC7 | **exact** | none | 6000013 | good | all 0 | equal | 2.48 | 62.76 | 90.4 | 18:45:06 | `d0c073b22605` |
| `tinytapeout_dvd_screensaver` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.48 | 62.77 | 90.4 | 18:46:36 | `54c2dd566861` |
| `tinytapeout_logo_screensaver` | openXC7 | **exact** | none | 6000013 | good | all 0 | equal | 2.47 | 62.75 | 90.4 | 18:48:07 | `4100d72c7c86` |
| `tinytapeout_logo_screensaver` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.49 | 62.75 | 90.5 | 18:49:37 | `aecb1bec4bea` |
| `urish_rings` | openXC7 | not run |
| `urish_rings` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.49 | 62.74 | 89.8 | 18:51:08 | `ed2c38f007e1` |
| `algofoogle_fun_vga_clock` | openXC7 | not run |
| `algofoogle_fun_vga_clock` | Vivado | **exact** | none | 6000014 | good | all 0 | equal | 2.50 | 62.79 | 89.5 | 18:52:37 | `e03c4d0a7ead` |
| `algofoogle_vga` | openXC7 | not run |
| `algofoogle_vga` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.46 | 62.73 | 89.9 | 18:54:07 | `d3d5464758b1` |
| `algofoogle_dottee` | openXC7 | **exact** | none | 6000013 | good | all 0 | equal | 2.49 | 62.78 | 90.9 | 18:55:37 | `655644f2364c` |
| `algofoogle_dottee` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.46 | 62.72 | 90.8 | 18:57:08 | `1d8be5909346` |
| `gamepad_pmod_demo` | openXC7 | **exact** | none | 6000013 | good | all 0 | equal | 2.48 | 62.74 | 91.6 | 18:58:39 | `c1bb7db7bb96` |
| `gamepad_pmod_demo` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.48 | 62.76 | 89.5 | 19:00:10 | `260288ab7209` |
| `2048_vga_game` | openXC7 | **exact** | none | 6000013 | good | all 0 | equal | 2.50 | 62.78 | 91.5 | 19:01:40 | `0fb9945848ad` |
| `2048_vga_game` | Vivado | **exact** | none | 6000013 | good | all 0 | equal | 2.46 | 62.73 | 91.9 | 19:03:12 | `8008231a31e4` |

- "Frame CRC" is the decoder's check of the frame (magic, version, length, CRC-32). "UART
  error counters" are the kernel's for the port over the reception (`TIOCGICOUNT`: frame,
  overrun, parity, break, buffer overrun). "Payload sha256 against reference" is a check
  that does not use the verdict code: the SHA-256 of the 6,000,000 payload bytes against
  the reference's `dump_sha256` (the values in the table under "How it was run").
- "Load" is the `openFPGALoader` command timed from here. "Load to last byte" is from
  starting the load to the receiver finishing; the frame itself took 60.06 to 60.07 s from
  header to last byte in every run. "Whole command" is `tt-harness run-compare` start to finish.
- **One stray byte, once.** In the Vivado run of `tt_um_algofoogle_fun_vga_clock` the
  receiver got 6,000,014 bytes: a single `0x00` 0.6 ms before the frame header (first byte
  at 2.4842 s after the load began, header at 2.4848 s), no error counted. The frame after
  it was complete and its CRC good; the decoder looks for the header and the verdict is
  `exact`. The same bitstream run again straight afterwards gave 6,000,013 bytes with the
  header first, and `exact` again (`hw-vivado-repeat.json`). So it is not a property of the
  bitstream. It arrived at the moment the FPGA is configured, which points at the UART line
  as the device starts; that was not established. One in fourteen runs here, none in the
  four runs of the first hardware session.
- **Repeatability.** The two designs run in the first hardware session
  (`tt_um_tinytapeout_logo_screensaver`, `tt_um_algofoogle_dottee`) were rebuilt for this
  run, so these are different bitstream files from those; all four are `exact` again.
  With the repeat above, one bitstream has been run twice; every other, once.
- **`tt_um_algofoogle_dottee`** is the one flagged design (a clock from its own vsync). A
  difference would have been `nondeterministic`; there was none, with either toolchain. A
  reviewer's prediction, noted here untested: a design that captures data on the falling
  edge of a clock it derives from its own logic can legitimately differ between zero-delay
  simulation and hardware even with timing met. No pool design does that (dottee's derived
  clock has rising-edge users only), so the pool neither supports nor refutes it.
- **`tt_um_gamepad_pmod_demo` and `tt_um_2048_vga_game`** match as one still picture with
  idle inputs; nothing about their games or the gamepad is shown.
- **`tt_um_urish_rings`, `tt_um_algofoogle_fun_vga_clock`, `tt_um_algofoogle_vga`** ran with
  the Vivado bitstream only.
- State left: the last bitstream loaded (`tt_um_algofoogle_fun_vga_clock`, Vivado) stays in
  the FPGA, idle after its frame. On the board host: no `tt-harness-run-*` directory, no
  process of ours, the UART unheld, disk use as before (3.2 MB of 458 MB). No `sudo`,
  nothing installed.

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
- the per-design entries are in the table "Limitations reported per build" above.

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

## Conclusion

Spike A set out to show that a Tiny Tapeout VGA design can be checked on a real FPGA, clock
for clock, against a simulation, for a first pool of eight designs and with both an open
and a vendor toolchain.

What it showed:

- In simulation the compare build reproduces the reference for all eight designs
  (1,300,000 clocks each).
- With Vivado all eight build, meet timing under the gate, and on the Arty reproduce the
  reference exactly over 6,000,000 clocks.
- With openXC7 five of the eight build and those five are exact on the Arty as well. The
  other three do not produce a bitstream: one is refused by the harness's own cross-domain
  budget by 0.68 ns on a path Vivado passes with 8 ns to spare, and two contain multipliers
  that Yosys maps to DSP48E1 cells nextpnr-xilinx cannot route.
- No verdict other than `exact` occurred anywhere, so the `mismatch`, `nondeterministic`
  and `inconclusive` paths were exercised only by the unit tests and by the negative
  control of the first hardware session, not by a pool design.
- The UART link at 1,000,000 baud carried fourteen 6 MB frames without an error; one run
  had a stray byte before the frame, which the framing absorbed.

What remains for the sweep:

- The two openXC7 questions: designs with multipliers (try `-nodsp`, and see what the
  clock scan makes of a DSP's tied-off clock pin), and whether the 10 ns cross-domain
  budget should stay as it is for designs with slow, unregistered outputs. Until then
  openXC7 will refuse some designs that Vivado builds.
- Everything under "Known gaps": inputs and the gamepad, `uio` and audio, real clock
  speed, more than one board and more than one run per bitstream.
- The awkward cases of `docs/test-designs.md` (other clock rates and modes, the QSPI
  designs), and any design that latches, loops or clocks on a derived falling edge, none
  of which this pool contains.
