# Core blocks, SoC, firmware runner and the Arty demo build: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A demo build that runs one Tiny Tapeout project at its real clock speed under a RISC-V running the demo board's `demo.json` runner, with gamepad emulation, VGA capture into a framebuffer, the virtual 7-segment sampler and the audio filter, built for the Arty with both toolchains and checked on hardware by an at-speed checksum and a framebuffer readback.

**Architecture:** Board-independent Migen blocks in `tt_harness/core/`, each usable and tested alone. A `ProjectSlot` owns the project instance, its MMCM clock and every signal crossing between the system and project clock domains. A LiteX SoC (VexRiscv, 50 MHz) exposes the blocks as CSRs; C firmware parses the embedded `demo.json` and runs it. The Arty demo board layer adds a UART console protocol for the host. Game Bub's screen, sound, link and overlay are plan 3; the sweep is plan 4.

**Tech Stack:** Python 3.12 with `uv`; Migen e19524c, LiteX and litex-boards 2026.08 (already pinned); VexRiscv via LiteX; C firmware built with the RISC-V GCC in the OSS CAD Suite; Verilator for simulation; Vivado 2025.2 and openXC7 0.8.2; pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-tt-harness-handheld-vga-design.md` (sections "How it works (stage 1)", "Test bench (Arty) board layer", "Verification"). Results of plan 1 that bind this plan: `docs/results/2026-11-spike-a-compare-build.md`, `docs/results/2026-11-spike-b-project-clock.md`.

## Global Constraints

- Gateware is Migen/LiteX. No Chisel. Plain Verilog only for third-party project sources and for generated output.
- `uv run` for all Python. Never write under `/tmp`. No `2>/dev/null`. Never pipe a command into grep/head/tail: log to a file under the worktree's `tmp/`, then read it.
- New source files carry `# SPDX-License-Identifier: Apache-2.0`.
- Each task is done in its own git worktree and branch, committed in small commits, and merged only after an independent review that re-runs the validation.
- One FPGA build at a time per agent, at most 16 cores each.
- A build fails if any clock domain is unconstrained or misses timing, in both toolchains. The achieved figures are recorded with the bitstream.
- The project clock comes from `tt_harness.core.clocking.ProjectClock` unchanged (explicit `COMPENSATION="ZHOLD"`). Every build that contains it runs the decoded-feedback gate (`tt_harness.mmcm_fasm.check_feedback`) and fails without the dedicated feedback.
- Every signal the harness drives into the project, other than its clock, is a constant or the direct output of a flip-flop **clocked by the project clock**. Every build checks this in the implemented netlist (`tt_harness.input_gate`), including the register's clock.
- The achieved project clock must be within 0.1% of `clock_hz`; the build never substitutes a nearby setting. `clock_hz: 0` means the clock stays stopped.
- `demo.json` is the version 1 format of `tt-demo-configurator` at commit 8b7c392, unchanged, including `{"gamepad": [...]}` steps with up to two controllers. Exactly one project in this plan. This project defines no syntax of its own.
- The firmware follows the demo board's runner, not the reference testbench, on: the order in which enable, clock and `ui_in` are applied; resetting when the steps run out (a short reset pulse, `ui_in` not re-applied); never resetting when the steps are empty; not arming the duration timer when there is only one project.
- An invalid `demo.json` is a loud failure at build time and again at boot.
- Shared hardware: the PS1 Arty is a public board. Check for other users by hand (15 minutes idle, UART unheld) and with the runner's own check before every session; no sudo; leave nothing behind.
- `tt_harness/build.py` (the compare build's flow) is not restructured in this plan. Shared helpers are moved out of it only when a task says so.

## Review Focus

The inputs and conditions the spec implies that no single task's obvious tests exercise, most likely to bite first. Each has its test in the task named.

1. **A project that never produces sync.** The capture must report "no signal" and the framebuffer must not show a frozen frame (Task 3: `test_no_sync_reports_no_signal_and_blanks`).
2. **`clock_hz: 0` and a stopped clock.** Nothing in the harness may wait on a project-domain event that never comes: register writes, reset, the at-speed check and the capture status must all return (Task 5: `test_stopped_clock_never_blocks_the_system_side`).
3. **A `demo.json` that is valid JSON but invalid as a demo** (unknown field, float where an integer is required, `"set"` touching the gamepad pins, twelve buttons at once). Build and boot must both refuse it with the configurator's exact message (Task 1 vectors; Task 7: `test_firmware_rejects_every_invalid_vector`).
4. **A design clocked at a multiple of its pixel rate, or with unusual sync polarity.** The classifier must set the divider and polarity from measurement, not assume 640x480 at 25.175 MHz (Task 3: `test_double_rate_design_is_divided`, `test_positive_sync_polarities`).
5. **Reset release against a running clock.** `rst_n` is asserted asynchronously and released on a project clock edge, never mid-cycle, whatever the system clock phase (Task 5: `test_reset_release_is_synchronous_at_every_phase`).

---

### Task 1: Runner specification, validator and shared vectors

The firmware is written from a specification, not translated from `demo.py` (which is LGPL-2.1; this repository is Apache-2.0).

**Files:**
- Create: `docs/demo-runner.md`, `tt_harness/demo/__init__.py`, `tt_harness/demo/validate.py`, `tt_harness/demo/model.py`, `tests/data/demo/valid/*.json`, `tests/data/demo/invalid/*.json`, `tests/data/demo/traces/*.json`, `tests/test_demo_validate.py`, `tests/test_demo_model.py`

**Interfaces:**
- Produces: `validate.validate(conf: object, shuttle: str) -> list[str]` (messages identical to `demoFile.ts` `validateDemo` without a lookup); `validate.parse(text: str, shuttle: str) -> DemoFile` raising `DemoError(errors: list[str])`; dataclasses `DemoFile(version, shuttle, projects)`, `Entry(address, subtile, duration_ms, clock_hz, ui_in, steps)`, `Step` as `Set(value) | WaitMs(ms) | Gamepad(controllers: tuple[tuple[str, ...], ...])`; `validate.GAMEPAD_BUTTONS: tuple[str, ...]` in wire order; `validate.gamepad_bits(controllers) -> int` (24-bit value as shifted out, with "no controller" encoded as the receiver expects).
- Produces: `model.run(entry: Entry, total_projects: int, until_ms: int) -> list[Event]` where `Event(t_ms: int, kind: str, value: int)` and `kind` is one of `ena`, `clock_hz`, `rst_n`, `ui_in`, `gamepad`: the exact ordered writes the runner makes. This is the oracle for Task 7.

- [ ] **Step 1:** Write `docs/demo-runner.md` from `tt-demo-configurator/src/data/demo.py` (commit 8b7c392; its header names the upstream commit): what the runner does for one entry, in order, with times; the reset pulse (length, which signals, what is not re-applied); empty steps; the duration timer with one project; gamepad steps (frame format, bit order, both controllers, how long buttons stay held); what an error does. State facts and cite line numbers; do not copy code.
- [ ] **Step 2:** Vectors: every case in `tt-demo-configurator/src/lib/demoFile.test.ts` as a file under `tests/data/demo/`, each invalid one with its expected messages in a sibling `.errors.json`. Record the source commit in `tests/data/demo/README.md`.
- [ ] **Step 3:** Failing tests `test_every_invalid_vector_gives_exactly_the_recorded_messages`, `test_every_valid_vector_parses`, `test_float_where_integer_required_is_rejected` (Python keeps `1.0` a float: the board rejects it, and so must this).
- [ ] **Step 4:** Implement `validate.py`; tests pass.
- [ ] **Step 5:** Traces: for four entries (no steps; set/wait steps that wrap; gamepad steps with two controllers; `clock_hz: 0`), write the expected event list by hand from the specification into `tests/data/demo/traces/`. Failing test `test_model_reproduces_every_trace`, then implement `model.py`.
- [ ] **Step 6:** Commit.

### Task 2: Gamepad emulator

**Files:**
- Create: `tt_harness/core/gamepad.py`, `tests/test_gamepad.py`, `tests/data/gamepad/gamepad_pmod.v` (the real receiver, with its licence and source commit recorded beside it), `tt_harness/sim/gamepad_tb.cpp`

**Interfaces:**
- Consumes: `validate.gamepad_bits`, `validate.GAMEPAD_BUTTONS`.
- Produces: `GamepadEmulator(clk_freq_hz: int)` in the clock domain it is instantiated in, with `buttons: Signal(24)` (level, sampled at the start of each frame), `latch`, `clock`, `data` outputs (each a direct register output), `frame: Signal()` (one-cycle pulse per frame sent). Timing constants at module level, taken from the Gamepad Pmod's firmware.

- [ ] **Step 1:** Record the protocol in the module docstring: frame rate, clock period, bit order, idle levels, how "no controller" reads (sources: the Gamepad Pmod repository and `tt-demo-configurator/src/lib/gamepad.ts`).
- [ ] **Step 2:** Failing tests, simulated with Verilator against the real `gamepad_pmod.v` receiver clocked at 25.175 MHz and at 50 MHz: `test_each_single_button_is_received_on_controller_1`, `test_each_single_button_on_controller_2`, `test_two_controllers_together`, `test_no_controller_reads_as_absent`, `test_buttons_change_only_between_frames`, `test_outputs_are_register_outputs` (fragment walk as in `tests/test_project_inputs.py`).
- [ ] **Step 3:** Implement; tests pass.
- [ ] **Step 4:** Commit.

### Task 3: VGA timing measurement, capture and framebuffer

**Files:**
- Create: `tt_harness/core/vga.py`, `tests/test_vga.py`, `tests/vga_model.py` (a parameterised VGA signal generator for tests)

**Interfaces:**
- Produces, all in the `project` clock domain unless stated:
  - `decode(uo_out) -> (r, g, b, hsync, vsync)` for the Tiny VGA pinout (2 bits per channel).
  - `VgaTiming()`: input `uo_out`; outputs `clocks_per_line: Signal(12)`, `lines_per_frame: Signal(11)`, `hsync_width: Signal(12)`, `vsync_lines: Signal(11)`, `hsync_positive`, `vsync_positive`, `frames: Signal(16)` (wrapping count of frames seen), `locked` (two consecutive frames with identical measurements), `no_signal` (no frame completed within `NO_SIGNAL_CLOCKS`). Measurements are held stable and published together on the frame boundary.
  - `VgaCapture(width: int, height: int)`: inputs `uo_out`, and settings `divider: Signal(3)` (project clocks per pixel), `h_offset: Signal(12)`, `v_offset: Signal(11)`, `h_step`, `v_step` (decimation, for the quarter-size buffer); a write port `we`, `addr`, `data: Signal(6)`; `blank` input that forces writes of zero for one whole frame.
  - `Framebuffer(width, height)`: dual-clock block RAM, write side in `project`, read side in the domain given, 6 bits per pixel.
  - `classify(clocks_per_line, lines_per_frame, hsync_width, vsync_lines, clock_hz) -> Mode | None` in plain Python (`Mode(name, divider, h_offset, v_offset, width, height)`), with the same table emitted as a C header for the firmware (`tt_harness/core/vga_modes.h` generated, with a test that fails if it is stale).

- [ ] **Step 1:** Failing tests with `vga_model`: `test_measures_640x480_at_25_175`, `test_positive_sync_polarities`, `test_double_rate_design_is_divided`, `test_measurements_publish_together`, `test_no_sync_reports_no_signal_and_blanks`, `test_capture_writes_the_picture_at_the_offsets`, `test_quarter_size_capture_decimates`, `test_larger_mode_is_cropped_top_left`, `test_unknown_mode_classifies_as_none`.
- [ ] **Step 2:** Implement; tests pass.
- [ ] **Step 3:** One pool design end to end in simulation: Verilator runs `tt_um_tinytapeout_dvd_screensaver` into `VgaTiming` and `VgaCapture`; the captured frame equals frame 2 cut from the reference dump with `vgacap` (`test_pool_design_frame_matches_reference`, marked `toolchain`).
- [ ] **Step 4:** Commit.

### Task 4: Seven-segment sampler and audio filter

**Files:**
- Create: `tt_harness/core/sevenseg.py`, `tt_harness/core/audio.py`, `tests/test_sevenseg.py`, `tests/test_audio.py`

**Interfaces:**
- Produces: `SevenSegSampler(window_clocks_log2: int)` in `project`: input `uo_out`; outputs `duty: Array(Signal(8) for 8)` (segments a-g then the decimal point, 0..255 = fraction of the window each bit was high), published together once per window, plus `window: Signal()` pulse.
- Produces: `AudioFilter(clk_freq_hz: int, sample_rate: int = 48000)` in `project`: inputs `uio_out`, `uio_oe`; outputs `sample: Signal((16, True))`, `valid`. Source bit is `uio_out[7] & uio_oe[7]`. Filter structure and decimation as the VGA Playground does (read `vga-playground`'s audio path and record what it does and at which commit in the docstring). Silent (zero) when the pin is never enabled.

- [ ] **Step 1:** Failing tests: `test_constant_segments_read_0_and_255`, `test_half_duty_reads_128_within_1`, `test_window_publishes_all_eight_together`; `test_silent_when_pin_not_enabled`, `test_square_wave_comes_out_at_its_frequency` (FFT peak of the decimated output), `test_dc_is_removed_or_bounded` (state which and why), `test_sample_rate_is_exact_for_25_175_and_50_mhz` (a fractional decimator: state the long-run rate error).
- [ ] **Step 2:** Implement; tests pass. Commit.

### Task 5: Project slot, control and the at-speed check

The only module that contains both clock domains. Everything else is in one domain.

**Files:**
- Create: `tt_harness/core/slot.py`, `tt_harness/core/atspeed.py`, `tests/test_slot.py`, `tests/test_atspeed.py`
- Modify: `tt_harness/compare/inputs.py` and `tt_harness/input_gate.py` (clock-domain check), their tests

**Interfaces:**
- Consumes: `ProjectClock`, `GamepadEmulator`, `UoSampler` conventions, `ReferenceStimulus` constants (`RESET_CLOCKS = 10`).
- Produces: `ProjectSlot(top_module: str, clk_in: Signal, clk_in_hz: int, clock_hz: int)` creating clock domain `project` and exposing, in `sys`:
  - controls (level, each crossing through a two-flop synchroniser into `project`): `ena`, `rst_n`, `ui_in: Signal(8)`, `gamepad_enable`, `gamepad_buttons: Signal(24)`, `clock_enable`;
  - status: `locked`, `clock_running`;
  - in `project`: `uo_out`, `uio_out`, `uio_oe` (raw, for the blocks of Tasks 3 and 4).
  - Rules it implements: `ui_in[6:4]` comes from the gamepad emulator when `gamepad_enable`, else from `ui_in`; `uio_in` is zero; `rst_n` into the project is asserted asynchronously and released by a two-flop synchroniser in `project`; a multi-bit control (`ui_in`, `gamepad_buttons`) crosses with a handshake so the project never sees a mixed value; with the clock stopped, writes are held and applied on the first clock, and no system-side operation blocks.
- Produces: `AtSpeedCheck()` with `sys`-side `start` (pulse), `clocks: Signal(32)` (N), `done`, `crc: Signal(32)`; on start it stops the clock, puts the project in the reference stimulus state, restarts the clock, holds `rst_n` low for clocks 0..9 and accumulates zlib CRC-32 over `uo_out` for clocks 0..N-1, where byte k is the value after rising edge k (the reference dump's definition).
- Produces: `inputs.project_inputs` and the netlist gate also report the clock of each driving register; the gate fails when it is not the project clock net.

- [ ] **Step 1:** Failing tests: `test_controls_reach_the_project`, `test_multibit_control_is_never_mixed` (toggle between 0x00 and 0xFF at every relative phase of two unrelated clocks; the project side sees only those two values), `test_reset_release_is_synchronous_at_every_phase`, `test_gamepad_mux`, `test_stopped_clock_never_blocks_the_system_side`, `test_project_inputs_are_project_domain_registers` (fragment walk, now with domain), `test_gate_rejects_register_in_the_wrong_domain` (saved netlist cases for both flows).
- [ ] **Step 2:** Implement `slot.py` and the gate change; tests pass.
- [ ] **Step 3:** Failing tests for `AtSpeedCheck` in Verilator with two pool designs at their real clock ratio to a 50 MHz system clock: `test_crc_equals_crc_of_reference_dump` (N = the reference's clock count), `test_crc_differs_when_one_byte_differs` (a design variant), `test_restart_gives_the_same_crc`. Implement; pass.
- [ ] **Step 4:** Commit.

### Task 6: SoC and simulation

**Files:**
- Create: `tt_harness/soc/__init__.py`, `tt_harness/soc/core.py`, `tt_harness/soc/sim.py`, `docs/registers.md` (generated), `tests/test_soc.py`

**Interfaces:**
- Consumes: Tasks 2 to 5.
- Produces: `HarnessSoC(platform, sys_clk_freq: int, slot: ProjectSlot, fb_size: tuple[int, int], demo_json: bytes, project_name: str, with_audio: bool, with_readback: bool)`, a LiteX `SoCCore` with VexRiscv (`standard` variant), UART at 115200 baud, timer, integrated ROM for the BIOS and firmware, and CSR modules named `slot`, `gamepad`, `vga`, `sevenseg`, `atspeed`, `info`. `info` holds build facts as read-only CSRs: requested and achieved `clock_hz` (achieved as numerator and denominator), framebuffer size, flags for the project's declared Pmods. `demo.json` is linked into the firmware image as bytes; nothing is derived from it at build time except the clock setting.
- Produces: `soc.sim.run(sources, demo_json, until: Callable, max_cycles) -> SimResult` (Verilator, LiteX simulation platform, UART output captured).
- `docs/registers.md` is generated from the CSR map with a test that fails if it is stale.

- [ ] **Step 1:** Failing tests: `test_csr_map_is_documented_and_current`, `test_bios_boots_and_prints_banner` (simulation, `toolchain` marker), `test_registers_read_back`.
- [ ] **Step 2:** Implement; pass. Commit.

### Task 7: Firmware

**Files:**
- Create: `firmware/main.c`, `firmware/runner.c`, `firmware/runner.h`, `firmware/demo_parse.c`, `firmware/demo_parse.h`, `firmware/hal.h`, `firmware/hal_litex.c`, `firmware/vga_mode.c`, `firmware/third_party/jsmn.h` (MIT, with its licence), `firmware/Makefile`, `tests/firmware/host_main.c`, `tests/test_firmware_host.py`, `tests/test_firmware_sim.py`

**Interfaces:**
- Consumes: `docs/demo-runner.md`, the vectors and traces of Task 1, the CSR map of Task 6, `vga_modes.h` of Task 3.
- Produces: `int demo_parse(const char *text, size_t len, const char *shuttle, struct demo *out, struct errors *errs)` with messages identical to Task 1's validator; `void runner_init(struct runner *, const struct demo *, const struct hal *)`, `void runner_tick(struct runner *, uint32_t now_ms)`; `struct hal` of function pointers (`set_ena`, `set_clock`, `set_rst_n`, `set_ui_in`, `set_gamepad`, `log`), which is the whole boundary between logic and hardware.
- Console lines (UART, one per event, `key=value` fields, documented in `docs/registers.md`): `boot`, `demo ok|error`, `clock`, `mode`, `state`, `error`.
- On an invalid `demo.json`: every message printed, the project never enabled, the seven-segment error pattern set.

- [ ] **Step 1:** Host build of `runner.c` and `demo_parse.c` with the system C compiler behind a test `hal` that prints events. Failing tests: `test_firmware_rejects_every_invalid_vector` (same messages as the vector files), `test_firmware_accepts_every_valid_vector`, `test_firmware_reproduces_every_trace` (event for event against `model.run`), `test_runner_never_resets_with_empty_steps`, `test_duration_timer_not_armed_for_one_project`.
- [ ] **Step 2:** Implement; pass. No dynamic allocation; sizes are compile-time limits with a loud error when exceeded.
- [ ] **Step 3:** `hal_litex.c`, `main.c` (boot, parse, classify the VGA mode from the measured counters once `locked`, program the capture, report), built for VexRiscv. Simulation tests (`toolchain`): `test_sim_boots_runs_dvd_screensaver_and_reports_mode`, `test_sim_invalid_demo_is_loud_and_project_stays_disabled`, `test_sim_gamepad_step_reaches_the_design` (the gamepad demo design changes its picture).
- [ ] **Step 4:** Commit.

### Task 8: Arty demo build

**Files:**
- Create: `tt_harness/boards/arty_demo.py`, `tt_harness/demo_build.py`, `tests/test_demo_build.py`
- Modify: `tt_harness/cli.py` (sub-command `build-demo`), `docs/toolchains.md`

**Interfaces:**
- Consumes: `HarnessSoC`; from `tt_harness/build.py` the public parsers and `TimingDomain`; `clock_build`'s decode and feedback gate; `input_gate`.
- Produces: `demo_build.build(project_id: str, demo_json: Path, board: str, toolchain: str, out_dir: Path) -> DemoBuildResult` with `bitstream`, `timing: dict[str, TimingDomain]`, `achieved_clock_hz: Fraction`, `feedback_source`, `input_drivers`, `limitations`.
- Clock domains and their treatment, the same in both flows: `sys` (50 MHz from the 100 MHz oscillator); `project` (`clock_hz` from the MMCM); the two are asynchronous. Every path between them goes through a synchroniser or the handshake of Task 5, and is constrained as a bounded datapath (`set_max_delay -datapath_only` of one period of the faster clock in Vivado; the equivalent budget in the open flow), never a blanket false path or clock group. The build fails on any cross-domain path that does not start or end at a named synchroniser register.
- CLI: `tt-harness build-demo PROJECT --demo demo.json --board arty --toolchain vivado|openxc7`.

- [ ] **Step 1:** Failing unit tests on saved report fragments: `test_cross_domain_path_outside_a_synchroniser_fails`, `test_unconstrained_domain_fails`, `test_clock_off_by_more_than_0_1_percent_fails`, `test_feedback_and_input_gates_are_run`.
- [ ] **Step 2:** Implement the top, constraints and both flows; a design with `clock_hz` 25175000 and one with 31500000 build with both toolchains (`toolchain` and `vivado` markers). Report resource use against the Arty A7-35T (block RAM in particular: the quarter-size framebuffer plus the CPU's memories).
- [ ] **Step 3:** Commit.

### Task 9: Host side: console, readback, at-speed verdict

**Files:**
- Create: `tt_harness/host/demo_console.py`, `tt_harness/host/readback.py`, `tests/test_demo_console.py`, `tests/test_readback.py`
- Modify: `firmware/main.c` (console commands), `tt_harness/cli.py` (sub-command `run-demo`), `tt_harness/host/board_host.py` if the receiver needs a second mode

**Interfaces:**
- Console commands (one line each, answered by one or more lines and a terminating `ok` or `error ...`): `atspeed N`, `mode`, `fb` (framebuffer as a framed binary block with length and CRC-32), `seg`, `state`, `press BUTTONS MS`.
- Produces: `run_demo(build: DemoBuildResult, board: Board, reference: Reference) -> DemoRunResult` with `atspeed: Verdict` (`exact` when the CRC equals the CRC of the first N reference bytes, `mismatch`, `inconclusive`, `nondeterministic` as in `tt_harness.verdict`), `mode`, `framebuffer: Path` (PNG) and `framebuffer_match` (the read-back picture against the same-size decimation of a reference frame; for a moving picture, the best match over the reference's frames and its pixel difference count).

- [ ] **Step 1:** Failing tests with a fake console: `test_atspeed_exact_and_mismatch`, `test_truncated_framebuffer_is_inconclusive`, `test_bad_crc_is_inconclusive`, `test_readback_matches_reference_frame`, `test_flagged_design_mismatch_is_nondeterministic`.
- [ ] **Step 2:** Implement; pass. Simulation test of the whole path (`toolchain`): firmware in the SoC simulation answers `atspeed` with the reference CRC for one pool design.
- [ ] **Step 3:** Commit.

### Task 10: Pool on hardware

**Files:**
- Create: `docs/results/2026-12-demo-build-arty.md`, `scripts/demo_pool_run.py`

- [ ] **Step 1:** A `demo.json` for each of the eight pool designs (under `tests/data/demo/pool/`), each valid, with the design's real `clock_hz`; `gamepad_pmod_demo` with gamepad steps.
- [ ] **Step 2:** Build sixteen demo bitstreams; every gate and every domain passes. Any failure is investigated to a root cause and reported, not worked around.
- [ ] **Step 3:** On the PS1 Arty: for each, the at-speed verdict (N = the reference's clock count), the measured mode, the framebuffer readback and its match, the console log. Expected: sixteen `exact`. Run the two designs with asynchronous resets twice. For `gamepad_pmod_demo`, show that a scripted press changes the picture.
- [ ] **Step 4:** Results document: tables, what is established and what is not (one board, room temperature; at-speed covers N clocks under fixed inputs; framebuffer is quarter size; audio and seven-segment are simulated only), and what remains for Game Bub.

### Task 11: Sampler for `vgacap/rtl/`

**Files (in a branch of `~/github/mithro/vgacap`, following the fork workflow):**
- Create: `rtl/uo_sampler.py` (Migen), `rtl/uo_sampler.v` (generated), `rtl/README.md`, `tests/test_rtl_uo_sampler.py`

- [ ] **Step 1:** The sampler as used here (`tt_harness/core/sampler.py`), self-contained, with its port list documented and a test that fails if the committed Verilog is stale.
- [ ] **Step 2:** Record in this repository which `vgacap` commit the copy here matches. Push the branch; opening the pull request waits for the owner's word.

---

## Left to the owner

- **Licence of the runner.** `demo.py` is LGPL-2.1 and this repository is Apache-2.0. This plan writes a specification and implements from it. If the owner would rather relicense `demo.py` (he is its author) or make the firmware LGPL, Task 1 and Task 7 get simpler.
- **System clock on the Arty.** 50 MHz as the spec says, from the 100 MHz oscillator, so the Arty and Game Bub builds run the same firmware timing.
- Whether a Game Bub can be made reachable (plan 3 needs it for anything beyond simulation).

## Not in this plan

- Game Bub display scan-out, I2S, overlay, core package, and the link's CPU-side mailboxes: plan 3.
- The all-shuttle sweep: plan 4.
- Partial reconfiguration: stages 2 and 3.
