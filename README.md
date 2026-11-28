# tt-harness-handheld-vga

An FPGA harness for running [Tiny Tapeout](https://tinytapeout.com/) demo designs on
handheld-console-shaped FPGA boards.

**Status: plan 1 (the spikes) is done.** The compare build runs a Tiny Tapeout VGA design on
a Digilent Arty A7-35T, records its `uo_out` every clock and checks it byte for byte
against a reference simulation; all eight designs of the first test pool are `exact` on
hardware with both toolchains. The results:

- [Spike A: the compare build over the whole test pool](docs/results/2026-11-spike-a-compare-build.md)
- [Spike B: the project clock under both toolchains](docs/results/2026-11-spike-b-project-clock.md)
- [Spike C: synthesis of the Game Bub MCU link](docs/results/2026-11-spike-c-gamebub-link-synthesis.md)

The management SoC, the screen, buttons and sound of the consoles below are later plans.

The harness wraps an unmodified Tiny Tapeout project (`tt_um_*`, the standard
`ui_in` / `uo_out` / `uio_*` / `clk` / `rst_n` interface) and connects it to the board the
way the [VGA Playground](https://github.com/TinyTapeout/vga-playground) connects it to a
browser:

| Tiny Tapeout side | Board side |
|---|---|
| Tiny VGA Pmod on `uo_out` | the console's screen |
| Gamepad Pmod on `ui_in[6:4]` (latch, clock, data) | the console's buttons |
| Audio Pmod on `uio[7]` | the console's speaker / headphones |
| 7-segment display on `uo_out` | a virtual 7-segment display drawn on screen |

A small RISC-V SoC in the harness (LiteX) handles management. In the first stage each
bitstream contains the harness and one design built together; a later stage puts the design
in a reconfigurable region so harness and demos can be built and loaded separately.

## Targets

- **Game Bub** (XC7A100T) is the v0 target.
- **Digilent Arty A7** is the development and test target (boards on fpgas.online).
- Other handheld-console FPGA boards are intended later.

## Toolchains

Both [Xilinx Vivado](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html)
and the open-source [openXC7](https://github.com/openXC7) flow
([Yosys](https://github.com/YosysHQ/yosys),
[nextpnr-xilinx](https://github.com/openXC7/nextpnr-xilinx),
[Project X-Ray](https://github.com/f4pga/prjxray)).

[`uv run scripts/setup_toolchains.py`](scripts/setup_toolchains.py) installs the
open-source tools into `.toolchains/`; see [`docs/toolchains.md`](docs/toolchains.md).

## Quick start

Everything runs through [uv](https://docs.astral.sh/uv/). The fast tests need nothing but
`uv sync`:

```sh
uv sync
uv run pytest -m "not toolchain and not vivado" -q -W error -rs
```

Tests marked `toolchain` need `.toolchains/`, tests marked `vivado` need Vivado, and tests
marked `board` use a real board and run only with `--board`.

The `tt-harness` command (`uv run tt-harness --help`) has these sub-commands:

| Sub-command | What it does |
|---|---|
| `fetch` | fetch a design's sources and print the source root |
| `reference` | simulate a design with Verilator and dump `uo_out` once per clock |
| `build-compare` | build the compare bitstream for a design and gate it on timing |
| `verdict` | compare a UART recording with the shared reference and print JSON |
| `run-compare` | build, run on a board over SSH, record and compare; print JSON |
| `pool-sim` | run every pool design through the simulated compare build |

One build, with the open flow, after installing the tools:

```sh
uv run scripts/setup_toolchains.py
uv run tt-harness build-compare tt08/tt_um_tinytapeout_logo_screensaver --board arty --toolchain openxc7
```

The first test pool in simulation, eight designs at once:

```sh
uv run tt-harness pool-sim --clocks 1300000 --jobs 8
```

Two sibling checkouts are read as files (never imported), found through these
environment variables:

| Variable | Default | Holds |
|---|---|---|
| `TT_VIDEOS_REPO` | `~/github/TinyTapeout/tinytapeout-vga-videos` | the manifest of designs ([tinytapeout-vga-videos](https://github.com/mithro/tinytapeout-vga-videos)) |
| `TT_REFERENCE_DIR` | `~/github/TinyTapeout/tt-harness-fpga-vga/work/reference` | the shared reference simulations ([tt-harness-fpga-vga](https://github.com/mithro/tt-harness-fpga-vga)) |

Both can also be given per command (`--videos-repo`, `--reference-dir`).

## Documentation

| Document | Subject |
|---|---|
| [`docs/arty-boards.md`](docs/arty-boards.md) | the Arty A7 boards on fpgas.online |
| [`docs/demo-runner.md`](docs/demo-runner.md) | the Tiny Tapeout demo board's demo runner |
| [`docs/gamebub-hardware.md`](docs/gamebub-hardware.md) | verified Game Bub hardware facts |
| [`docs/gamebub-mcu-protocol.md`](docs/gamebub-mcu-protocol.md) | the Game Bub MCU-to-core protocol |
| [`docs/test-designs.md`](docs/test-designs.md) | the test designs and reference data |
| [`docs/toolchains.md`](docs/toolchains.md) | installing and using the FPGA toolchains |
| [`docs/cdc-constraints.md`](docs/cdc-constraints.md) | the clock-domain crossing constraints the FPGA build must apply |
| [`docs/superpowers/specs/2026-11-23-tt-harness-handheld-vga-design.md`](docs/superpowers/specs/2026-11-23-tt-harness-handheld-vga-design.md) | the design specification |
| [`docs/superpowers/plans/2026-11-23-plan-1-spikes.md`](docs/superpowers/plans/2026-11-23-plan-1-spikes.md) | plan 1: the spikes |
| [`docs/superpowers/plans/2026-11-25-plan-2-core-soc-arty-demo.md`](docs/superpowers/plans/2026-11-25-plan-2-core-soc-arty-demo.md) | plan 2: the core SoC and Arty demo |
| [`docs/results/2026-11-first-hardware-run.md`](docs/results/2026-11-first-hardware-run.md) | the first hardware run |
| [`docs/results/2026-11-spike-a-compare-build.md`](docs/results/2026-11-spike-a-compare-build.md) | Spike A: the compare build |
| [`docs/results/2026-11-spike-a-compare-build-9b-timing.md`](docs/results/2026-11-spike-a-compare-build-9b-timing.md) | Spike A after Task 9b: timing per domain |
| [`docs/results/2026-11-spike-a-compare-build-9c-timing.md`](docs/results/2026-11-spike-a-compare-build-9c-timing.md) | Spike A after Task 9c: timing per domain |
| [`docs/results/2026-11-spike-b-project-clock.md`](docs/results/2026-11-spike-b-project-clock.md) | Spike B: the project clock |
| [`docs/results/2026-11-spike-c-gamebub-link-synthesis.md`](docs/results/2026-11-spike-c-gamebub-link-synthesis.md) | Spike C: the Game Bub MCU link |

## Licence

Apache-2.0. See `LICENSE`.

## Development

```sh
uv sync
uv run tt-harness --version
uv run pytest -m "not toolchain and not vivado"
```

Tests marked `toolchain` need `.toolchains/`; tests marked `vivado` need Vivado.
Tests marked `slow` (exhaustive simulation sweeps) are skipped unless `--run-slow` is given or
`-m` names `slow`; run them before merging changes to the code they cover:

```sh
uv run pytest -m "not toolchain and not vivado"   # fast
uv run pytest tests/test_slot.py tests/test_atspeed.py tests/test_atspeed_abort.py --run-slow -m "not vivado" -q -W error   # everything for the slot / at-speed check
```

Parallel (pytest-xdist; `-n` is not a default because FPGA-build tests must run one at a time,
so use it only with the markers that leave those out):

```sh
uv run pytest -m "not toolchain and not vivado" -n 16 --dist loadgroup -q -W error   # fast, parallel
uv run pytest tests/test_slot.py tests/test_atspeed.py tests/test_atspeed_abort.py tests/test_atspeed_pins.py \
    tests/test_atspeed_random.py tests/test_crossing.py tests/test_slot_order.py tests/test_cdc_structure.py --run-slow -m "not vivado" -n 16 --dist loadgroup -q -W error
```
