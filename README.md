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

## Licence

Apache-2.0. See `LICENSE`.
