# tt-harness-handheld-vga: design

Date: 2026-11-23. Status: draft for the owner's approval.

Supporting facts are in [`docs/gamebub-hardware.md`](../../gamebub-hardware.md),
[`docs/test-designs.md`](../../test-designs.md) and [`docs/toolchains.md`](../../toolchains.md).
This document does not repeat them.

## Intent

Let people see a Tiny Tapeout VGA design running on a handheld FPGA console as it would
after tapeout or on a Tiny Tapeout FPGA board: picture on the screen, the console's
buttons acting as the Gamepad Pmod, sound from the Audio Pmod pin, and a 7-segment display
that behaves like the one on the demo board. Game Bub is the first console. The Digilent
Arty A7 boards on fpgas.online are the test bench, because they can be driven and checked
remotely.

## Terms

- **Project**: an unmodified Tiny Tapeout design, a `tt_um_*` module with the standard
  `ui_in`, `uo_out`, `uio_in`, `uio_out`, `uio_oe`, `ena`, `clk`, `rst_n` ports.
- **Harness**: everything else in the FPGA.
- **Slot**: the place in the harness where the project is instantiated. In stage 2 it
  becomes the reconfigurable region.
- **Stage 1 / 2 / 3**: see "Stages".

## Goals

1. A LiteX harness with a RISC-V soft CPU that hosts one project in a slot and connects
   it the way the VGA Playground does: Tiny VGA pinout on `uo_out`, Gamepad Pmod on
   `ui_in[6:4]` (latch, clock, data), audio on `uio[7]`.
2. A virtual 7-segment display on screen, driven by `uo_out` as on the demo board.
3. The RISC-V firmware runs `demo.json` sequences in the format already used by the demo
   board firmware and the demo configurator (reset, `ui_in`, set/wait steps, loud failure
   on an invalid file).
4. Builds with both Vivado and openXC7, for both Game Bub and Arty A7-35T.
5. On Arty only: record the project's output so it can be compared with a reference
   simulation of the same design.
6. After a small pool is reliable on Arty, build and check every shuttle VGA design that
   has a reference.
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

## Anti-goals

- A harness that only works with one toolchain, or needs the project modified.
- Board-specific logic mixed into the board-independent core, which would make the next
  handheld a rewrite.
- A verdict that depends on two frame-cutting implementations agreeing.
- A dependency tree between the harness projects. A little duplicated code is preferred
  to importing code from several sibling repositories (owner's instruction).
- Copying Game Bub framework RTL (CERN-OHL-S/GPL) into this Apache-2.0 repository.
- Claims about Game Bub hardware behaviour that were only checked in simulation.

## Constraints

- Languages: LiteX and Migen, or plain Verilog. No Chisel (owner's instruction). The
  Game Bub's own framework is Chisel and is not used.
- Game Bub: the MCU owns the SD card and FPGA configuration; the FPGA sees it only over
  QSPI. The panel is parallel RGB scanned as 480x800 portrait. Audio is I2S at a fixed
  48 kHz with MCLK = 256 x fs. One 50 MHz oscillator.
- Arty A7-35T: 1.8 Mbit of block RAM, 256 MB DDR3, Ethernet to the host Pi, 24 Pi GPIOs
  on the PMOD headers, USB JTAG and UART. No screen or audio.
- nextpnr-xilinx 0.8.2 ignored the XDC `create_clock` in the smoke test; builds must pass
  `--freq`.
- The Welland boards are public: check `status.json` and the claims directory before each
  use, keep sessions short, leave boards as found.
- Whether a Game Bub is available for testing is not known (see open questions).

## Approaches

**How the project and harness are combined in stage 1.**

1. *One project per full bitstream.* **Chosen by the owner.** The harness and one project
   are built together; `demo.json` holds a single project.
2. *Mini-shuttle with a mux.* Rejected by the owner as not a priority.
3. *Straight to DFX.* Rejected: it cannot be checked with openXC7 and skips proving the
   peripherals.

**How the picture reaches the screen.** One workable way: sample the project's VGA pins
using the project's own sync into a framebuffer, and scan that out with the harness's own
timing. A pass-through cannot rotate for the portrait panel, and the project cannot be
slaved to harness timing because it generates its own sync.

**How Arty output is recorded.** The raw `uo_out` value per project clock (chosen, and
what the other two projects also record) rather than hashing frames in RTL, which would
make a pass depend on a frame cutter in gateware agreeing with one in software.

## Stages

- **Stage 1** (this design): full bitstream containing harness and one project.
- **Stage 2** (own design later): the slot becomes a Vivado DFX region. The RISC-V loads
  partial bitstreams through ICAPE2. On Game Bub the files come from the SD card using
  the stock firmware's on-demand file commands; `demo.json` may then list several
  projects, each naming a partial bitstream file. The project clock becomes
  run-time programmable.
- **Stage 3** (own design later): partial-region support in openXC7, proven against the
  stage 2 Vivado flow.

Stage 1 must not block stage 2: the slot has a fixed port list and nothing in the harness
reaches inside the project.

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

### Slot and project control

- The slot instantiates the project's top module. `ena` is tied high.
- The project clock comes from an MMCM set at build time to the `clock_hz` in
  `demo.json`. It is a separate clock domain; every signal between project and harness is
  synchronised.
- The RISC-V controls `rst_n` and the scripted part of `ui_in` through registers.
- `uio_in` is driven low. `uio_out` and `uio_oe` are observed (audio).

### Gamepad

- A gamepad emulator produces the Gamepad Pmod's latch, clock and data on `ui_in[6:4]`,
  using the bit order the VGA Playground and Uri Shaked's `gamepad_pmod` receiver expect.
- Its button state comes from a register. On Game Bub the board layer feeds the twelve
  physical buttons (A, B, X, Y, D-pad, L, R, Start, Select) into it; scripted presses
  from the firmware are OR-ed in. On Arty only the firmware sets it.
- The emulator drives `ui_in[6:4]` only when the project declares the Gamepad Pmod
  (`pmods` in the shared manifest); otherwise all of `ui_in` is the scripted value.

### VGA capture and display

- The capture block samples `uo_out` on the project clock, decodes the Tiny VGA pinout
  (2 bits per channel plus hsync and vsync), measures the timing, and writes 6-bit pixels
  into a 640x480 framebuffer in block RAM (Game Bub only).
- Modes larger than 640x480 are cropped to the top-left 640x480 in this version; the
  measured mode is shown in the status area. *(Builder's call, noted as mine.)*
- The Game Bub board layer scans the framebuffer out to the panel, rotated to landscape,
  unscaled. The area beside the picture holds the virtual 7-segment display and a small
  status readout (project name, measured mode, firmware errors).
- No sync from the project means a "no signal" picture, not a frozen frame.

### Virtual 7-segment display

- Segments a-g and the decimal point are `uo_out[0..7]`, as on the demo board.
- Each segment's brightness is its duty cycle averaged over a display frame, so a VGA
  design shows the same shimmer a real display would, and a counter design reads cleanly.
- A firmware error is shown as a flashing pattern on it, matching the demo board's loud
  failure.

### Audio

- `uio_out[7] & uio_oe[7]` is treated as the Audio Pmod's 1-bit output, low-pass filtered
  and decimated to 48 kHz as the VGA Playground does, and sent as I2S (Game Bub).
- Silent if the project never enables that pin.

### SoC and firmware

- LiteX SoC with a VexRiscv CPU at 50 MHz, UART console on Arty.
- Firmware in C. It validates the `demo.json` embedded at build time, applies reset and
  `ui_in` as the demo board firmware does (`ena=1`, `rst_n=0` for 10 project clocks with
  `ui_in` already set), runs the steps, and after `duration_ms` resets and starts again.
- `demo.json` is the existing version 1 format, unchanged, with exactly one project.
  More than one project, unknown fields, or a project that does not match the one built
  in are loud failures at build time and again at boot. Validation mirrors the demo
  configurator's validator (`tt-demo-configurator/src/lib/demoFile.ts`).
- The project is identified by `shuttle` plus `address`, resolved to a macro, repository
  and commit through the shared manifest.
- The project clock is `demo.json`'s `clock_hz`; the manifest's value is not consulted.
  A `clock_hz` of 0 is a loud build failure *(my call)*: the file is the one place the
  clock is stated, and guessing would silently run a design at the wrong speed.
- Scripted gamepad presses are added when the shared `demo.json` format gains them; this
  project does not define its own syntax.

### Game Bub board layer

- Panel timing and the framebuffer scan-out, written new in Migen.
- I2S clocks and data for the DAC.
- A QSPI slave and the part of the stock firmware's core protocol needed to be loaded
  from the SD card menu and stay loaded: the framing, the `0xF1` registers the firmware
  reads (buttons, interrupt and command status), the two mailboxes and the setup/run/halt
  handshakes. Written from the protocol description in `docs/gamebub-hardware.md`.
  The on-demand file commands are stage 2.
- Output package: `/cores/<Author.Name>/` with `core.json` and the `.bit` (design-name
  tag carrying the expected `UserID`).

### Arty board layer

The Arty build has two modes, chosen at build time.

**Demo mode** is the same as Game Bub without the screen and sound: the firmware runs
`demo.json`, the gamepad emulator runs, and the UART console reports state. It exists so
the firmware and gamepad paths can be exercised on a board.

**Compare mode** reproduces the reference simulation's stimulus exactly and records the
result:

- The stimulus is the reference's, not `demo.json`'s: `ui_in` and `uio_in` held at the
  reference's fixed values, the gamepad emulator off, `ena=1`, `rst_n=0` for 10 project
  clocks, applied by a small RTL sequencer so it is exact to the clock. The reference
  keeps the first 8 frames from reset; compare mode records the same span.
- The recorder samples `uo_out` on every project clock from the first reset clock and
  sends the bytes to the host over the UART.
- **The project clock is gated by the recorder's FIFO** *(my call)*: when the FIFO is
  nearly full the project's clock stops, and it resumes when there is room. A Tiny
  Tapeout project is fully synchronous, so its outputs per clock are the same at any
  speed. This gives a complete, gap-free record of any length with no DDR3 and no
  Ethernet, which removes the two largest openXC7 risks from stage 1. Eight frames are
  about 3.4 MB, roughly half a minute at 1 Mbaud.
- What this does and does not show: compare mode proves the project computes the right
  pixels on the FPGA. It does not exercise the project at full speed; that rests on the
  toolchain's timing report for the project clock, which the build checks and records.
  An at-speed recorder using the DDR3 is a possible later addition.
- The host turns the bytes into a vgacap stream: `VGCH` header in external-clock mode
  and `RAW ` chunks (`samples_per_word=4`, first-sample-MSB flag, as the RP2350 capture
  writes). No samples can be lost, so no `TIME` overrun chunk is expected; a UART framing
  or length error makes the run inconclusive.
- The sampler itself is plain Verilog with no vendor primitives and is contributed to
  `vgacap/rtl/` as the first piece of a shared capture library, by a reviewed change on a
  branch in the local `vgacap` checkout (agreed with the capture project, which owns the
  run-length encoder). The clock gate, FIFO and UART framing are LiteX/Migen in this repo.
- A host-side script loads the bitstream, collects the bytes, writes the stream and
  produces a verdict.

**Comparison is per clock, not per frame.** Both the recording and the reference are
the `uo_out` byte for every project clock from the first reset clock, so the verdict is a
byte-for-byte comparison of the two sequences over the reference's span: `exact`,
`mismatch` (with the first differing clock), or `inconclusive`. No frame cutting or
hashing is involved in the verdict. Frames are cut only to show a person what differed.

**Reference** *(my call)*: `tt-harness-fpga-vga` plans to publish, per design, a
`reference.json` and a raw per-clock dump (`uo_out.bin.gz`, one byte per clock, with its
SHA-256), but has not built it yet. This project does not wait: its own Verilator
simulation of the same project under the same stimulus writes the same kind of dump.
When the shared reference files land, the comparison uses them and the local simulation
remains as a cross-check.

**Boards.** The Welland Arty A7-35T boards are on Raspberry Pi 4 hosts reached through
`tweed.welland.mithis.com`, loaded with `openFPGALoader -b arty`, UART on
`/dev/ttyUSB1`; see `docs/hardware/site-welland.md`, `docs/verify-hardware.md` and
`docs/hardware/rpi-hat-pmod.md` in `fpgas.online-test-designs`, and docs.fpgas.online.
Those documents are from October 2026 and ports have moved since, so the first Arty task
is a read-only survey of which boards are live. Compare mode needs only JTAG and the
UART. Use at most two boards, one run at a time per board, a claim file per board in
`~/github/TinyTapeout/tmp/board-claims/`, and stop if `status.json` shows another client.

### Dependencies on other projects

No code is imported from any sibling project to build the harness or to reach a verdict.

- **Data, read as files:** the shared manifest (`data/targets.json` and compiled
  overrides from a `tinytapeout-vga-videos` checkout) and, when they exist, the reference
  files from `tt-harness-fpga-vga`. Their schemas are the interface.
- **Shared RTL:** the sampler lives in `vgacap/rtl/` and is pulled in at a pinned commit
  for the Arty compare build only.
- **Optional tool:** `vgacap`'s frame tools turn a recorded stream into pictures for a
  person to look at. Nothing depends on them for a pass or fail.
- Nothing from any of them is needed to build or run the Game Bub harness.

## Verification

- **Simulation** (no hardware): each core block has unit tests; the gamepad emulator is
  tested against the real `gamepad_pmod` receiver; the QSPI slave against a model of the
  firmware's transactions; and the whole core is simulated with real pool designs, with
  captured frames compared to the reference.
- **Arty hardware**: the eight pool designs, both toolchains, in compare mode. A run
  with missing or malformed data is inconclusive, never a mismatch. The firmware and
  gamepad paths are exercised in demo mode and checked over the UART.
- **Game Bub**: bitstreams and core packages build on both toolchains and pass
  simulation. Anything not run on a real unit is labelled unverified in the docs.
- **Sweep**: every design with a usable reference and no external-hardware dependency,
  built for Arty with Vivado, loaded, recorded and compared. The pool designs are also
  built with openXC7. Outliers go to the owner; the rest are reported as counts.

## Order of work

1. Toolchains. *(Done: openXC7, Yosys, RISC-V GCC installed and smoke-tested.)*
2. Core blocks with simulation tests: slot and control, capture, gamepad, 7-segment, audio.
3. Arty: SoC, recorder, host script; first pool design end to end on a board with Vivado,
   then openXC7.
4. Firmware `demo.json` runner.
5. Whole pool reliable on Arty with both toolchains.
6. Game Bub board layer and core package, to the limit of what can be checked.
7. Sweep on Arty.
8. Stage 2 design, then stage 3.

Steps 2 to 4 and step 6 can overlap.

## Team and process

- At most three sub-agents at once; builds capped at about 44 cores.
- Each sub-agent works in its own git worktree and branch and commits regularly.
- Nothing merges to `main` until a different agent has reviewed the diff and
  independently re-run the validation.
- The repository stays local until the owner names where to push it.

## Left to the owner

- Approving this design.
- Whether a Game Bub can be made reachable for testing.
- Where the repository is published.

## Left to the builder

- Package layout, register maps, block interfaces, filter design, test framework.
- Framebuffer packing and status-area layout.
- Recorder FIFO size and UART framing.
- How project sources are fetched, provided it reads the shared manifest
  (`data/targets.json` and compiled overrides from `tinytapeout-vga-videos`).

## Unverified

- The panel's visible area (720 or 800 wide).
- Whether stock firmware accepts a core implementing only the minimal protocol, and what
  the Home button and overlay do with it.
- Real QSPI throughput.
- Which Welland Arty boards are currently live.
- Whether nextpnr-xilinx handles the gated project clock (BUFGCE) and the project-clock
  MMCM as LiteX emits them.
