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

Both FPGA harness projects (this one and `tt-vga-fpga-harness` for the iCE40 boards) take
their per-project inputs from the same files rather than fetching independently:

- `data/targets.json` (key `targets`): repo, commit, top_module, source_files, pinout,
  pmods, clock_hz (declared; 0 means unset, and the reference used 25.175 MHz), tiles.
- `overrides/<shuttle>/<macro>.yaml`, compiled to
  `data/overrides-compiled/<shuttle>/<macro>.{json,inputs,gamepad}`: per-project sources,
  top module, clock, `ui_in`, patches, QSPI map, flash contents and input scripts. Times
  are in clocks from the first reset clock.
- Fetch logic: `ttvga/harness/job.py` (shallow fetch of the commit, then apply the patch).

## Comparing against the reference

- The published clips are 2x nearest-neighbour, yuv420p and lossy. They are for looking
  at, not for pixel comparison. They live under
  `/srv/data.wafer.space/tinytapeout-vga-videos/<shuttle>/<macro>/`.
- Exact reference: the testbench computes one FNV-1a 64-bit hash per frame over
  native-resolution RGB888, row-major, each channel being the 2-bit value times 85. The
  function is at `ttvga/harness/tb.cpp:237`.
- The per-frame hash lists are **not** published. `tt-vga collect`
  (`ttvga/remote.py:263-268`) replaces `timing.frame_hashes` in
  `data/results/<shuttle>/<macro>/result.json` with `frame_hashes_count`; the full lists
  exist only in the pipeline's private scratch directories. Reference hashes therefore
  have to be recomputed by re-running the testbench with the same stimulus. The
  `tt-vga-fpga-harness` project plans to do this, and the result should be shared between
  the two harness projects.
- Reset stimulus in the reference: `ena=1`, `rst_n=0` for 10 clocks with `ui_in` already
  set.
- Frame 0 is not the first frame after reset. The testbench calibrates on sync for up to
  one second and captures from the next vsync end. The clock count where that happened is
  in `sim_log_tail` of `result.json` as `tb: sync at clock N`.
