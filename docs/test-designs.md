# Test designs and reference data

Collected 2026-11-23 from the session that maintains
[`tinytapeout-vga-videos`](https://github.com/mithro/tinytapeout-vga-videos), based on its
run of all 440 VGA projects on 17 September 2026. Paths below are relative to a checkout of
that repository unless they start with `/`.

## First test pool

| Shuttle / macro | Author | Why it is in the pool |
|---|---|---|
| `tt07/tt_um_tinytapeout_dvd_screensaver` | Uri Shaked | plain 640x480@60, 25.175 MHz, 1x1 |
| `tt08/tt_um_tinytapeout_logo_screensaver` | Uri Shaked | plain 640x480@60, 25.175 MHz, 1x1 |
| `ttihp26a/tt_um_urish_rings` | Uri Shaked | declares clock 0; reference ran at 25.175 MHz |
| `ttihp0p3/tt_um_algofoogle_fun_vga_clock` | Anton Maurovic | 25.000 MHz, about 59.5 fps, 1x2 |
| `ttihp25a/tt_um_algofoogle_vga` | Anton Maurovic | 25.000 MHz, 1x2 |
| `ttsky26a/tt_um_algofoogle_dottee` | Anton Maurovic | audio on `uio[7]`; 64% of pixels change per frame |
| `ttsky25a/tt_um_gamepad_pmod_demo` | Uri Shaked | the only reference run that drove the Gamepad Pmod |
| `ttihp25a/tt_um_2048_vga_game` | Uri Shaked | gamepad game, 2x2; the reference run pressed nothing |

Awkward cases, to add once the pool above is reliable:

| Shuttle / macro | Why it is awkward |
|---|---|
| `tt07/tt_um_vga_perlin` | 31.5 MHz, 640x480@72 |
| `tt08/tt_um_johshoff_metaballs` | 50 MHz, 800x600@72 |
| `tt08/tt_um_MichaelBell_mandelbrot` | 100 MHz |
| `ttihp0p2/tt_um_algofoogle_raybox_zero` | 4x2, needs the QSPI Pmod; the reference is nearly static, so not a pixel reference |

No design is confirmed to drive a 7-segment display on `uo_out` alongside VGA. Unchecked
candidates from a keyword search: `ttsky25b/tt_um_verilog_meetup_template_project_example`
and `tt08/tt_um_nvious_graphics`. The 7-segment path is otherwise tested with non-VGA
designs such as the factory test.

## Which projects have a reference

Filter `data/index.json` `projects[]` on `verdict`. Of 440 projects, 408 have clips. Without:

- skipped (9): analogue, plus one Wokwi project
- build-failed (9): Verilator failed
- fetch-failed (1)
- no-sync (12): mostly need the QSPI Pmod or external input
- blank (18): many are QSPI or framebuffer designs that drew nothing with the test-pattern
  flash; these reflect the simulation inputs, not the designs

Also suspect: unstable-sync (4), bad-timing (1). `simulation.qspi` lists the 35 designs
simulated with a QSPI pin map.

## Shared manifest

Both FPGA harness projects (this one and `tt-harness-fpga-vga` for the iCE40 boards) take
their per-project inputs from the same files rather than fetching independently:

- `data/targets.json` (key `targets`): repo, commit, top_module, source_files, pinout,
  pmods, clock_hz (declared; 0 means unset, and the reference used 25.175 MHz), tiles.
- `overrides/<shuttle>/<macro>.yaml`, compiled to
  `data/overrides-compiled/<shuttle>/<macro>.{json,inputs,gamepad}`: per-project sources,
  top module, clock, `ui_in`, patches, QSPI map, flash contents and input scripts. Times
  are in clocks from the first reset clock.
- **`data/overrides-compiled/` is gitignored**, so a clean clone does not have it. From a
  clone, read instead: each project's override dict in
  `data/results/<shuttle>/<macro>/result.json` (key `override`), and the tracked
  `overrides/<shuttle>/<macro>.yaml` for input events (interpreted as
  `ttvga/overrides.py` does). Reported by the `tt-harness-fpga-vga` project, 2026-11-23.
- Ten designs have a non-zero fixed `ui_in` in the reference run, for example
  `tt_um_toivoh_retro_console` on tt06 and ttihp0p2 (0x30) and, on ttsky26c,
  `tt_um_moein_maleki_arm16` and `tt_um_nobleg30_uart_vga_scroller` (0x08). The
  `reference.json` files from `tt-harness-fpga-vga` carry `ui_in` and `uio_in` per
  design (the values applied; schema 3, 2026-11-25).
- Fetch logic: `ttvga/harness/job.py` (shallow fetch of the commit, then apply the patch).

## What is shared between the projects (as of 2026-11-23)

The arrangement changed during the day; the current one is in the design document under
"Sharing and dependencies". In short: comparison is per clock against a raw `uo_out`
dump, so no project depends on another's frame cutter or hash. `tt-harness-fpga-vga`
keeps its own frame cutter and hash and publishes written definitions
(`docs/frame-definition.md` and the `reference.json` schema in that repository).
`vgacap` owns the capture stream format and a shared capture RTL library (`vgacap/rtl/`).

Lessons from capturing real boards (from the `tt-vga-capture` project):

- A frame with missing samples is "inconclusive", never "mismatch".
- Match frame sequences by searching for an offset, not by index.
- Pin `ui_in`; a demo board driving `ui_in=1` once switched a design to 800x600.
- The VGA Playground `hvsync_generator` is one pixel and one line off VESA phase.
- Whether the reference testbench and `libvgaframe` cut frames at identical pixels is
  unproven.

## Comparing against the reference

- The published clips are 2x nearest-neighbour, yuv420p and lossy. They are for looking
  at, not for pixel comparison. They live under
  `/srv/data.wafer.space/tinytapeout-vga-videos/<shuttle>/<macro>/`.
- Exact reference: the testbench computes one FNV-1a 64-bit hash per frame over
  native-resolution RGB888, row-major, each channel being the 2-bit value times 85. The
  function is at `ttvga/harness/tb.cpp:237`.
- The per-frame hash lists are published since 2026-11-23 (commit 3505405 of
  `tinytapeout-vga-videos`), for all 408 projects with frames, now at
  `data/results/<shuttle>/<macro>/frames/tb-v1/frame_hashes.fnv1a64-tb2026-rgb888.txt`.
  They were first at `data/results/<shuttle>/<macro>/frame_hashes.txt` and moved in commit
  `e112cdb`; "The hash lines did not change, and neither did `hash_offset_basis`"
  ([`docs/consumers.md` at 26b1004](https://github.com/mithro/tinytapeout-vga-videos/blob/26b1004/docs/consumers.md)).
  The file has `#` comment lines, then `key: value` lines (id,
  mode, width, height, clock_hz, clocks_per_pixel, capture_after_clock, frames), then one
  hash per line, frame 0 first. Frame 0 starts at the first vsync-pulse end after
  `capture_after_clock`; clock 0 is the first clock of the reset sequence.
- **Those hashes are not standard FNV-1a 64.** `ttvga/harness/tb.cpp:238` has the offset
  basis as 1469598103934665603 (0x14650fb0739d0383); the standard one is
  14695981039346656037 (0xcbf29ce484222325). A standard FNV-1a over the correct pixels
  mismatches every line. Reported by the `tt-harness-fpga-vga` project on 2026-11-23,
  which reproduced the published hashes exactly using the mistyped basis, so the pixels
  agree. Resolved on 2026-11-24: the videos project kept the
  hashes and documented the constant. Each `frame_hashes.fnv1a64-tb2026-rgb888.txt` carries a
  `hash_offset_basis` line (0x14650fb0739d0383); pass that value to the FNV routine when
  comparing. Pushed to `mithro/tinytapeout-vga-videos` main (148ccf5..87cea85). The videos testbench's clock counter is the
  per-clock sample index plus 1.
- `tt-harness-fpga-vga`'s own reference files use standard FNV-1a 64. Its
  `reference.json` schema 3 has, among others: id, profile, clock_hz, clock_declared, reset_clocks, ui_in,
  uio_in, clocks, dump (`uo_out.bin.gz`), dump_sha256 (of the uncompressed bytes),
  uio_dump, uio_dump_sha256, mode, line_clocks, lines, sync polarities, width, height,
  frames (index, start_clock, hash, png), frames_complete, partial_frames_dropped, tools.
- This project reads schema 3 only (`tt-harness-fpga-vga` commit 112ff4b, 2026-11-25;
  `docs/frame-definition.md` in that repository). The file is
  `work/reference/<shuttle>/<macro>/<profile>/reference.json`; this harness drives every
  input itself, so it reads the `ideal` profile and checks `profile` is `ideal`. `ui_in`
  and `uio_in` are the values applied. `ui_in_unmet` and `uio_in_unmet` are the bits
  where the applied value differs from the requested one; non-zero means the design did
  not see what was asked, and this project refuses such a reference. A design whose pads
  do not settle has a `reference-fail.json` (`id`, `status`, `reason`, `timed_out`,
  `limited`, `limits`) and no `reference.json`; this project reports its status and
  reason. Schemas 1 and 2 and the old path without a profile directory are refused. The
  fields this project relies on: `dump` is `uo_out.bin.gz`, one byte per clock, byte *i* being `uo_out` after the
  rising edge of clock *i*; clock 0 is the first reset clock; `rst_n` is low for the
  rising edges of clocks 0 to 9; `dump_sha256` is of the uncompressed bytes; `uio_dump`
  is `uio.bin.gz`, two bytes per clock (`uio_out` then `uio_oe`). Reference files are
  under `work/reference/<shuttle>/<macro>/<profile>/` in that checkout.
- For anyone cutting frames: a design using the VGA Playground's sync generator is cut
  one line later than its own row 0, so row 479 is black porch. Per-clock comparison is
  unaffected.
- Reference files for the eight pool designs (and five calibration designs) exist since
  2026-11-23 under `work/reference/<shuttle>/<macro>/` (since 2026-11-25
  `.../<macro>/ideal/`, schema 3) in the `tt-harness-fpga-vga`
  checkout on this machine: `reference.json`, `uo_out.bin.gz` (6,000,000 clocks),
  `uio.bin.gz` and eight frame PNGs. That directory is git-ignored scratch, not in the
  repository. All eight use `ui_in` 0 and `uio_in` 0 and simulate to 640x480@60.
- Notes on the pool from that project's first batch:
  - `tt_um_algofoogle_dottee` has SystemVerilog syntax (`p2 >>= 1;`) in a `.v` file, so
    Yosys needs `read_verilog -sv`. It also has a second clock derived from its own vsync
    output (`uo_out[3]`); check multi-clock timing figures carefully.
  - `tt_um_gamepad_pmod_demo` and `tt_um_2048_vga_game` show one static picture with idle
    inputs, so their references prove only the still image.
  - On the iCE40UP5K five of the eight miss 25.175 MHz on paper (17 to 23 MHz).
- This project's verdict is per clock and does not use these hashes. They are a second,
  independent check: frames cut from a recording can be hashed and compared with them.
- Reset stimulus in the reference: `ena=1`, `rst_n=0` for 10 clocks with `ui_in` already
  set.
- Frame 0 is not the first frame after reset. The testbench calibrates on sync for up to
  one second and captures from the next vsync end. The clock count where that happened is
  in `sim_log_tail` of `result.json` as `tb: sync at clock N`.
