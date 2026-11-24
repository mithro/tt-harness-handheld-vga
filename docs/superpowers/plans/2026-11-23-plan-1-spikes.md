# Plan 1: scaffold and the three spikes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the three things that could change the design: a compare build that records a Tiny Tapeout project's `uo_out` per clock and matches the reference, a fractional project clock that both toolchains accept, and a Game Bub MCU link that satisfies a model of the stock firmware.

**Architecture:** A Python package `tt_harness` of small Migen modules, each simulated on its own, plus board modules and a command-line build tool. The compare build is pure Migen with no CPU. System tests convert the Migen top to Verilog and run it with the real project in Verilator.

**Tech Stack:** Python 3.12 managed by `uv`; Migen and LiteX (with litex-boards); Verilator and Yosys from `.toolchains/oss-cad-suite`; openXC7 from `.toolchains/openxc7`; Vivado 2025.2 from `/opt/xilinx`; pytest; ruff.

**Spec:** `docs/superpowers/specs/2026-11-23-tt-harness-handheld-vga-design.md`. Read it and `docs/gamebub-hardware.md`, `docs/test-designs.md`, `docs/toolchains.md` before starting any task.

## Global Constraints

- Gateware is Migen/LiteX. No Chisel. Hand-written Verilog only for simulation harness glue.
- No code imported from sibling projects. Their data is read as files.
- Apache-2.0; every new source file starts with `# SPDX-License-Identifier: Apache-2.0`. Do not copy code from the Game Bub repositories (CERN-OHL-S, GPL).
- All Python through `uv` (`uv run`, `uv add`). Never bare `python` or `pip`.
- Never write under `/tmp`. Scratch goes in `<repo>/tmp/`, build output in `<repo>/build/`, fetched sources and results in `<repo>/work/`; all three are git-ignored.
- Never use `2>/dev/null`. Never pipe a command into `grep`/`tail`; redirect full output to a log file and read the log.
- Dates in ISO 8601 or day-first form.
- Each task is done in its own git worktree and branch named `task-<n>-<slug>`, with small commits, each message ending `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. No pushing, no merging; the coordinator merges after an independent review re-runs the task's verification.
- Builds use at most 16 cores each. No SSH to any host and no contact with any board in this plan.
- A build that leaves a clock domain unconstrained, or misses timing in any domain, is a failure.
- Tests that need `.toolchains/` or Vivado are marked `toolchain` or `vivado` and skip cleanly when the tool is absent; everything else must pass with only `uv sync`.
- Paths to sibling checkouts come from environment variables with these defaults: `TT_VIDEOS_REPO=~/github/TinyTapeout/tinytapeout-vga-videos`, `TT_REFERENCE_DIR=~/github/TinyTapeout/tt-harness-fpga-vga/work/reference`.

## Review Focus

1. A project whose sources fail to fetch or synthesise: the tool reports which project and which step, and exits non-zero; it never emits a bitstream for a different or empty slot. (Task 2, Task 5)
2. A recording that is short, has a bad checksum or bad magic: verdict `inconclusive`, never `mismatch` or `exact`. (Task 6)
3. A `clock_hz` that the MMCM cannot hit within 0.1%, including 0 and values above the part's limit: a clear error naming the requested and nearest achievable frequency. (Task 7)
4. A reference file whose `dump_sha256` does not match its dump, or whose `clocks` exceeds the dump length: refused with an error, not compared. (Task 6)
5. An MCU transaction the specification does not define (unknown register, unknown command, a read with CS released early): the link stays responsive to the next valid transaction and reports the unknown command as an error status, not a hang. (Task 8)

---

## File structure

```
pyproject.toml                      package metadata, dependencies, ruff, pytest markers
tt_harness/__init__.py
tt_harness/manifest.py              Task 2: read the shared manifest
tt_harness/fetch.py                 Task 2: fetch a project's sources at its commit
tt_harness/core/sampler.py          Task 3: UoSampler
tt_harness/core/stimulus.py         Task 3: ReferenceStimulus
tt_harness/compare/recorder.py      Task 3: StepRecorder (stepping clock, UART framing)
tt_harness/compare/top.py           Task 5: CompareTop (slot + stimulus + recorder)
tt_harness/reference.py             Task 4: local Verilator reference dump
tt_harness/sim/                     Tasks 4, 5: C++ Verilator harnesses
tt_harness/boards/arty.py           Task 5: Arty pins and clocks for the compare build
tt_harness/build.py                 Task 5: toolchain drivers, timing gate
tt_harness/cli.py                   Tasks 2, 4, 5, 6: `tt-harness` command
tt_harness/host/frame.py            Task 6: UART frame decode, dump and vgacap stream writers
tt_harness/verdict.py               Task 6: reference reader and verdicts
tt_harness/prescreen.py             Task 6: latch and loop pre-screen
tt_harness/core/clocking.py         Task 7: MMCM solver and ProjectClock
tt_harness/boards/gamebub/platform.py   Task 7: Game Bub LiteX platform
tt_harness/boards/gamebub/qspi.py   Task 8: QSPI slave in the SPI clock domain
tt_harness/boards/gamebub/link.py   Task 8: registers, mailboxes, handshakes
docs/gamebub-mcu-protocol.md        Task 8: the register specification
docs/results/                       Tasks 7, 9: written results
tests/                              one test file per module, same name
```

---

### Task 1: Package scaffold

**Files:**
- Modify: `pyproject.toml`, `README.md`, `.gitignore`
- Create: `tt_harness/__init__.py`, `tt_harness/cli.py`, `tests/test_cli.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: importable package `tt_harness` with `__version__`; console script `tt-harness` (argparse, sub-commands added by later tasks via `add_subparsers`); pytest markers `toolchain` and `vivado`; helper `tt_harness.tools.find(name: str) -> Path | None` that locates a tool under `.toolchains/` or on `PATH`, and `tt_harness.tools.vivado_settings() -> Path | None` returning `/opt/xilinx/Vivado/2025.2/settings64.sh` if present.

- [ ] **Step 1:** Add dependencies with `uv add`: `migen`, `litex` and `litex-boards` from their Git repositories pinned to the newest LiteX release tag (record the tag in `docs/toolchains.md`), plus `pyyaml`; dev group `pytest`, `ruff`. Add `work/` to `.gitignore`.
- [ ] **Step 2:** Write `tests/test_cli.py`: `tt-harness --version` prints `tt_harness.__version__` and exits 0; `tt-harness` with no arguments exits 2 and prints usage; `tools.find("definitely-not-a-tool")` is `None`.
- [ ] **Step 3:** Run `uv run pytest tests/test_cli.py -v`; expect failures (no package).
- [ ] **Step 4:** Implement the package, CLI skeleton, `tools.py` and marker registration. Keep the existing `tests/toolchain/` smoke test working under the `toolchain` marker.
- [ ] **Step 5:** CI workflow: on push and pull request, `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -m "not toolchain and not vivado"`.
- [ ] **Step 6:** Run `uv run ruff check . && uv run pytest -m "not vivado" -q`; expect all pass (toolchain tests run if `.toolchains/` is linked into the worktree, otherwise skip).
- [ ] **Step 7:** Commit.

Note for every later task: a worktree has no `.toolchains/`. Create a symlink `.toolchains -> /home/tim/github/TinyTapeout/tt-harness-handheld-vga/.toolchains` in the worktree (it is git-ignored) rather than reinstalling.

---

### Task 2: Manifest and source fetch

**Files:**
- Create: `tt_harness/manifest.py`, `tt_harness/fetch.py`, `tests/test_manifest.py`, `tests/test_fetch.py`, `tests/data/manifest/` (a three-project fixture cut from the real files)
- Modify: `tt_harness/cli.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Project`: `shuttle: str`, `macro: str`, `repo: str`, `commit: str`, `top_module: str`, `source_files: tuple[str, ...]`, `clock_hz: int`, `pmods: tuple[str, ...]`, `address: int | None`, `ui_in: int`, `uio_in: int`, `patch: str | None`; property `id -> str` returning `"<shuttle>/<macro>"`.
  - `manifest.load(videos_repo: Path) -> dict[str, Project]` keyed by `id`.
  - `manifest.by_address(projects, shuttle: str, address: int) -> Project`.
  - `@dataclass(frozen=True) class Sources`: `project: Project`, `root: Path`, `files: tuple[Path, ...]` (absolute, in manifest order), `needs_sv: bool`.
  - `fetch.fetch(project: Project, cache: Path) -> Sources`; cache layout `cache/<shuttle>/<macro>/<commit>/`.
  - CLI: `tt-harness fetch <shuttle>/<macro>` prints the source root.

Read `data/targets.json` (key `targets`), the `override` entry of `data/results/<shuttle>/<macro>/result.json`, and `overrides/<shuttle>/<macro>.yaml`, interpreting overrides as `ttvga/overrides.py` in the videos repository does (read it; do not import it). The fixed `ui_in`/`uio_in` is the first input event's value, default 0.

- [ ] **Step 1:** Tests in `tests/test_manifest.py` against the fixture: the three fixture projects load with the exact repo, commit, top module and file list from the fixture; a project with an override takes the override's sources and top module; `tt06/tt_um_toivoh_retro_console` (include it in the fixture) has `ui_in == 0x30`; `by_address` finds a project by integer address although the file stores a string; an unknown id raises `KeyError` naming the id; a missing videos repository raises `FileNotFoundError` saying to set `TT_VIDEOS_REPO`.
- [ ] **Step 2:** Tests in `tests/test_fetch.py` using a local Git repository built in `tmp_path` as the "remote": fetch checks out exactly the pinned commit; a second fetch is a cache hit and runs no Git network command; a patch from the override is applied; a commit that does not exist raises `fetch.FetchError` naming project and commit; a manifest source file missing from the checkout raises `FetchError` naming the file; `needs_sv` is true when any file ends `.sv`; repository URLs and commits are validated (hex commit, `https://` or local path) and passed to Git after `--`.
- [ ] **Step 3:** Run both test files; expect failures.
- [ ] **Step 4:** Implement. One marked-`toolchain`-free integration test, skipped unless `TT_VIDEOS_REPO` exists: the real manifest loads at least 400 projects and contains the eight pool ids listed in `docs/test-designs.md`.
- [ ] **Step 5:** Run `uv run pytest tests/test_manifest.py tests/test_fetch.py -v`; expect pass.
- [ ] **Step 6:** Commit.

---

### Task 3: Sampler, stimulus and stepping recorder (spike A, gateware)

**Files:**
- Create: `tt_harness/core/sampler.py`, `tt_harness/core/stimulus.py`, `tt_harness/compare/recorder.py`, `tests/test_sampler.py`, `tests/test_stimulus.py`, `tests/test_recorder.py`

**Interfaces:**
- Produces (all Migen `Module`s, no vendor primitives):
  - `UoSampler(width=8)`: inputs `uo: Signal(8)`, `strobe: Signal()`; outputs `data: Signal(8)`, `valid: Signal()`, `count: Signal(32)` (number of samples taken). On `strobe` it registers `uo`, asserts `valid` for one cycle and increments `count`.
  - `ReferenceStimulus(ui_in: int, uio_in: int, reset_clocks: int = 10)`: input `clock_index: Signal(32)`; outputs `ena` (constant 1), `rst_n` (0 while `clock_index < reset_clocks`), `ui_in: Signal(8)`, `uio_in: Signal(8)` (constants).
  - `StepRecorder(sys_clk_freq: int, baud: int, clocks: int, settle: int = 2)`: outputs `pclk: Signal()` (the project clock, a register in the system domain), `clock_index: Signal(32)`, `sample_strobe: Signal()`, `tx: Signal()` (UART, 8N1, idle high), `done: Signal()`; input `uo: Signal(8)`. Behaviour: after reset it sends the header, then for each of `clocks` project clocks it waits until the UART can accept a byte, raises `pclk`, waits `settle` system cycles, strobes the sampler, queues the byte, and lowers `pclk` for at least as long as it was high. After the last byte it sends the trailer and asserts `done`.
  - **Frame format** (also used by Task 6): magic `b"TTHC"`, `u8` version = 1, `u32le` `clocks`, then `clocks` bytes, then `u32le` CRC-32 (IEEE, as `zlib.crc32`) of those bytes.
  - `recorder.frame_length(clocks: int) -> int` returning `13 + clocks`.

Byte *i* is `uo` after the rising edge of project clock *i*; clock 0 is the first clock with `rst_n` low (the reference's definition).

- [ ] **Step 1:** `tests/test_sampler.py` (Migen simulator): with `uo` driven from a counter, strobing every third cycle yields exactly the strobed values in order; `count` equals the number of strobes; no `valid` without `strobe`.
- [ ] **Step 2:** `tests/test_stimulus.py`: `rst_n` is 0 for `clock_index` 0..9 and 1 from 10; `ui_in`/`uio_in` equal the constructor values; `reset_clocks=0` never asserts reset.
- [ ] **Step 3:** `tests/test_recorder.py`: connect a behavioural "project" (a Migen module clocked by `pclk`'s rising edge that outputs a free-running 8-bit counter, reset by `rst_n`) through `ReferenceStimulus` and `UoSampler`; decode `tx` with a bit-level UART receiver written in the test. Assert: the frame equals magic, version 1, `clocks`, the expected counter bytes (0 for the ten reset clocks, then 1, 2, ...), and the correct CRC; `pclk` never has a high or low phase shorter than `settle` cycles; `pclk` rises exactly `clocks` times; with `baud` chosen so the UART is the bottleneck no byte is lost or duplicated (compare against the expected sequence for `clocks=300`); `done` rises after the last stop bit.
- [ ] **Step 4:** Run the three files; expect failures.
- [ ] **Step 5:** Implement the three modules.
- [ ] **Step 6:** Run `uv run pytest tests/test_sampler.py tests/test_stimulus.py tests/test_recorder.py -v`; expect pass.
- [ ] **Step 7:** Commit.

---

### Task 4: Local reference dump

**Files:**
- Create: `tt_harness/reference.py`, `tt_harness/sim/reference_main.cpp`, `tests/test_reference.py`, `tests/data/projects/counter/` (a tiny `tt_um_test_counter` project in Verilog for tests)
- Modify: `tt_harness/cli.py`

**Interfaces:**
- Consumes: `fetch.Sources`, `tools.find("verilator")`.
- Produces:
  - `reference.simulate(sources: Sources, clocks: int, out_dir: Path, reset_clocks: int = 10) -> Path` returning the path of `uo_out.bin` (uncompressed, one byte per clock). Verilator flags match the videos pipeline where they affect results: `--x-initial fast`, and `-sv` handling when `sources.needs_sv`.
  - `reference.ReferenceError` for build or run failures, carrying the log path.
  - CLI: `tt-harness reference <shuttle>/<macro> --clocks N`.

- [ ] **Step 1:** `tests/test_reference.py` (marked `toolchain`): for the test counter project, the dump has exactly `clocks` bytes, the first ten are the reset value and the rest count up; a project with a syntax error raises `ReferenceError` whose log exists and names the file; running twice gives byte-identical output.
- [ ] **Step 2:** Agreement test, skipped unless `TT_REFERENCE_DIR` and `TT_VIDEOS_REPO` exist: for `tt08/tt_um_tinytapeout_logo_screensaver` the first 1,000,000 bytes of the local dump equal the first 1,000,000 bytes of the sibling project's `uo_out.bin.gz`.
- [ ] **Step 3:** Run; expect failures.
- [ ] **Step 4:** Implement: generate a Verilator build in `out_dir`, drive `clk`, `rst_n`, `ena`, `ui_in`, `uio_in` exactly as the stimulus in the spec, write `uo_out` after each rising edge.
- [ ] **Step 5:** Run `uv run pytest tests/test_reference.py -v`; expect pass, including the agreement test on this machine. If the agreement test fails, stop and report the first differing clock; do not adjust the stimulus to force a match without understanding why.
- [ ] **Step 6:** Commit.

---

### Task 5: Compare build for Arty with both toolchains (spike A, system)

**Files:**
- Create: `tt_harness/compare/top.py`, `tt_harness/boards/arty.py`, `tt_harness/build.py`, `tt_harness/sim/uart_capture_main.cpp`, `tests/test_compare_top.py`, `tests/test_build.py`
- Modify: `tt_harness/cli.py`

**Interfaces:**
- Consumes: Task 2 `Sources`, Task 3 modules and `frame_length`, Task 4 `reference.simulate`.
- Produces:
  - `CompareTop(project: Project, sys_clk_freq: int, baud: int, clocks: int)`: a Migen module with ports `clk`, `rst`, `tx`, `done`, instantiating the project's top module by name as a black box clocked by `pclk`.
  - `compare.top.to_verilog(top: CompareTop, path: Path) -> Path`.
  - `build.build_compare(project: Project, sources: Sources, board: str, toolchain: str, out_dir: Path, clocks: int, baud: int = 1_000_000, jobs: int = 16) -> BuildResult`.
  - `@dataclass class BuildResult`: `bitstream: Path`, `toolchain: str`, `part: str`, `timing: dict[str, TimingDomain]`, `log: Path`; `TimingDomain` has `constraint_mhz: float`, `achieved_mhz: float`, `met: bool`.
  - `build.BuildError` (synthesis, fit) and `build.TimingError` (a domain missed or was unconstrained), both carrying the log path.
  - CLI: `tt-harness build-compare <shuttle>/<macro> --board arty --toolchain vivado|openxc7 [--clocks N]`, writing to `build/compare/<board>-<toolchain>/<shuttle>/<macro>/`.

Arty: part `xc7a35ticsg324-1L`, 100 MHz oscillator on E3, UART TX on D10, `done` on LED H5 (verify against litex-boards' `digilent_arty` platform, which may be used for the pin table). The system clock is the 100 MHz input; `pclk` goes through a `BUFG`; constrain it as a generated clock with period `2 * (settle + 1)` system cycles. For openXC7 pass per-domain frequencies the way `constrain_openxc7_clocks` does in `/home/tim/github/fpgas-online/fpgas.online-test-designs` (read it; reimplement, do not import).

- [ ] **Step 1:** `tests/test_compare_top.py` (marked `toolchain`): convert `CompareTop` for the test counter project to Verilog, build it with Verilator together with the project and `uart_capture_main.cpp` (which runs until `done` and writes the bytes seen on `tx`), with `baud = sys_clk_freq // 4` for speed. Assert the captured frame's payload equals `reference.simulate` for the same project and `clocks=5000`, byte for byte, and its CRC is right.
- [ ] **Step 2:** The same test for `tt08/tt_um_tinytapeout_logo_screensaver` with `clocks=450_000` (just over one frame), skipped unless `TT_VIDEOS_REPO` exists; and for `ttsky26a/tt_um_algofoogle_dottee` (SystemVerilog syntax, second clock derived from vsync).
- [ ] **Step 3:** `tests/test_build.py`: unit tests for the timing-report parsers using saved report excerpts for both toolchains (include a multi-clock nextpnr report where the clock-name column is padded); a report with a failing domain makes `build_compare` raise `TimingError`; a report missing a domain present in the design raises `TimingError` ("unconstrained").
- [ ] **Step 4:** Run; expect failures.
- [ ] **Step 5:** Implement `CompareTop`, the Arty module, `build.py` and the CLI.
- [ ] **Step 6:** Run `uv run pytest tests/test_compare_top.py tests/test_build.py -v`; expect pass.
- [ ] **Step 7:** Build for real, logging to `tmp/`: `tt-harness build-compare tt08/tt_um_tinytapeout_logo_screensaver --board arty --toolchain openxc7 --clocks 6000000` and the same with `--toolchain vivado`. Expect a `.bit` from each, every domain met. Add both as tests marked `toolchain` and `vivado` respectively that assert a bitstream exists and `all(d.met for d in result.timing.values())`.
- [ ] **Step 8:** Commit. In the commit body give the achieved frequency per domain for both toolchains.

---

### Task 6: Host frame decode, verdicts and pre-screen

**Files:**
- Create: `tt_harness/host/frame.py`, `tt_harness/verdict.py`, `tt_harness/prescreen.py`, `tests/test_frame.py`, `tests/test_verdict.py`, `tests/test_prescreen.py`
- Modify: `tt_harness/cli.py`

**Interfaces:**
- Consumes: the Task 3 frame format; `tools.find("yosys")`.
- Produces:
  - `frame.decode(data: bytes) -> Recording`; `@dataclass class Recording`: `clocks: int`, `samples: bytes`, `ok: bool`, `problem: str | None` (`"bad magic"`, `"bad version"`, `"short"`, `"bad crc"`). Leading garbage before the magic is skipped.
  - `frame.write_dump(rec: Recording, path: Path)` (raw bytes, gzip when the name ends `.gz`).
  - `frame.write_vgacap(rec: Recording, path: Path, clock_hz: int, desc: str)`: a vgacap stream with a `VGCH` header (version 1, 8 sample bits, external-clock mode, Tiny VGA signal map `(7, 3, 0, 4, 1, 5, 2, 6)`, `samples_per_word=4`, first-sample-MSB flag) and `RAW ` chunks. Reimplement from `include/vgacap/stream.h` in the vgacap checkout; do not import vgacap.
  - `verdict.load_reference(ref_dir: Path, project_id: str) -> Reference`; `Reference`: `clocks: int`, `reset_clocks: int`, `ui_in: int`, `uio_in: int`, `clock_hz: int`, `samples: bytes`. Verifies `dump_sha256` against the uncompressed dump and that `clocks <= len(samples)`; raises `verdict.ReferenceInvalid` otherwise.
  - `verdict.compare(rec: Recording, ref_samples: bytes, flagged: bool) -> Verdict`; `Verdict`: `kind: Literal["exact", "mismatch", "inconclusive", "nondeterministic"]`, `first_diff_clock: int | None`, `detail: str`. Rules: recording not `ok` or shorter than the reference span gives `inconclusive`; equal gives `exact`; different gives `nondeterministic` when `flagged`, else `mismatch`, with the first differing clock.
  - `prescreen.check(sources: Sources) -> Prescreen`; `Prescreen`: `latches: int`, `comb_loops: int`, `derived_clocks: int`, `flagged: bool` (any non-zero), `log: Path`.
  - CLI: `tt-harness verdict <shuttle>/<macro> <recording-file>` printing one JSON object.

- [ ] **Step 1:** `tests/test_frame.py`: a frame built by hand round-trips; each of bad magic, bad version, truncated payload, truncated trailer and wrong CRC yields `ok == False` with the matching `problem`; 37 bytes of noise before the magic are skipped; `write_vgacap` output parsed by a minimal reader written in the test yields the same samples, and, when the vgacap checkout is present at `~/github/TinyTapeout/vgacap`, is read back identically by that checkout's `python/vgacap/stream.py` loaded by path (skipped otherwise).
- [ ] **Step 2:** `tests/test_verdict.py`: equal gives `exact`; one differing byte at index 12345 gives `mismatch` with `first_diff_clock == 12345`; the same with `flagged=True` gives `nondeterministic`; a not-`ok` recording gives `inconclusive` even when the bytes received so far match; a recording shorter than the reference gives `inconclusive`; a recording longer than the reference is compared over the reference's span only; a reference whose SHA-256 is wrong raises `ReferenceInvalid`; `clocks` larger than the dump raises `ReferenceInvalid`.
- [ ] **Step 3:** `tests/test_prescreen.py` (marked `toolchain`): the test counter is not flagged; a fixture with an inferred latch reports `latches >= 1`; a fixture with a ring of inverters reports `comb_loops >= 1`; a fixture clocking a flop from another flop's output reports `derived_clocks >= 1`.
- [ ] **Step 4:** Run; expect failures. Implement. Run `uv run pytest tests/test_frame.py tests/test_verdict.py tests/test_prescreen.py -v`; expect pass.
- [ ] **Step 5:** Commit.

---

### Task 7: Project clock under both toolchains (spike B)

**Files:**
- Create: `tt_harness/core/clocking.py`, `tt_harness/boards/gamebub/__init__.py`, `tt_harness/boards/gamebub/platform.py`, `tests/test_clocking.py`, `tests/test_clock_build.py`, `docs/results/2026-11-spike-b-project-clock.md`

**Interfaces:**
- Consumes: Task 5 `build.py` timing gate (reuse its parsers; if Task 5 is not merged yet, build against its branch).
- Produces:
  - `clocking.solve(clk_in_hz: int, clock_hz: int, tolerance: float = 0.001) -> MmcmSetting`; `MmcmSetting`: `divclk: int`, `mult: float` (fractional, 0.125 steps), `div: float` (output 0 divider, fractional, 0.125 steps), `vco_hz: float`, `achieved_hz: float`, `error_ppm: float`. Respects the Artix-7 speed grade -1 VCO range 600 to 1200 MHz and PFD limits. Raises `clocking.ClockError` naming the requested and nearest achievable frequency when outside tolerance or when `clock_hz <= 0`.
  - `ProjectClock(platform, clk_in, clk_in_hz: int, clock_hz: int)`: a Migen module instantiating `MMCME2_ADV` and `BUFGCE`; inputs `enable: Signal()`; outputs `clk: Signal()`, `locked: Signal()`; attribute `setting: MmcmSetting`. `enable` is synchronised into the output domain before reaching the buffer's CE.
  - `boards.gamebub.platform.Platform`: a LiteX `XilinxPlatform` for `xc7a100tcsg324-1` with the pins in `docs/gamebub-hardware.md` (clock, LCD, buttons, DAC, MCU QSPI, PMOD, test points), all LVCMOS33; toolchain selectable `vivado` or `openxc7`.

- [ ] **Step 1:** `tests/test_clocking.py`: from 50 MHz, 25_175_000 solves with `error_ppm < 100` and is not the integer-only 25.000 MHz answer; 31_500_000 solves exactly; 25_000_000, 50_000_000 and 100_000_000 solve exactly; from 100 MHz the same five solve; 0 raises `ClockError`; 3_000_000_000 raises `ClockError`; for every distinct non-zero `clock_hz` in the real manifest (skipped unless `TT_VIDEOS_REPO` exists) either a setting within 0.1% exists or the test lists the failures in its assertion message, and the count of failures is asserted to be 0 unless documented in the results file.
- [ ] **Step 2:** Run; expect failures. Implement `solve` and `ProjectClock`.
- [ ] **Step 3:** `tests/test_clock_build.py`: a minimal top (input clock, `ProjectClock` at 25.175 MHz with `enable` tied to a button, a 24-bit counter in the project domain driving an output pin) built for Arty (`xc7a35ticsg324-1L`, 100 MHz in) and Game Bub (`xc7a100tcsg324-1`, 50 MHz in) with openXC7 (marked `toolchain`) and Vivado (marked `vivado`): a bitstream exists and both domains are constrained and met.
- [ ] **Step 4:** Run the four builds, logs under `tmp/`. If nextpnr-xilinx rejects the fractional MMCM or the `BUFGCE`, do not work around it silently: record the exact error, then try, in order, (a) integer-only settings within tolerance where they exist, (b) `PLLE2_ADV`, and record what each gives.
- [ ] **Step 5:** Write `docs/results/2026-11-spike-b-project-clock.md`: per board and toolchain, the setting, achieved frequency, timing per domain, and any primitive the open flow refused, with log excerpts. State plainly whether the spec's clock design stands or needs changing.
- [ ] **Step 6:** Commit.

---

### Task 8: Game Bub MCU protocol specification and link (spike C)

**Files:**
- Create: `docs/gamebub-mcu-protocol.md`, `tt_harness/boards/gamebub/qspi.py`, `tt_harness/boards/gamebub/link.py`, `tests/gamebub/firmware_model.py`, `tests/gamebub/test_qspi.py`, `tests/gamebub/test_link.py`

**Interfaces:**
- Produces:
  - `docs/gamebub-mcu-protocol.md`: written from the firmware source at `elipsitz/gamebub` branch `v1.1-beta` commit 8b1135b (a clone is at `/home/tim/.claude-tmp/claude-1000/-home-tim-github-TinyTapeout/475d1ac4-2709-4456-b4b3-357c35af80e1/scratchpad/gamebub`, reachable as `FETCH_HEAD`; re-clone into `work/` if it is gone). It must be sufficient to implement from alone: QSPI framing with a timing diagram; every address the firmware reads or writes with direction, width, reset value and meaning (including input capabilities, interrupt enable and pending, command status, button register and its bit order, overlay and framebuffer regions); mailbox layouts; every command the firmware sends and expects, with the sequence from end of configuration to "core running", timeouts, and what the firmware does on each failure. Each fact cites file and line. Describe the interface; do not copy source text beyond short identifiers.
  - `QspiSlave()` (Migen, clocked by the SPI clock in clock domain `spi`): pads `clk`, `cs_n`, `dq_i[4]`, `dq_o[4]`, `dq_oe`; bus master interface `adr: Signal(32)`, `dat_w: Signal(8)`, `we: Signal()`, `dat_r: Signal(8)`, `re: Signal()` with byte-granular auto-increment as the command byte selects. No oversampling from the system clock.
  - `McuLink(qspi: QspiSlave)`: implements the `0xF1` registers and the two mailboxes, answers the setup, run, halt and focus handshakes in RTL, exposes `buttons: Signal(12)` (input), `irq: Signal()` (output, active high; the board inverts it through an n-FET. Corrected after review: the plan first said `irq_n`), `running: Signal()`, and a system-domain port for a CPU to read commands the RTL does not handle. Unknown commands get an error status.
  - `tests/gamebub/firmware_model.py`: `FirmwareModel(dut)` with coroutines `write(addr, data: bytes, word_size)`, `read(addr, n, word_size) -> bytes` generating bit-accurate QSPI waveforms at a chosen SPI clock, and `boot() -> None` performing the firmware's documented sequence and raising `FirmwareTimeout`/`FirmwareError` exactly where the real firmware would give up.

- [ ] **Step 1:** Write the specification. Have it checked before coding: list at the end every firmware behaviour you could not determine, under "Unverified".
- [ ] **Step 2:** `tests/gamebub/test_qspi.py` (Migen simulator with separate `spi` and `sys` domains): single write and read at each word size; auto-increment burst of 16 KiB; byte-swap bit; a read returns data after exactly the documented dummy cycles; CS released mid-address leaves the slave ready for the next transaction; back-to-back transactions with one SPI clock of CS high.
- [ ] **Step 3:** `tests/gamebub/test_link.py`: `FirmwareModel.boot()` completes against `McuLink` with the system-domain side idle (no CPU), at SPI write clock 40 MHz and read clock 10 MHz (the firmware's actual read rate; 16 MHz is only its cap) and also at 16 MHz, within the documented timeouts scaled to simulation; the button register returns `buttons` in the documented bit order for each of the twelve buttons individually; an unknown command returns the documented error status and the next valid command still succeeds; a write to an undefined `0xF1` register is ignored and a read returns 0; `boot()` against a link held in reset raises `FirmwareTimeout`.
- [ ] **Step 4:** Run; expect failures. Implement `QspiSlave` and `McuLink`.
- [ ] **Step 5:** Run `uv run pytest tests/gamebub -v`; expect pass.
- [ ] **Step 6:** Synthesis check (marked `toolchain` and `vivado`): a top with `QspiSlave` and `McuLink` on the Game Bub platform pins builds with both toolchains with the `spi` domain constrained at 40 MHz and met. Needs Task 7's platform; if it is not merged, build against its branch.
- [ ] **Step 7:** Commit. The commit body lists the "Unverified" items.

---

### Task 9: Pool run in simulation and results

**Files:**
- Create: `tt_harness/pool.py`, `tests/test_pool.py`, `docs/results/2026-11-spike-a-compare-build.md`
- Modify: `tt_harness/cli.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5, 6.
- Produces: `pool.POOL: tuple[str, ...]` (the eight ids from `docs/test-designs.md`); `pool.run_sim(project_id: str, clocks: int, work: Path) -> PoolResult` which fetches, pre-screens, simulates the compare top in Verilator, decodes the frame and returns the verdict against the shared reference when present, else the local one; CLI `tt-harness pool-sim [--clocks N] [--jobs J]` writing `work/pool/<id>/result.json` and a summary table.

- [ ] **Step 1:** `tests/test_pool.py`: with the test counter project registered as a one-entry pool, `run_sim` returns `exact`; with a deliberately corrupted reference it returns `mismatch` with the right clock; results are written as JSON with the fields `id`, `verdict`, `first_diff_clock`, `prescreen`, `reference_source` (`"shared"` or `"local"`), `clocks`.
- [ ] **Step 2:** Run; expect failures. Implement.
- [ ] **Step 3:** Run `tt-harness pool-sim --clocks 1300000 --jobs 8` (about three frames per design), log to `tmp/pool-sim.log`. Expected: eight `exact`. Any other verdict is investigated to a root cause before the task is reported done; do not relax the comparison.
- [ ] **Step 4:** Build compare bitstreams for all eight with both toolchains (`--clocks 6000000`), at most two builds at a time; record fit and timing per domain.
- [ ] **Step 5:** Write `docs/results/2026-11-spike-a-compare-build.md`: a table per design with verdict, first differing clock if any, pre-screen flags, and for each toolchain LUT/FF use and achieved frequency per domain; then what was and was not shown (nothing ran on a board). No claim without the command and log that supports it.
- [ ] **Step 6:** Commit.

---

## Order and parallelism

- Task 1 first.
- Then Tasks 2, 3, 7 and 8 are independent of each other.
- Task 4 needs 2. Task 6 needs 3 (frame format) and 2 (`Sources`).
- Task 5 needs 2, 3 and 4.
- Task 9 needs 2, 4, 5 and 6.

At most three implementers at once.

## Not in this plan

The SoC and firmware, VGA capture and framebuffer, gamepad emulator, 7-segment, audio, the Game Bub display, overlay and core package, any run on a board, and the sweep. Those are plan 2, written after the spikes report.

---

### Task 10: First hardware run of the compare build (added 2026-11-24)

Added after the plan was written, when the owner granted use of any fpgas.online device and
a live Arty A7-35T was found at the PS1 site (`docs/arty-boards.md`). It overrides the
"no SSH to any host and no contact with any board" global constraint for this task only.

**Files:**
- Create: `tt_harness/host/run.py`, `tests/test_host_run.py`, `docs/results/2026-11-first-hardware-run.md`
- Modify: `tt_harness/cli.py`, `docs/arty-boards.md`

**Interfaces:**
- Consumes: `build.build_compare` and its `BuildResult` (bitstream path), `host.frame.decode`, `verdict.load_reference` / `verdict.compare`, `prescreen.check`, `compare.recorder.frame_length`.
- Produces:
  - `run.Board`: `host: str`, `port: int`, `user: str = "pi"`, `uart: str` (a `/dev/serial/by-id/...-if01-port0` path), `loader_args: tuple[str, ...] = ("-b", "arty")`.
  - `run.board_activity(board) -> Activity` with `busy: bool` and `reasons: tuple[str, ...]`: busy when another session shows terminal activity within the last 10 minutes, or any process holds the UART or runs `openFPGALoader`, `picocom`, `tio`, `minicom`. A login alone is not use.
  - `run.record(board, bitstream: Path, clocks: int, baud: int, out: Path, timeout_s: float) -> Path`: refuses when `board_activity` is busy; copies the bitstream to the board host; starts a receiver on the board host that opens the UART at `baud` and reads until `frame_length(clocks)` bytes after the frame magic or the timeout; only then loads the bitstream; fetches the raw bytes to `out`; removes its files from the board host; returns `out`. The receiver must be reading before the load so the start of the frame is not lost.
  - CLI: `tt-harness run-compare <shuttle>/<macro> --host H --port P --uart PATH [--toolchain vivado|openxc7] [--clocks N]`: build (or reuse an existing build), record, decode, verdict; prints the verdict JSON; exit codes as `tt-harness verdict`.

- [ ] **Step 1:** Unit tests with a fake command runner (no network): the receiver is started before the loader and the loader only after the receiver reports it is listening; a busy board is refused with the reasons; remote files are removed on success and on failure; a receiver timeout yields a short recording that the verdict reports as `inconclusive`; the remote commands contain no shell injection from the bitstream name or UART path (reject unexpected characters).
- [ ] **Step 2:** Run them; expect failures. Implement.
- [ ] **Step 3:** On the board `ssh -p 10722 pi@ps1.fpgas.online` (Arty A7-35T, UART `/dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0`): check activity; run `tt08/tt_um_tinytapeout_logo_screensaver` built with Vivado, `--clocks 6000000`, 1,000,000 baud. Expected: verdict `exact` against the shared reference. Then the same design built with openXC7. Then `ttsky26a/tt_um_algofoogle_dottee` with both.
- [ ] **Step 4:** Any verdict other than `exact` is investigated to a root cause (first differing clock, what the bytes show, whether the UART dropped data, whether the baud rate is achievable by the board's USB-serial bridge) before the task is reported done. Do not relax the comparison. If 1,000,000 baud is unreliable, find the highest reliable standard rate and say so.
- [ ] **Step 5:** Leave the board as found: no files left, no processes left; the last bitstream loaded stays (there is no "previous" state to restore), and that is recorded.
- [ ] **Step 6:** Write `docs/results/2026-11-first-hardware-run.md`: board, date, commands, per run the build toolchain, bytes received, time taken, verdict and first differing clock if any; what was and was not shown. Update `docs/arty-boards.md` with anything learned about using the PS1 boards.
- [ ] **Step 7:** Commit.

---

### Task 7c: Measure the project clock on a real Arty (added 2026-11-24)

Added after Task 7b found that the open flow programs the MMCM differently from Vivado
(`docs/results/2026-11-spike-b-project-clock.md`): for a fractional CLKOUT0 divider with an odd
integer part the divider fields differ, and nextpnr-xilinx 0.8.2 hard-codes the lock/filter
table (right only for a multiplier of 10, 17 or 18). Whether the open-flow clock has the right
frequency, and whether the MMCM locks, can only be settled on hardware. This task may SSH to and
use the PS1 Arty, like Task 10.

**Files:**
- Create: `tt_harness/core/freq_meter.py`, `tt_harness/boards/arty_clock_probe.py`, `tests/test_freq_meter.py`, `tests/test_clock_probe.py`, `scripts/clock_probe_run.py`
- Modify: `docs/results/2026-11-spike-b-project-clock.md`, `tt_harness/clock_build.py` only if needed

**Interfaces:**
- Produces:
  - `FreqMeter(gate_cycles: int)` (Migen): counts rising edges of an asynchronous input clock `clk_meas` during a gate of `gate_cycles` reference (system) clock cycles, using a counter in the measured domain sampled across with a safe crossing (Gray code or a toggle-and-hold handshake); outputs `count: Signal(32)` and `valid` (one pulse per gate) in the system domain. Also `HighTimeMeter`-style duty measurement is optional (see Step 4).
  - A probe top for the Arty (100 MHz oscillator): one `ProjectClock`-equivalent MMCM with parameters given at build time (`divclk`, `mult`, `div`, so settings other than the solver's can be built), the free-running BUFG output measured by `FreqMeter` with a 1 s gate (100,000,000 system cycles), and a UART line printed once per gate at 115200 baud: `F <count> L <locked> U <lock-losses>\n` where `locked` is the MMCM's LOCKED sampled in the system domain and `lock-losses` counts falling edges of LOCKED since configuration (saturating). Also the gated (BUFGCE) output measured the same way on a second meter: `G <count>`.
  - `scripts/clock_probe_run.py`: builds a probe for a named setting and toolchain, loads it on the board with the existing `tt_harness.host.run` runner pieces (activity check, cleanup), captures 10 lines of UART, and prints a table.

- [ ] **Step 1:** `tests/test_freq_meter.py` (Migen simulator, two clock domains): a measured clock of a known ratio to the reference gives a count within ±1 of the expected value per gate, for ratios below and above 1 and for a non-integer ratio; a stopped measured clock gives 0; the count is never torn (a deliberately adversarial phase sweep gives only values within ±1).
- [ ] **Step 2:** Implement `FreqMeter`. `tests/test_clock_probe.py`: the UART line format, the lock-loss counter, and that the generated Verilog has the MMCM parameters asked for.
- [ ] **Step 3:** Build with both toolchains (marked `toolchain` / `vivado`) for these settings, each from the Arty's 100 MHz: **A** `divclk 1, mult 9, div 35.75` (the Arty's own solver setting: odd divider; wrong filter table in the open flow); **B** `divclk 2, mult 18, div 35.75` (the Game Bub's MMCM numbers: PFD 50 MHz, VCO 900 MHz; filter table right in the open flow); **C** `divclk 1, mult 9, div 36.75` (even-integer control, 24.4898 MHz); **D** an integer control `divclk 1, mult 9, div 36` (25.000 MHz). Timing must be met with every domain constrained, as in Task 7b.
- [ ] **Step 4:** Run all eight bitstreams on the board (`ssh -p 10722 pi@ps1.fpgas.online`, UART `/dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0`), ten 1 s readings each. Expected frequency for A and B is 25,174,825 Hz, C 24,489,796 Hz, D 25,000,000 Hz, each within the oscillator's tolerance (the Arty's oscillator is ±50 ppm class; the Pi's UART timing is irrelevant because the gate is the FPGA's own oscillator, so the reading is a pure ratio and should be within a few counts of the ideal). Record LOCKED and lock losses. If cheap, also measure the duty cycle of the free-running output for A and B in both flows (sample the measured clock with the 100 MHz clock over many cycles and report the fraction high; asynchronous sampling gives the duty cycle statistically) since the open flow's encoding is expected to change it.
- [ ] **Step 5:** Update `docs/results/2026-11-spike-b-project-clock.md` with a "Measured on hardware" section: the table of readings, and a plain conclusion for each open question: is the open-flow frequency right for an odd integer part; does the MMCM lock and stay locked with the wrong filter table; what the duty cycle is. State what one board at room temperature for ten seconds does and does not show about lock stability. Then, based on the measurement, say whether the solver needs any toolchain-specific rule.
- [ ] **Step 6:** Leave the board clean. Commit.

---

### Task 9b: Make the open flow build the whole pool (added 2026-11-24)

Added after Task 9 found that openXC7 builds five of the eight pool designs
(`docs/results/2026-11-spike-a-compare-build.md`): the harness's cross-domain allowance refuses
three, two of which also need DSP inference turned off; and the open-flow clock scan reports a
constant-driven clock pin as a project clock. This task may use the PS1 Arty for the final runs.

**Files:**
- Modify: `tt_harness/compare/recorder.py`, `tt_harness/build.py`, `tt_harness/project_clocks.py` if needed, `tests/test_build.py` and fixtures, `docs/results/2026-11-spike-a-compare-build.md`

- [ ] **Step 1 (settle):** the compare build's default settle becomes 3 system cycles (it was 2). The step is then: pclk rises at t0, sample at t0+3, falls at t0+4, `clock_index` changes at t0+5, next rise at t0+8; pclk period 80 ns; the real project-to-system requirement 30 ns; the open flow's budget 20 ns with the allowance unchanged. Every constraint derived from settle (the generated clock's `-divide_by`, the multicycle values, project-clock periods and max/min delays, the open flow's per-clock frequencies and budgets) must follow from the one parameter; find any place that assumes 2 or 60 ns. This is the coordinator's ruling, not a decision by the owner, who has been told and may reverse it; an allowance based on measured skew was the alternative.
- [ ] **Step 2 (DSP):** the open flow synthesises without DSP inference (`synth_xilinx -nodsp`), recorded in `BuildResult.limitations` in fixed wording saying why (nextpnr-xilinx 0.8.2 cannot route the constant cascade inputs of inferred DSP48E1 cells). Vivado is unchanged.
- [ ] **Step 3 (clock scan):** the pre-pack scan ignores a net on a clock pin when it is driven by a constant (and a cell clock pin tied off); test with the two saved logs/netlists that showed `…CLK$const`, and a unit test.
- [ ] **Step 4 (tests build elsewhere):** real-build tests write under the pytest temporary directory, never into the default `build/compare` tree that the CLI and pool script use.
- [ ] **Step 5 (pre-header bytes):** the receiver records how many bytes arrived before the frame header and their values (up to 16) in the run's JSON, and `run-compare` prints the count; unit-tested with the fake runner.
- [ ] **Step 6:** rebuild all eight pool designs with both toolchains; expected sixteen bitstreams with every domain met. Any failure is investigated and reported, not worked around; if a design still misses the open flow's budget at settle 3, report the figure.
- [ ] **Step 7:** run all sixteen on the board; expected sixteen `exact`. Record pre-header byte counts.
- [ ] **Step 8:** update the results document: a dated section "After Task 9b" with the new tables, what changed, and what is still open; keep the original results as the record of what happened first.
