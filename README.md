# tt-harness-handheld-vga

An FPGA harness for running [Tiny Tapeout](https://tinytapeout.com/) demo designs on
handheld-console-shaped FPGA boards.

**Status: design phase. Nothing is built yet.**

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

Both Xilinx Vivado and the open-source openXC7 flow (Yosys, nextpnr-xilinx, Project X-Ray).

`uv run scripts/setup_toolchains.py` installs the open-source tools into `.toolchains/`;
see [`docs/toolchains.md`](docs/toolchains.md).

## Documentation

- [`docs/gamebub-hardware.md`](docs/gamebub-hardware.md): verified Game Bub hardware facts
  the design depends on.
- [`docs/toolchains.md`](docs/toolchains.md): installing and using the FPGA toolchains.
- [`docs/cdc-constraints.md`](docs/cdc-constraints.md): the timing constraints and checks the
  FPGA build must apply to the clock-domain crossings.

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
