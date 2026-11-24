# tt-harness-handheld-vga: design

Date: 2026-11-23. Status: revised after an independent review; awaiting the owner's
approval.

Supporting facts are in [`docs/gamebub-hardware.md`](../../gamebub-hardware.md),
[`docs/test-designs.md`](../../test-designs.md), [`docs/toolchains.md`](../../toolchains.md)
and [`docs/arty-boards.md`](../../arty-boards.md). This document does not repeat them.

## Intent

Let people see a Tiny Tapeout VGA design running on a handheld FPGA console as it would
after tapeout or on a Tiny Tapeout FPGA board: picture on the screen, the console's
buttons acting as the Gamepad Pmod, sound from the Audio Pmod pin, and a 7-segment display
that behaves like the one on the demo board. Game Bub is the first console. A remotely
reachable 7-series board (the Arty A7 boards on fpgas.online) is the test bench.

## Terms

- **Project**: an unmodified Tiny Tapeout design, a `tt_um_*` module with the standard
  `ui_in`, `uo_out`, `uio_in`, `uio_out`, `uio_oe`, `ena`, `clk`, `rst_n` ports.
- **Harness**: everything else in the FPGA.
- **Slot**: the place in the harness where the project is instantiated. In stage 2 it
  becomes the reconfigurable region.
- **Demo build**: a bitstream that runs a project for people to see or use.
- **Compare build**: a test-bench-only bitstream that records a project's outputs.

## Goals

1. A LiteX harness with a RISC-V soft CPU that hosts one project in a slot and connects
   it the way the VGA Playground does: Tiny VGA pinout on `uo_out`, Gamepad Pmod on
   `ui_in[6:4]` (latch, clock, data), audio on `uio[7]`.
2. A virtual 7-segment display on screen, driven by `uo_out` as on the demo board.
3. The RISC-V firmware runs `demo.json` sequences exactly as the demo board's runner
   does.
4. Builds with both Vivado and openXC7, for both Game Bub and Arty A7-35T.
5. On the test bench only: record the project's output so it can be compared with a
   reference simulation of the same design.
6. After a small pool is reliable on the test bench, build and check every shuttle VGA
   design that has a reference.
7. Later stages: the project is loaded separately from the harness while the harness
   runs (stage 2, Vivado DFX), and openXC7 gains the ability to do the same (stage 3).

## Non-goals

- No checking or comparison of any kind in the Game Bub build.
- No Tiny Tapeout mux and no multi-project bitstream. A "mini-shuttle" may become a side
  project; it is not part of this plan.
- No emulation of the QSPI Pmod or other external hardware in the first version. Designs
  that need it are excluded from the sweep.
- No fork of the Game Bub MCU firmware. Stock firmware v1.1-beta is the target.
- No F4PGA. Its install does not resolve; partial-region work goes into openXC7.
- HDMI output through the Game Bub dock, cartridge and link-port loading, and other
  handheld boards are later work, kept possible but not designed here.

**Stage 1 does not deliver "the harness can be built separately".** Every stage 1
bitstream is built from harness plus project together. Separate builds arrive with
stage 2. This is the staging the owner agreed; it is stated here so it is not mistaken
for done.

## Anti-goals

- A harness that only works with one toolchain, or needs the project modified.
- Board-specific logic mixed into the board-independent core, which would make the next
  handheld a rewrite.
- A verdict that depends on two frame-cutting implementations agreeing.
- A dependency tree between the harness projects (owner's instruction: a little
  duplicated code is preferred).
- Copying Game Bub framework RTL (CERN-OHL-S/GPL) into this Apache-2.0 repository.
- Claims about Game Bub hardware behaviour that were only checked in simulation.
- A build that reports success while a clock domain is unconstrained or fails timing.

## Constraints

- Languages (owner's instruction): LiteX and Migen are preferred over Verilog. Plain
  Verilog is acceptable but not the first choice. No Chisel.
- Game Bub: the MCU owns the SD card and FPGA configuration; the FPGA sees it only over
  QSPI. The panel is parallel RGB scanned as 480x800 portrait. Audio is I2S at a fixed
  48 kHz with MCLK = 256 x fs. One 50 MHz oscillator.
- Arty A7-35T: 1.8 Mbit of block RAM, USB JTAG and UART. No screen or audio.
- No Arty at the Welland site is live as of 2026-11-23 (`docs/arty-boards.md`).
- LiteX constrains only the input clock for nextpnr-xilinx and lets failing builds
  through; nextpnr-xilinx 0.8.2 also ignored an XDC `create_clock` in the smoke test.
- The Welland boards are public: check `status.json` and the claims directory before each
  use, keep sessions short, leave boards as found.
- Whether a Game Bub is available for testing is not known.

## Approaches

**How the project and harness are combined in stage 1.**

1. *One project per full bitstream.* **Chosen by the owner.**
2. *Mini-shuttle with a mux.* Rejected by the owner as not a priority.
3. *Straight to DFX.* Rejected: it cannot be checked with openXC7 and skips proving the
   peripherals.

**How the picture reaches the screen.** One workable way: sample the project's VGA pins
using the project's own sync into a framebuffer, and scan that out with the harness's own
timing. A pass-through cannot rotate for the portrait panel, and the project cannot be
slaved to harness timing because it generates its own sync.

**How the test bench checks a project.** Record the raw `uo_out` value on every project
clock and compare it byte for byte with a reference, rather than cutting and hashing
frames in gateware.

## Stages

- **Stage 1** (this design): full bitstream containing harness and one project.
- **Stage 2** (own design later): the slot becomes a Vivado DFX region. The RISC-V loads
  partial bitstreams through ICAPE2. On Game Bub the files come from the SD card using
  the stock firmware's on-demand file commands. Those require every file to be declared
  in advance in the core's `files.json`, at most 16, so the build generates `files.json`
  from `demo.json` and a `demo.json` is limited to about 15 projects. The project clock
  becomes run-time programmable.
- **Stage 3** (own design later): partial-region support in openXC7, proven against the
  stage 2 Vivado flow.

Stage 1 must not block stage 2: the slot has a fixed port list, nothing in the harness
reaches inside the project, and anything that depends on the project (clock frequency
aside) is a run-time register, not a build-time choice.

## How it works (stage 1)

### Structure

A Python package using LiteX/Migen, in three layers:

- **Core** (board-independent): slot, project control, gamepad emulator, VGA capture,
  7-segment sampler, audio filter, and the SoC with its firmware.
- **Boards**: one module per board providing pins, clocks and the board's outputs.
  `gamebub` and `arty` in this design.
- **Build tooling**: a command-line tool that takes a board, a toolchain and a
  `demo.json`, fetches the project's sources, and produces a bitstream and the board's
  package.

### Slot, clock and project control (demo build)

- The slot instantiates the project's top module. `uio_in` is driven low. `uio_out` and
  `uio_oe` are observed (audio).
- The project clock comes from an MMCM set at build time to `demo.json`'s `clock_hz`,
  through a clock-enable buffer so the firmware can stop it. It is a separate clock
  domain; every signal between project and harness is synchronised.
- Many `clock_hz` values need a fractional MMCM setting (25.175 MHz comes out at
  25.1736 MHz, 56 ppm off; 31.5 MHz is exact). The build computes the setting, records
  the achieved frequency, and fails if it is more than 0.1% from the request. It never
  silently substitutes a nearby integer setting such as 25.000 MHz.
- `clock_hz: 0` is valid in `demo.json` and means the clock is stopped, as on the demo
  board. The firmware holds the clock enable off.
- The RISC-V controls `ena`, `rst_n` and the scripted part of `ui_in` through registers.

### Gamepad

- A gamepad emulator produces the Gamepad Pmod's latch, clock and data on `ui_in[6:4]`,
  in the bit order the VGA Playground and the `gamepad_pmod` receiver use.
- Its button state comes from a register. On Game Bub the board layer feeds the twelve
  physical buttons into it; scripted presses from the firmware are OR-ed in.
- Whether the emulator drives `ui_in[6:4]` is a run-time register. The firmware sets it
  from the project's declared Pmods, which the build embeds beside `demo.json`.

### VGA capture and display

- The capture block samples `uo_out` on the project clock, decodes the Tiny VGA pinout
  (2 bits per channel plus hsync and vsync) and measures the timing: clocks per line,
  lines per frame, sync widths and polarities.
- The firmware classifies the mode from those counters and programs the capture's
  horizontal divider (designs clocked at a multiple of the pixel rate), offsets and
  crop. Unknown modes get a best-effort crop from the measured sync and a visible
  "unknown mode" status.
- Pixels go into a 640x480, 6-bit framebuffer in block RAM. Larger modes are cropped to
  the top-left 640x480 in this version.
- The Game Bub board layer scans the framebuffer out to the panel, rotated to landscape,
  unscaled. The area beside the picture holds the virtual 7-segment display and a small
  status readout (project name, measured mode, firmware errors).
- No sync from the project means a "no signal" picture, not a frozen frame.

### Virtual 7-segment display

- Segments a-g and the decimal point are `uo_out[0..7]`, as on the demo board.
- Each segment's brightness is its duty cycle averaged over a display frame, so a VGA
  design shows the shimmer a real display would, and a counter design reads cleanly.
- A firmware error is shown as a flashing pattern on it, matching the demo board's loud
  failure.

### Audio

- `uio_out[7] & uio_oe[7]` is treated as the Audio Pmod's 1-bit output, low-pass filtered
  and decimated to 48 kHz as the VGA Playground does, and sent as I2S (Game Bub).
- Silent if the project never enables that pin.

### SoC and firmware

- LiteX SoC with a VexRiscv CPU at 50 MHz.
- The firmware is a port of the demo board's runner
  (`tt-demo-configurator/src/data/demo.py`), specified from that file, with its tests
  ported. In particular it follows the runner, not the reference testbench, on: the
  order in which enable, clock and `ui_in` are applied; resetting when the steps run out
  (a short reset pulse, `ui_in` not re-applied); never resetting when the steps are
  empty; and not arming the duration timer when there is only one project.
- `demo.json` is the version 1 format, unchanged, with exactly one project in stage 1.
  Validation matches the configurator's validator
  (`tt-demo-configurator/src/lib/demoFile.ts`). An invalid file is a loud failure at
  build time and again at boot.
- The project is identified by `shuttle` plus `address`, resolved to a macro, repository
  and commit through the shared manifest (whose `address` is a string where `demo.json`
  has an integer).
- Scripted gamepad presses are added when the shared `demo.json` format gains them; this
  project does not define its own syntax.

### Game Bub board layer

- Panel timing and the framebuffer scan-out.
- I2S clocks and data for the DAC.
- **MCU link.** The stock firmware writes over QSPI at 40 MHz and starts a 100 ms setup
  timeout as soon as configuration finishes, so:
  - the QSPI slave runs in the SPI clock's own domain (the clock pin is clock-capable),
    not oversampled from 50 MHz;
  - the setup, run, halt and focus handshakes and the registers the firmware polls are
    answered by RTL, so they work before the CPU has booted;
  - the CPU sees the mailboxes through registers for anything slower.
- **Register specification first.** `docs/gamebub-hardware.md` describes the protocol
  only in outline. The first Game Bub task is a complete specification written from the
  firmware source: every `0xF1` register the firmware touches (including input
  capabilities, interrupt enable and pending, command status bits), the button bit
  order, mailbox formats and timeouts. The slave is then built against that
  specification and tested against a model of the firmware's transactions.
- **Overlay.** The firmware draws its entire menu through the `0xF2` overlay. Without it
  the Home menu and any load error are invisible. The overlay is therefore part of the
  Game Bub build, implemented after the link itself: an overlay memory the MCU writes,
  composited over the picture at scan-out.
- Output package: `/cores/<Author.Name>/` with `core.json` and the `.bit`. A `.bit`
  without a `UserID` tag is accepted by the firmware, which is what the open flow
  produces.

### Test bench (Arty) board layer

Two builds.

**Demo build**: the same core as Game Bub with no screen or sound. The firmware runs
`demo.json`, the gamepad emulator runs, and the UART console reports state. It adds two
things for checking:

- a quarter-resolution framebuffer the host can read back over the UART, so the capture
  path and mode classification are seen working on real hardware;
- an at-speed checksum: a CRC of `uo_out` over the first N project clocks after reset
  under the reference stimulus, compared with the same CRC computed from the reference
  dump. It involves no frame cutting. This is the only check that runs the project at
  its real clock speed.

**Compare build**: no CPU and no MMCM. A small state machine:

- generates the project clock as a registered pulse from the 50 MHz domain through a
  global buffer, one pulse each time the recorder has room, so the project, the sampler
  and the recorder are all synchronous and there is nothing to cross between domains;
- applies the reference stimulus exactly: `ena=1`, `ui_in` and `uio_in` at the
  reference's fixed values, `rst_n=0` for 10 clocks, gamepad emulator absent;
- sends the `uo_out` byte for every project clock to the host over the UART. Byte 0 is
  the value after the first reset clock's rising edge, the definition the sibling
  project's reference uses.

A Tiny Tapeout project that is fully synchronous to its clock gives the same output per
clock at any speed, so this record is complete and gap-free at UART speed (eight frames
are about 3.4 MB, roughly half a minute at 1 Mbaud). Designs clocking on both edges
still work. Designs that are not fully synchronous do not, which the verdicts allow for.

**Verdicts.** The comparison is byte for byte over the reference's span:

- `exact`;
- `mismatch`, with the first differing clock;
- `inconclusive`: missing or malformed data;
- `nondeterministic`: the design cannot be expected to match for reasons that are not
  the harness's. The reference simulation chooses its own initial values for
  un-reset registers and the FPGA has its own, and latches, ring oscillators and clocks
  divided in logic behave differently in fabric. The build pre-screens each project with
  the synthesiser's latch and combinational-loop checks and flags these; a mismatch on a
  flagged design is reported under this verdict.

Frames are cut from a recording only to show a person what differed.

**Reference.** `tt-harness-fpga-vga` plans to publish, per design, a `reference.json`
and a raw per-clock dump (`uo_out.bin.gz` with its SHA-256), but has not built it yet.
This project does not wait: its own Verilator simulation of the same project under the
same stimulus writes the same kind of dump. When the shared reference files land, the
comparison uses them and the local simulation remains as a cross-check.

**Boards.** No Welland Arty is live (see Constraints), so on-board steps wait on the
owner's choice of test board. Compare and demo builds need only JTAG and a UART, so
another 7-series board with those would serve; the board layer is small. When boards are
available: at most two, one run at a time each, the board's own lock taken for every
load, a claim file per board in `~/github/TinyTapeout/tmp/board-claims/`, and stop if
`status.json` shows another client.

### Timing

- Every clock domain in a build is constrained for both toolchains. For openXC7 this
  follows the approach already used in `fpgas.online-test-designs`
  (`constrain_openxc7_clocks`) rather than a single `--freq`.
- A build fails if any domain misses timing. The achieved figures are recorded with the
  bitstream.

### Sharing and dependencies

Two instructions from the owner both apply: functionality that can be shared and reused
is a good target, and a complicated dependency tree between projects is not. The rule
used here: **share by making small, self-contained pieces with a written interface, and
do not make a build wait on another repository.**

**Update, 2026-11-24 (owner):** "It is fine for this repo to depend on the vga capture
functionality we own." So `vgacap` may be a real code dependency of this project, pinned to
a commit: its stream format code, frame reconstruction and its RTL capture library. The
rule against a dependency tree still applies to the other sibling projects, whose output is
read as files. The pieces already written here before this was said (a small vgacap stream
writer, the `uo_out` sampler) stay as they are for now; new work that overlaps `vgacap`
should use `vgacap` rather than add another copy, and the existing copies are replaced when
the `vgacap` versions exist and it is convenient.

What this project offers for reuse:

- Each core block (stimulus sequencer, gamepad emulator, VGA capture, 7-segment sampler,
  audio filter, `uo_out` sampler) is a self-contained Migen module with a documented port
  list and its own tests, usable without the rest of the harness or the SoC.
- For projects that do not use LiteX, the blocks likely to be useful elsewhere (sampler,
  gamepad emulator, stimulus sequencer) are also exported as standalone generated
  Verilog with no vendor primitives, checked in simulation.
- The sampler is contributed to `vgacap/rtl/`, the shared capture library, as Migen
  source with its generated Verilog committed beside it and a test that fails if the
  committed Verilog is stale (owner's decision). It is developed and used in this
  repository first; the copy here records the `vgacap` commit it matches. No build here
  pulls from `vgacap`.
- Recordings are written in the shared per-clock dump format and as vgacap streams, so
  other projects' tools can read them.

What this project takes from others, all as files:

- The shared manifest from a `tinytapeout-vga-videos` checkout (`data/targets.json`, the
  `override` entry in each `data/results/.../result.json`, and the tracked override YAML;
  the compiled overrides directory is not in a clean clone).
- The reference files from `tt-harness-fpga-vga`, when they exist.
- Optionally, `vgacap`'s tools to turn a recording into pictures for a person.

Nothing from any of them is needed to build or run the Game Bub harness.

## Verification

- **Simulation** (no hardware): each core block has unit tests; the gamepad emulator is
  tested against the real `gamepad_pmod` receiver; the firmware runner against the
  ported `demo.py` tests; the QSPI slave against a model of the firmware's transactions;
  and the whole core is simulated with real pool designs.
- **Test bench hardware**: the eight pool designs, both toolchains. Compare build for
  the byte-for-byte verdict; demo build for the at-speed checksum, the framebuffer
  readback and the firmware and gamepad paths.
- **Game Bub**: bitstreams and core packages build on both toolchains and pass
  simulation. Anything not run on a real unit is labelled unverified in the docs.
- **Sweep**: every design with a usable reference and no external-hardware dependency,
  as a compare build with Vivado, loaded, recorded and compared; the at-speed checksum
  from a demo build for each as well. The pool designs are also built with openXC7.
  Outliers go to the owner; the rest are reported as counts per verdict.

What could still pass every check and be wrong on a Game Bub: the panel timing and
rotation, the audio level and filtering, the MCU link under real electrical conditions,
and the overlay. These need a unit.

## Order of work

Spikes first, because each can change the design:

- **A.** Compare build on its own (stepping clock, sampler, UART, one pool design), built
  with both toolchains, checked in simulation against the local reference.
- **B.** Fractional MMCM for 25.175 MHz and a clock-enable buffer under openXC7, with
  per-domain constraints.
- **C.** Game Bub register specification from the firmware source, and a QSPI slave in
  the SPI clock domain passing a transaction model of setup, run and focus.

Then:

1. Core blocks with simulation tests.
2. SoC and firmware runner; demo build for the test bench.
3. Pool reliable on the test bench with both toolchains (needs a board).
4. Game Bub board layer: display, audio, link, overlay, core package.
5. Sweep.
6. Stage 2 design, then stage 3.

Steps 1, 2 and 4 can overlap. A possible saving for the sweep, at the builder's
discretion: a Vivado checkpoint of the harness with the slot as a black box, reused
across projects.

## Team and process

- At most three sub-agents at once; builds capped at about 44 cores.
- Each sub-agent works in its own git worktree and branch and commits regularly.
- Nothing merges to `main` until a different agent has reviewed the diff and
  independently re-run the validation.
- The repository stays local until the owner names where to push it.

## Left to the owner

- Approving this design.
- Which 7-series board to test on, given no Welland Arty is live.
- Whether a Game Bub can be made reachable for testing.
- Where the repository is published.

## Left to the builder

- Package layout, register maps, block interfaces, filter design, test framework.
- Framebuffer packing, status-area and overlay layout.
- UART framing for the recorder and the readback.
- N for the at-speed checksum.
- How project sources are fetched, provided it reads the shared manifest.

## Unverified

- The panel's visible area (720 or 800 wide). At 720 the area beside the picture is only
  80 pixels, which is tight for the 7-segment display and status.
- Whether stock firmware keeps a core loaded that implements the protocol as specified
  here; it can only be modelled until a unit is available.
- Real QSPI throughput.
- That nextpnr-xilinx handles the fractional MMCM and clock-enable buffer as LiteX emits
  them (it lists the primitives; no build has been run). Spike B settles it.
- LiteX's behaviour when a requested clock is not exactly achievable.
