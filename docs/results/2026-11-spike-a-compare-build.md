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

The sections up to "Conclusion" are the record of that first run and are left as they were
written. The build was then changed to act on what it found, and the whole pool was built
and run again: see "After Task 9b" at the end, which is the current state.

## How it was run

| Step | Command | Results | Log |
|---|---|---|---|
| Simulation | `uv run tt-harness pool-sim --clocks 1300000 --jobs 8` | `work/pool/<shuttle>/<macro>/result.json`, `work/pool/summary.json` | `tmp/pool-sim.log` |
| Builds | `uv run python scripts/pool_run.py build` | `work/pool/<shuttle>/<macro>/build-<toolchain>.json`; build directories `build/compare/arty-<toolchain>/<shuttle>/<macro>/` with `build.log` | `tmp/pool-build.log` |
| Hardware | `uv run python scripts/pool_run.py hw --host ps1.fpgas.online --port 10722 --uart /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0 --known-hosts tmp/ps1/known_hosts` | `work/pool/<shuttle>/<macro>/hw-<toolchain>.json`, the recording `hw-<toolchain>.bin` and its measurements `hw-<toolchain>.bin.json` | `tmp/pool-hw.log`, and per run `hw-<toolchain>.log` |
| Hardware, one repeat | `uv run tt-harness run-compare ttihp0p3/tt_um_algofoogle_fun_vga_clock --toolchain vivado --clocks 6000000 --host ps1.fpgas.online --port 10722 --uart /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0 --known-hosts tmp/ps1/known_hosts --out work/pool/ttihp0p3/tt_um_algofoogle_fun_vga_clock/hw-vivado-repeat.bin` | standard output saved as `hw-vivado-repeat.json` beside the recording | `tmp/hw-repeat.log` |
| Tables | `uv run python scripts/pool_report.py` | the tables below | |

- `scripts/pool_run.py build` calls `tt_harness.build.build_compare`, the function behind
  `tt-harness build-compare`, with the timing gate, `--clocks 6000000`, 1,000,000 baud and
  16 cores, one build at a time. A build that fails is recorded with its error and is not
  retried.
- `scripts/pool_run.py hw` runs `tt-harness run-compare <id> --toolchain <t> --clocks
  6000000 ...` once per design and toolchain, one at a time, reusing the bitstreams just
  built. A design and toolchain without a successful build is not run: a failed build
  leaves no bitstream.
- The build directories are the default ones, which the test suite also builds into. Two
  of them no longer hold the bitstream that was run (see the note on bitstream hashes
  under "Builds"); the bitstream of a run is identified by the SHA-256 recorded in its
  `build-<toolchain>.json` and `hw-<toolchain>.json`, not by the file now in the directory.
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
- The bitstream hashes are of the files this run made, and those are the files that were
  loaded on the board (the same hashes are in the hardware table, from each run's JSON).
  **The openXC7 files of `tt_um_tinytapeout_logo_screensaver` and `tt_um_algofoogle_dottee`
  now in `build/compare/` are not those files**: a later run of the test suite rebuilt
  both into the same default directories (a test in `tests/test_build.py` builds there),
  and they now hash `c0613f2f57c4…` and `69fefee70795…` instead of the recorded
  `4100d72c7c86…` and `655644f2364c…`. The other build directories are as the run left
  them.
- **A rebuild gives a different file with the same contents** (openXC7, checked for those
  two designs; `tmp/fix1_probes.py`, `tmp/fix1-probes.log`). Each was compared with the
  bitstream an earlier task built for the same design and settings in the main checkout:
  the generated top is byte-identical, the FASM is byte-identical, and the configuration
  data of the two `.bit` files is byte-identical. Only the `.bit` header differs: it
  carries the path of the frames file, the build date and the build time (`xc7frames2bit`
  writes them). So the file hash changes on every build and with the directory, while
  place and route with a fixed seed is reproducible. Vivado's bitstreams were not
  compared.

### The three openXC7 failures

All three come down to one thing: **the open flow's allowance on paths from the project
clock to the system clock refuses them.** No timing problem on the board was demonstrated
for any of them; Vivado builds all three and all three are `exact` on the board with the
Vivado bitstream.

**The allowance.** The recorder samples `uo_out` S = 2 system cycles after a project clock
edge, so a path from a project flip-flop to the sample register really has 20 ns. Vivado
checks exactly that, clock arrival included. nextpnr-xilinx reports only the data-path
delay of such a path: it does not model the difference in arrival between `pclk` (a
register output through a BUFG) and `sys`. The build therefore sets aside
`CROSS_DOMAIN_ALLOWANCE_CYCLES` = 1 system period, 10 ns, for that unmodelled difference,
and checks nextpnr's figure against 20 - 10 = 10 ns (`tt_harness/build.py`,
`parse_nextpnr`). Vivado measures the difference it stands for: its clock path skew on the
worst `pclk->sys` path is between 2.82 and 3.33 ns in the eight builds
(`pclk-to-sys-setup.rpt` in each Vivado build directory; `tmp/fix1-probes.log`). A whole
system period is three times what was measured.

A design whose outputs are registered has about 1 ns on this path and never notices. A
design whose `uo_out` is combinational logic after its counters does:

| Design | openXC7 `pclk->sys` data path | Against 10 ns | Vivado: data path, skew, slack against 20 ns |
|---|---|---|---|
| `tt_um_urish_rings` | 10.68 ns (build, seed 1) | -0.680 ns | 8.910 ns, 2.948 ns, 8.215 ns |
| `tt_um_algofoogle_fun_vga_clock` | 10.58 ns (with `-nodsp`, review probe) | -0.580 ns | 8.914 ns, 3.287 ns, 7.792 ns |
| `tt_um_algofoogle_vga` | 10.19 ns (with `-nodsp`, review probe) | -0.190 ns | 8.081 ns, 2.964 ns, 8.685 ns |

For comparison, the five designs that pass have 0.91 to 0.98 ns on it in openXC7.

**`ttihp26a/tt_um_urish_rings`: refused by the timing gate.**

```
timing not met in pclk->sys (requirement 10.000 ns, setup slack -0.680 ns)
```

- The path is from the design's `hpos` register through its own output logic to the
  recorder's sample register. `uo_out`'s colour bits are not registered: they come from
  `hpos`, `vpos` and `frame` through two subtractions, two absolute values, a compare and
  select, and two additions (`src/project.v` lines 45 to 64). nextpnr reports 10.68 ns for
  it, 4.0 ns of logic and 6.6 ns of routing.
- It is not a placement accident: the same netlist placed with seeds 1 to 5 gives 10.68,
  10.70, 10.99, 10.61 and 10.40 ns (`tmp/rings_seeds.py`, `tmp/rings-seeds.log`; an
  investigation only, no bitstream was made).
- nextpnr itself exits 0: it has no requirement on a path between clocks, and the 10 ns is
  applied to its report afterwards. So nextpnr does not optimise this path, and the
  build's retry with other seeds, which runs only when nextpnr reports a clock below its
  constraint, did not happen.
- Consequence: no openXC7 bitstream, so no openXC7 hardware run of this design.

**`ttihp0p3/tt_um_algofoogle_fun_vga_clock` and `ttihp25a/tt_um_algofoogle_vga`: stopped in
the router, and behind that refused by the same gate.** As built, both fail like this:

```
place and route (nextpnr-xilinx) failed (exit 255): ERROR: Unrouteable $PACKER_GND_NET sink
$flatten\project.$<repo>/work/sources/ttihp0p3/tt_um_algofoogle_fun_vga_clock/7caa0f8f96be1c97d6b1ef31bdea5e1cb2df0de1/src/project.v:165$mul$420.CARRYCASCIN
(SITEWIRE/DSP48_X1Y38/CARRYCASCIN)
```

(`tt_um_algofoogle_vga`: `src/project.v:166$mul$417.CARRYCASCIN`, `DSP48_X0Y46`. `<repo>`
stands for the worktree path in the log.)

- Both designs share one line, `wire signed [10:0] product = psubx*psubx + psuby*psuby;`
  with 5-bit operands. Yosys `synth_xilinx` maps each multiplication to a `DSP48E1` (two
  per design). nextpnr-xilinx 0.8.2 places them and then cannot route the constant 0 to a
  DSP48E1's `CARRYCASCIN` pin. They are the only pool designs with a DSP cell. Vivado
  builds both with no DSP, from LUTs and carry chains.
- **Avoiding the DSP does not make them build.** The reviewer of this task probed it: the
  builds' own Yosys scripts with `synth_xilinx -nodsp`, then the builds' own nextpnr
  command, seed 1 (`tmp/review/nodsp_probe.py`; logs `tmp/review/nodsp-fvc.log`,
  `tmp/review/nodsp-avga.log` and `tmp/review/nodsp/`; no bitstream made, no project file
  touched). Both then route, and both are refused by the gate: `pclk->sys` 10.58 ns (slack
  -0.580 ns) and 10.19 ns (slack -0.190 ns). The original logs pointed the same way
  before the router stopped: nextpnr's estimates after placement were 11.12 and 10.23 ns.
  So the DSP routing failure is real, but it is the first of two obstacles, and the second
  is the one `tt_um_urish_rings` meets.
- Consequence: no openXC7 bitstream and no openXC7 hardware run of either design.

**What could be done about the allowance** (not decided here, and nothing was changed):

- (a) A settle of 3 system cycles. The real requirement becomes 30 ns and the budget
  20 ns. It costs no run time: stepping is limited by the UART, one project clock per
  byte, not by the settle.
- (b) An allowance based on the measured skew, about 3 ns (2.8 to 3.3 measured), instead
  of a whole system period. Its cost: that figure was measured by a different toolchain on
  a different placement, so the open flow's budget would rest on a number the open flow
  cannot check for its own result.
- (c) Leave it, and accept that designs with slow unregistered outputs are refused by the
  open flow and can be checked with Vivado only.

**A defect in merged code, seen in the two DSP logs: a constant taken for a clock.** Before
the router stopped, the build's clock scan printed (`<repo>` as above):

```
tt-harness clock: domain project net '$flatten\project.$<repo>/work/sources/ttihp0p3/tt_um_algofoogle_fun_vga_clock/7caa0f8f96be1c97d6b1ef31bdea5e1cb2df0de1/src/project.v:165$mul$420.CLK$const' 16.666666666666668 MHz (cells 1 rising 1 falling 0 latches 0 intra no; clocks $flatten\project.$<repo>/work/sources/ttihp0p3/tt_um_algofoogle_fun_vga_clock/7caa0f8f96be1c97d6b1ef31bdea5e1cb2df0de1/src/project.v:165$mul$420.CLK)
```

- Cause: the scan run inside nextpnr (`_PREPACK` in `tt_harness/build.py`) constrains the
  net on every cell clock pin that is not already a known clock as a project clock, and
  does not exclude a net driven by a constant. The unregistered DSP48E1's `CLK` pin is
  tied off, so its tie-off net becomes a "project clock".
- Effect, determined by running the build's own parser (`build.parse_nextpnr`) on each of
  the two logs with a routing-complete marker and the log's own timing lines appended
  (`tmp/fix1_probes.py`, after the reviewer's `tmp/review/const.py`): it is not a hard
  failure. The result gains a domain `project:…$mul$420.CLK$const`: "n/a achieved against
  16.67 MHz, unclassified clock, NOT SIGNED OFF, no paths within the clock", met, with the
  reason "nothing sequential drives it". Had such a build passed the gate, it would report
  a project clock the pre-screen denies and limitations that are false.
- Severity: low. It errs on the conservative side (a spurious not-signed-off clock, never
  a hidden one), but it misleads. It is confirmed, not fixed here, and belongs in a
  follow-up together with the DSP question.

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
  receiver (started 08:22:41 UTC) got 6,000,014 bytes: a single `0x00` at offset 0 and the
  frame header at offset 1, no error counted. The frame after it was complete and its CRC
  good; the decoder looks for the header and the verdict is `exact`.
  - How long before the header the byte arrived is not resolved. The receiver's two
    timestamps (first byte 2.4842 s after it started, header found at 2.4848 s) are times
    of read chunks, not of bytes, and the same two timestamps are 0.35 to 1.84 ms apart
    in the thirteen runs that had no stray byte (0.57 ms in this one). All that can be
    said is that the byte came within a millisecond or two of the header, at
    configuration.
  - The same bitstream was run again at 08:35:23 UTC, 13 minutes and seven runs later:
    6,000,013 bytes, the header first, `exact` again (`hw-vivado-repeat.json`). One clean
    repeat shows only that the stray byte is not deterministic for that bitstream.
  - Where it comes from was not established. One in fourteen runs here, none in the four
    runs of the first hardware session.
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
  other three do not produce a bitstream, and for all three the reason is the open flow's
  10 ns allowance on paths from the project clock to the system clock: their unregistered
  outputs take 10.2 to 10.7 ns against a budget of 10 ns, on paths that really have 20 ns
  and that Vivado passes with about 8 ns to spare. Two of the three are stopped earlier, in
  the router, by multipliers Yosys maps to DSP48E1 cells; a probe by the reviewer shows
  that without the DSP cells they route and then miss the same budget. This is the
  allowance refusing designs, not a timing problem shown on the board.
- One defect in merged code was confirmed on the way: the open flow's clock scan takes a
  DSP's tied-off clock pin for a project clock (low severity, not fixed here).
- No verdict other than `exact` occurred anywhere, so the `mismatch`, `nondeterministic`
  and `inconclusive` paths were exercised only by the unit tests and by the negative
  control of the first hardware session, not by a pool design.
- The UART link at 1,000,000 baud carried fourteen 6 MB frames without an error; one run
  had a stray byte before the frame, which the framing absorbed.

What remains for the sweep:

- **The allowance question.** It blocks three of the eight pool designs in the open flow
  and will gate every design with slow unregistered outputs. The options are under "The
  three openXC7 failures": a settle of 3, an allowance from the measured skew, or leaving
  it and using Vivado for such designs.
- The DSP48E1 routing failure in nextpnr-xilinx (`-nodsp` gets past it, per the review
  probe), and the fix for the constant taken as a project clock.
- **The stray byte before the frame.** Seen once in fourteen runs and not explained.
  Recording, for every run, how many bytes precede the header would show how often it
  happens and whether it follows a board, a bitstream or neither.
- **Verdict paths no pool design exercised**: any verdict other than `exact` on a real
  design, a half-cycle domain, and a clock that is not signed off. The sweep will meet
  them first.
- **Bitstream reproducibility.** For openXC7 the configuration data is reproducible and
  only the header (path, date, time) changes; a run should identify its bitstream by
  something that survives a rebuild, and Vivado's bitstreams have not been compared.
- Everything under "Known gaps": inputs and the gamepad, `uio` and audio, real clock
  speed, more than one board and more than one run per bitstream.
- The awkward cases of `docs/test-designs.md` (other clock rates and modes, the QSPI
  designs), and any design that latches, loops or clocks on a derived falling edge, none
  of which this pool contains.

## After Task 9b

Written 2026-11-24, later the same day, from branch `task-9b-open-flow-pool`. The build
code was changed to act on what the run above found, and the whole pool was built and run
again. **All eight designs now build with both toolchains. Fifteen of the sixteen
bitstreams are `exact` on the board; the sixteenth, `tt_um_urish_rings` with openXC7, is a
`mismatch`, traced to a glitch on the reset the harness gives the design** (a defect of the
compare top that this run uncovered, not fixed here). The sections above are the record
of the first run; where this section and they disagree, this is the current state.

**What the fifteen `exact` verdicts do and do not show.** The defect can only affect a
design that uses `rst_n` as an asynchronous reset. Two pool designs do
(`tt_um_urish_rings`: `always @(posedge clk or negedge rst_n)`; `tt_um_algofoogle_dottee`:
`always @(posedge vsync, negedge rst_n)`; no other pool source has such a block, and no
other open-flow netlist has a flip-flop with an asynchronous clear or preset,
`tmp/9b/async-src.log`, `tmp/9b/async-scan.log`). For those two, an `exact` verdict shows
that no glitch cleared anything in that build and run, not that the build is free of the
defect: the rings Vivado build and both dottee builds are exposed to it, here and equally
in the first run at settle 2, and happened not to fire. The other six designs use `rst_n`
only synchronously and are immune: the glitch happens just after the clock index changes,
while `pclk` is low, and has settled long before the next clock edge.

### What changed

1. **The settle is 3 system cycles, not 2** (`tt_harness.compare.recorder.DEFAULT_SETTLE`,
   the one place it is set; this was the coordinator's ruling, option (a) of the list
   above, and the project owner may still reverse it). One project clock is now: `pclk`
   rises at t0, `uo_out` is sampled at t0+3, `pclk` falls at t0+4, the clock index and
   `rst_n` change at t0+5, and the next rise is at t0+8 at the earliest. So `pclk`'s
   shortest period is 80 ns (12.5 MHz), a path from the project to the sample register
   really has 30 ns, and the open flow's budget for it, with its 10 ns allowance
   unchanged, is 20 ns. Every constraint follows from the one parameter: the generated
   clock (`-divide_by 8`), the multicycle paths between `sys` and `pclk` (3, 3, 3 and 2,
   the approved scheme with new numbers), the project-clock periods and delays, and the
   open flow's frequencies and budgets. It costs no run time: a frame still takes 60.07 s,
   because stepping is limited by the UART.
2. **The open flow synthesises without DSP inference** (`synth_xilinx -nodsp`). Every
   openXC7 build now carries a third standing limitation, verbatim: "openXC7: synthesised
   without DSP inference (synth_xilinx -nodsp), so multipliers are built from LUTs and
   carry chains: nextpnr-xilinx 0.8.2 cannot route the constant cascade inputs of inferred
   DSP48E1 cells". Vivado's synthesis is unchanged. Looking closer at the two failed
   netlists: the DSP48E1 that cannot be routed is the one with no register packed into it,
   whose `CLK`, `CARRYCASCIN` and other unused inputs Yosys ties to constants; a DSP48E1
   with a packed register (clock connected, cascade inputs left open) is placed and routed.
3. **A clock pin tied to a constant is not a project clock**, in both flows. The open
   flow's scan no longer constrains such a net and prints `tt-harness tied-off clock pin:
   …` instead; run on the two saved netlists of the first run it reports the DSP's `CLK`
   pin that way and no project clock (`tmp/9b/dsp-netlists.log`). Vivado's root discovery
   was checked for the same blind spot with a design for which Vivado infers an
   unregistered DSP48E1, and had it, with a worse result: the GND cell driving the `CLK`
   pin was taken as a clock root (`project/GND/G`), constrained as a primary clock, and the
   gate then refused the build ("unconstrained: clock domain(s) project:project/GND/G
   absent from the timing report"; `tmp/9b/vivado-dsp.log`). The same rule was added
   there; that design now builds with no project clock and every `check_timing` section at
   zero (`tmp/9b/vivado-dsp-fixed.log`). No pool design has such a cell in Vivado.
4. **Tests build under pytest's temporary directory**, never in `build/compare/`, and a
   test session that creates, removes or modifies a file there fails. The sixteen pool
   bitstreams in `build/compare/` are exactly the ones recorded below. Four other
   directories are still there, `build/compare/arty-openxc7/tt00/tt_um_test_{clocks,edges,
   fallreg,negclk}`: stale builds of test designs left by a test run of the first task
   (2026-11-24 19:09 to 19:10 local, settle 2, before `-nodsp`). They are not results and
   were left alone.
5. **The receiver counts the bytes before the frame header** and keeps the first sixteen
   values; both are in each run's JSON and in the hardware table below, and `run-compare`
   prints the count.

The result files of the first run were kept aside as `work/pool-task9/` (local,
git-ignored) before this run wrote `work/pool/`.

### Builds

`uv run python scripts/pool_run.py build`, 2026-11-24 09:30 to 09:53 UTC, log
`tmp/9b/pool-build.log`: **sixteen of sixteen built, every domain met.** Columns as in
"Builds" above.

#### openXC7

| Design | Build | LUTs | FFs | CARRY4 | DSP48E1 | Placed LUT / FF sites | Time | Sources read as | Project clocks | `default_nettype` rewrites | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | ok | 556 | 164 | 34 | 0 | 806 / 164 | 23 s | verilog-2005 | none | none | `dfb57b0708ab` |
| `tinytapeout_logo_screensaver` | ok | 577 | 174 | 36 | 0 | 838 / 174 | 24 s | verilog-2005 | none | none | `2f10efe39d23` |
| `urish_rings` | ok | 227 | 142 | 35 | 0 | 511 / 142 | 15 s | verilog-2005 | none | none | `8ea25815848e` |
| `algofoogle_fun_vga_clock` | ok | 430 | 184 | 76 | 0 | 978 / 184 | 22 s | verilog-2005 | none | none | `bbcf14292d37` |
| `algofoogle_vga` | ok | 376 | 179 | 60 | 0 | 796 / 179 | 18 s | verilog-2005 | none | none | `370687faeeb2` |
| `algofoogle_dottee` | ok | 485 | 180 | 66 | 0 | 956 / 180 | 21 s | systemverilog | `project.uo_out[3]` | none | `cc3d129339a1` |
| `gamepad_pmod_demo` | ok | 308 | 132 | 34 | 0 | 588 / 132 | 18 s | verilog-2005 | none | none | `7b92c9d66bae` |
| `2048_vga_game` | ok | 1622 | 486 | 59 | 0 | 2106 / 486 | 85 s | verilog-2005 | none | none | `aa2ec0e9d3ec` |

#### Vivado

| Design | Build | LUTs | FFs | CARRY4 | DSP48E1 | Occupied slices | Time | Sources read as | Project clocks | `default_nettype` rewrites | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | ok | 409 | 164 | 21 | 0 | 131 slices | 142 s | verilog-2005 | none | `palette.v` (1), `tt_um_tinytapeout_dvd_screensaver.v` (1), `vga_sync_generator.v` (1) | `8dc7a368db60` |
| `tinytapeout_logo_screensaver` | ok | 442 | 174 | 21 | 0 | 143 slices | 144 s | verilog-2005 | none | `palette.v` (1), `tt_um_tinytapeout_logo_screensaver.v` (1), `vga_sync_generator.v` (1) | `bfc0df2cde73` |
| `urish_rings` | ok | 193 | 141 | 16 | 0 | 69 slices | 134 s | verilog-2005 | none | `project.v` (1) | `e0992d95cf12` |
| `algofoogle_fun_vga_clock` | ok | 358 | 184 | 38 | 0 | 119 slices | 138 s | verilog-2005 | none | `mv_vga_clock.v` (1), `project.v` (1), `vga_sync.v` (1) | `8395e2ffca9e` |
| `algofoogle_vga` | ok | 309 | 179 | 32 | 0 | 104 slices | 139 s | verilog-2005 | none | `mv_vga_clock.v` (1), `project.v` (1), `vga_sync.v` (1) | `09643be56a38` |
| `algofoogle_dottee` | ok | 426 | 180 | 32 | 0 | 139 slices | 184 s | systemverilog | `project/hvsync_gen/vsync_reg/Q` | `audio.v` (1), `hvsync_generator.v` (1), `project.v` (1) | `3af13ed85045` |
| `gamepad_pmod_demo` | ok | 182 | 132 | 8 | 0 | 66 slices | 118 s | verilog-2005 | none | none | `a963e158ee27` |
| `2048_vga_game` | ok | 206 | 214 | 8 | 0 | 83 slices | 164 s | verilog-2005 | none | `button_debounce.v` (1), `debug_controller.v` (1), `draw_game.v` (1), `galois_lfsr.v` (1), `game_logic.v` (1), `game_row_push_merge.v` (1), `project.v` (1), `vga_sync_generator.v` (1), `welcome_screen.v` (1) | `134ede5d6e4e` |

The path that refused three designs, before and after. "Data path" is nextpnr's figure for
the worst `pclk->sys` path; the Vivado columns are its worst path with the clock skew it
measures:

| Design | openXC7 data path | Against the 20 ns budget | Vivado slack against 30 ns | Vivado clock skew |
|---|---|---|---|---|
| `tt_um_urish_rings` | 10.88 ns | 9.120 ns to spare | 17.852 ns | 3.1 ns |
| `tt_um_algofoogle_fun_vga_clock` | 10.94 ns | 9.060 ns to spare | 17.898 ns | 3.0 ns |
| `tt_um_algofoogle_vga` | 10.26 ns | 9.740 ns to spare | 17.796 ns | 3.0 ns |

- The five designs with registered outputs have 0.93 to 0.99 ns on that path, as before.
  The smallest margin of any clock pair in the open flow is now 9.06 ns
  (`tt_um_algofoogle_fun_vga_clock`, `pclk->sys`); among the other five it is `sys->pclk`
  of `tt_um_2048_vga_game`, 11.69 ns of 20.
- Vivado's smallest setup margin is `sys` itself (about 5 ns of 10, the recorder's own
  logic); its smallest hold margin is 0.032 ns (`tt_um_2048_vga_game`, within `sys`).
  All met.
- `tt_um_algofoogle_dottee`'s project clock is found and classed as before by both
  (`register`, signed off; no falling-edge user). No domain is `half_cycle` and none is
  not signed off. Rewrites are as in the first run. No DSP48E1 in any build.
- With `-nodsp` the two multiplier designs take 430 and 376 LUTs in the open flow (382 and
  314 with the two DSP48E1 each before).
- Per-domain figures for all sixteen builds are in
  [`2026-11-spike-a-compare-build-9b-timing.md`](2026-11-spike-a-compare-build-9b-timing.md).

What Vivado reports for the paths between `sys` and `pclk` at settle 3, from the pair
reports of these builds, and how each relates to what the recorder does:

| Path | Check | Requirement, as Vivado prints it | Against the recorder |
|---|---|---|---|
| `sys->pclk` | setup | 30 ns (`pclk rise@80 - sys rise@50`) | exact: the clock index and `rst_n` change at t0+5 and the next `pclk` rise is at t0+8 |
| `sys->pclk` | hold | -10 ns (`pclk rise@0 - sys rise@10`) | exact for a flip-flop on `pclk`'s falling edge (t0+4, one cycle before the change at t0+5); conservative for one on the rising edge, where the nearest change is five cycles later |
| `pclk->sys` | setup | 30 ns (`sys rise@30 - pclk rise@0`) | exact: the sample is at t0+3 |
| `pclk->sys` | hold | 0 ns (`sys rise@0 - pclk rise@0`) | conservative: the sample register only takes `uo_out` at t0+3, and the sample before that was at least five cycles earlier |

For a project clock made by a flip-flop (dottee): 160 ns period with both edges on `pclk`
rising edges; to `pclk` 80 ns setup and 0 ns hold; to and from `sys` 30 ns, with -10 ns
hold from `sys`.

### Hardware

`uv run python scripts/pool_run.py hw …` as before, on the same board (pi7, Arty
`210319A764F5`), 2026-11-24 09:54 to 10:19 UTC, one session, log `tmp/9b/pool-hw.log`:
sixteen runs. **Fifteen are `exact`. One is a `mismatch`: `tt_um_urish_rings` built with
openXC7**, from clock 18 on, with a good frame CRC and no UART error. It is a real
difference, it repeats, and its cause is in the harness, not in the design or the link:
see the next section.

| Design | Toolchain | Verdict | First differing clock | Bytes received | Bytes before the header | Frame CRC | UART error counters | Payload sha256 against reference | Load s | Load to last byte s | Whole command s | Started (local) | Bitstream sha256 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `tinytapeout_dvd_screensaver` | openXC7 | **exact** | none | 6000149 | 136 (`00000000000000000000000000000000`) | good | break 2, overrun 1, others 0 | equal | 2.49 | 62.77 | 91.8 | 20:23:58 | `dfb57b0708ab` |
| `tinytapeout_dvd_screensaver` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.48 | 62.76 | 90.5 | 20:25:29 | `8dc7a368db60` |
| `tinytapeout_logo_screensaver` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.50 | 62.75 | 90.6 | 20:27:00 | `2f10efe39d23` |
| `tinytapeout_logo_screensaver` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.47 | 62.72 | 91.0 | 20:28:31 | `bfc0df2cde73` |
| `urish_rings` | openXC7 | **mismatch** | 18 | 6000013 | 0 | good | all 0 | differs | 2.50 | 65.24 | 92.0 | 20:30:02 | `8ea25815848e` |
| `urish_rings` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.47 | 62.75 | 114.7 | 20:31:34 | `e0992d95cf12` |
| `algofoogle_fun_vga_clock` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.49 | 62.77 | 89.5 | 20:33:28 | `bbcf14292d37` |
| `algofoogle_fun_vga_clock` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.48 | 62.76 | 89.7 | 20:34:58 | `8395e2ffca9e` |
| `algofoogle_vga` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.47 | 62.73 | 91.4 | 20:36:28 | `370687faeeb2` |
| `algofoogle_vga` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 3.90 | 64.18 | 90.9 | 20:37:59 | `09643be56a38` |
| `algofoogle_dottee` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.47 | 62.75 | 91.5 | 20:39:30 | `cc3d129339a1` |
| `algofoogle_dottee` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.47 | 62.75 | 91.3 | 20:41:02 | `3af13ed85045` |
| `gamepad_pmod_demo` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.46 | 62.74 | 89.7 | 20:42:33 | `7b92c9d66bae` |
| `gamepad_pmod_demo` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.48 | 62.77 | 90.0 | 20:44:03 | `a963e158ee27` |
| `2048_vga_game` | openXC7 | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.49 | 62.73 | 93.7 | 20:45:33 | `aa2ec0e9d3ec` |
| `2048_vga_game` | Vivado | **exact** | none | 6000013 | 0 | good | all 0 | equal | 2.50 | 62.78 | 94.8 | 20:47:07 | `134ede5d6e4e` |

- The board was checked by hand before and after the session (`tmp/9b/check-*.log`): the
  same idle `tmux` login as before, nothing holding the UART. Another agent of this project
  used the board for short runs earlier in the day; the runner's activity check never
  refused a run during this session. Afterwards: no `tt-harness-run-*` directory, no process of ours, disk use as
  before; the last bitstream loaded is a scratch build of `tt_um_urish_rings` (see below),
  idle after its frame.
- **Bytes before the header.** Fifteen runs: none. The first run of the session
  (`tt_um_tinytapeout_dvd_screensaver`, openXC7): 136 bytes, every one `0x00`, with two
  breaks and one overrun counted by the kernel. They arrived before the load: the first
  came 0.094 s after the receiver started, the load began after that, and the frame
  header came at 2.478 s; two more bytes were already waiting when the receiver opened
  the port. So they did not come from this bitstream. The likely source is whatever was
  in the FPGA before the session (another agent of this project had used the board
  since the first run), driving the UART line; that is plausible and not proven, since
  nothing recorded what was loaded. The receiver already discards what is buffered
  before it reports that it is listening, but it cannot discard what a design that is
  still sending delivers after that and before the load. The receiver looks for the
  header, the frame after it was whole, and the verdict is `exact`; the count now shows
  such a start. The two extra runs below also had none, so no run of this session shows
  the single stray byte of the first run.
- One run took 115 s instead of 90 (`tt_um_urish_rings`, Vivado: the recording step itself
  took 85.6 s, the rest was before it) and one load took 3.9 s instead of 2.5; neither
  was looked into.
- All sixteen bitstreams are the files now in `build/compare/` (hashes above); the test
  suite no longer builds into that tree.

### The one mismatch: a glitch on the harness's `rst_n`

`tt_um_urish_rings`, openXC7, bitstream `8ea25815848e…`: 549,561 of 6,000,000 bytes differ
(9.2 %), first at clock 18 (`uo_out` 0xad, reference 0xbd). Vivado's bitstream of the same
design is `exact`, and so was the simulation of the compare top.

- **What the bytes show.** Only colour bits differ, and every colour edge is displaced
  as one count of `frame` displaces it. A model of the design (`tmp/9b_rings_model.py`; it reproduces the
  reference with no byte wrong) finds the value of the design's `frame` counter that each
  part of a recording shows: the reference and the Vivado recording show 1 through the
  first video frame, then 2, 3, …, 14; the openXC7 recording shows 0, then 1, 2, …, 13
  (`tmp/9b/rings-model-1.log`). The counter is exactly one behind for the whole run: one
  count was lost in the first clocks after reset and nothing went wrong afterwards.
- **It repeats.** The same bitstream run again gives a byte-identical recording
  (`hw-openxc7-repeat.json`, `tmp/9b/rings-diff-2.log`).
- **The design.** `frame` is the only state in this design with an asynchronous reset
  (`always @(posedge clk or negedge rst_n)`); it counts when `hpos == 0 && vpos == 0`,
  which is the case at clock 10, the first clock after reset. In the reference it becomes
  1 there.
- **The harness.** The compare top makes `rst_n` combinationally: `rst_n = (clock_index >=
  10)`, on the recorder's 32-bit clock counter (`tt_harness/core/stimulus.py`). In the
  open-flow netlist that comparison is a carry chain, and its inverted output drives the
  `CLR` pins of the eight `frame` flip-flops directly (`tmp/9b/rings-rst-cone.log`). The
  output of such a chain can dip for a moment when the counter changes, because the
  select and the data input of a stage come through different LUTs. A zero-delay
  simulation has no such pulse.
- **What is established.** A glitch on that comparator pulsed the asynchronous clear at
  some moment after the clock-10 edge, where `frame` becomes 1, and before the clock-18
  sample, the first byte that depends on `frame` (bytes 10 to 17 are the same for
  `frame` 0 and 1, `tmp/9b/window.log`). It happened once: the counter is never more
  than one behind, so no later change of the index cleared it again.
- **Which counter change it was is not determined.** The recording cannot tell, and
  nothing was built to find out. From the netlist the candidates in that window are:
  11 to 12, where the second stage's select (one LUT6) falls while its data input (a LUT2
  and then a LUT5) rises; 10 to 11, where the first stage's select and data input, two
  LUT2s, swap in the same way; and 15 to 16, where two inputs of the second stage's data
  LUT change in opposite directions. The first of these has the larger difference in
  logic depth and is at least as likely as the second. The other changes in the window
  (12 to 15, 16 to 18) leave the output held by a steady data input.
- **What is excluded.** A late release of reset (the 9 to 10 change): reset is released
  one UART byte time, about 10 us, before the clock-10 edge, and the scratch build below
  releases it a cycle later still and is `exact`. A hold failure on the first count: the
  count at clock 10 and the thirteen later ones go through the same paths under the same
  condition (`hpos` leaves 0 right after the edge), and the later ones all arrived. A
  link fault: the frame CRC is good and the repeat is byte-identical.
- **Confirmed by experiment, as a diagnosis.** A scratch build, not project code,
  identical except that `rst_n` comes from a system-clock register and so cannot glitch,
  was built with openXC7 and run once on the board: `exact`, 6,000,013 bytes, none
  before the header (`tmp/9b_regrst.py`, `tmp/9b/regrst-build.log`,
  `tmp/9b/regrst-run.log`). That build is not the form the repair will take: its register
  follows the comparator, so `rst_n` changes one system cycle later than the clock index
  (t0+6 at settle 3) and really has 20 ns to the next clock edge while the constraints
  assume 30. It served to show the cause and nothing else.
- **What it means.** It is a defect of the compare top, present before this task and in
  both flows: any design that uses `rst_n` as an asynchronous reset can have state cleared
  by a glitch whenever the clock index changes, depending on how the comparator happens
  to be mapped and routed. Neither toolchain's timing analysis covers a glitch on a reset
  made by logic. Of the pool, this design and `tt_um_algofoogle_dottee` use an
  asynchronous reset. Dottee's asynchronously reset counter is clocked by its vsync and
  first counts about one video frame into the run; a glitch before that clears a counter
  that is still at its reset value and cannot be seen, and a glitch at any later change
  of the index (the comparator's inputs keep changing for the whole run) would put it
  behind and would be seen. Its four `exact` runs, two at each settle, show that none
  cleared it in those builds, as the rings Vivado runs do for that build; they do not
  show that it cannot happen. On a real Tiny Tapeout chip `rst_n` is a pin and does not
  glitch.
- **Not fixed here.** The verdict stands as recorded. The repair is in the gateware: drive
  the project's `rst_n` from a register that is updated on the same system clock edge as
  the clock index (t0+5 at settle 3), so that the constraints between `sys` and `pclk`
  stay as they are. It needs the recorder or the stimulus changed, their tests, new
  bitstreams and new runs, and is the next task. The pre-screen's own list of blind
  spots already names asynchronous resets driven from logic; this is that case, with the
  logic being the harness's.

### What is still open after Task 9b

- **The `rst_n` glitch**, above: one of sixteen runs is a `mismatch` because of it, and
  the fifteen `exact` ones are not proof against it for designs with asynchronous resets.
- The settle of 3 is a ruling that may be reversed; the alternative was an allowance based
  on measured skew.
- DSP cells stay unused in the open flow. A DSP48E1 with a packed register does place and
  route in nextpnr-xilinx 0.8.2; one without fails, in more than one way (an unrouteable
  constant on `CARRYCASCIN` for the pool designs, "Port BCOUT15 has no connections" for
  the test design).
- Bytes before the header are now counted. The single stray byte of the first run did not
  recur in eighteen runs; a run that follows a bitstream which is still sending can start
  with bytes that are not its own.
- Two other tests still build into default trees outside this task's scope:
  `tests/test_clock_build.py` into `build/clock/` and `tests/toolchain/test_blink.py` into
  `build/toolchain/`. Four stale test builds remain in `build/compare/` (see "What
  changed", item 4).
- Unchanged from "Known gaps" and "What remains for the sweep": inputs held fixed, `uio`
  not recorded, nothing at the real clock rate, one board, bitstreams identified by a file
  hash that changes on rebuild, and the verdict paths `nondeterministic` and
  `inconclusive`, half-cycle domains and not-signed-off clocks, which no pool design
  exercises. `mismatch` has now been exercised by a real design.
