# Game Bub hardware notes

Facts the harness design depends on, collected 2026-11-23 from the upstream repositories
(commercial board revision 4.1). Each fact names the file it came from so it can be
re-checked. Items marked *unverified* were not confirmed against a primary source.

Sources:

- `elipsitz/gamebub`, branch `handheld` @266edbb and tag `v1.1-beta` @8b1135b (MCU firmware, Chisel RTL)
- `gamebub/framework` @3845381 (core framework, constraints, build scripts)
- `gamebub/gamebub-pcb` (KiCad, `handheld_rev4_1/schematic.pdf`)
- `gamebub/core-example`

## FPGA

- Fitted part is `XC7A100T-2CSG324I` (`gamebub-pcb/handheld_rev4_1/fpga_config.kicad_sch`).
  The official build targets `xc7a100tcsg324-1` (`framework/scripts/build_core.py:71`).
- `xc7a100tcsg324-1/-2/-2L/-3` are all in openXC7 `prjxray-db` (`artix7/mapping/parts.yaml`).
- All user I/O is LVCMOS33 except HDMI (TMDS_33) (`framework/verilog/handheld/rev_4.xdc`).
- One 50 MHz oscillator on P17. No configuration flash.

## Configuration and the MCU

- The MCU is an ESP32-S3-MINI-1-N8 running open Rust firmware (GPL-3.0,
  `gamebub/firmware/handheld`). It owns the microSD card (SDIO), USB, the UI, the I2C
  peripherals, FPGA power, and FPGA configuration.
- Configuration is slave serial. The MCU pulses PROGRAM_B and streams the bitstream at
  80 MHz (`firmware/handheld/src/device/drivers/fpga/mod.rs`). Every load is therefore a
  full reconfiguration.
- The configuration pins share nets with the runtime link: `FPGA_SPI_CLK` goes to both CCLK
  (E9) and user pin T14, and `FPGA_SPI_D0` goes to both DIN (K18) and V15.
- Runtime link is QSPI (clk, cs_n, d[3:0]) plus an open-drain `mcu_irq_n`. There is no UART.
- The stock firmware expects the framework register map at `0xF100_xxxx` over QSPI
  immediately after load, and reads the twelve FPGA-side buttons through register
  `0xF1002000`.
- JTAG is on a 6-pin header, J702.
- The FPGA has no path to the SD card except through the MCU.

## Loading third-party cores (stock firmware)

- Needs firmware v1.1-beta. On the `handheld` branch `DIR_CORES` is `/dev/null`.
- `/cores/Author.Name/core.json` on the SD card, listing
  `bitstreams[{target: "gamebub_rev4", filename: "*.bit"}]`.
- The file must be a Xilinx `.bit` with the standard header
  (`firmware/handheld/src/device/drivers/fpga/xilinx.rs`). If the design-name tag carries
  `UserID=` it must equal `0xB0100004`; a missing ID is accepted.

## Display

- ILI9806E controller, 24-bit parallel RGB (dot clock, hsync, vsync, DE, 8/8/8) driven
  directly by the FPGA. The MCU does the SPI initialisation, reset and backlight
  (`firmware/handheld/src/device/drivers/ili9806e.rs`).
- Scanned as 480x800 portrait and rotated in RTL, dot clock 25.8 to 35 MHz
  (`platform/handheld/display/ILI9806E.scala`). The exact visible area (720x480 per the
  docs, 800x480 logical in the RTL with `displayOffsetX=-28`) is *unverified*.
- The upstream display RTL is Chisel under CERN-OHL-S-2.0, so it is not reused here.

## Buttons

- Twelve on the FPGA, active low with pull-ups: A, B, X, Y, Up, Down, Left, Right, L, R,
  Start, Select.
- Home, Vol+, Vol- and Power are on the MCU. No analogue sticks.

## Audio

- TLV320DAC3101 stereo DAC with speaker amplifier and headphone driver. I2S
  (MCLK, WCLK, BCLK, DIN) from the FPGA; I2C configuration (address 0x18) and reset from
  the MCU.

## Other peripherals

| Peripheral | Wired to | Notes |
|---|---|---|
| microSD | MCU only | SDIO 4-bit |
| Cartridge slot | FPGA | level shifted; 5 V (GB) or 3.3 V (GBA); MCU controls cartridge power |
| Link port | FPGA | level shifted, same switched voltage |
| USB-C data | MCU | USB full speed |
| HDMI | FPGA | TMDS on the USB-C SuperSpeed pairs, to the dock |
| SDRAM | FPGA | 2x W9825G6KH-6, 32 MiB each, shared bus |
| SRAM | FPGA | IS61WV25616BLL-10, 512 KiB, 16-bit |
| PMOD | FPGA | 4 I/O, 3.3 V, 200R series |
| Rumble | FPGA | DRV8837C |

## FPGA pins (`framework/verilog/handheld/rev_4.xdc`)

| Function | Pins |
|---|---|
| clk_50mhz | P17 |
| LCD sync | dotclk T18, hsync U18, vsync T16, DE R18 |
| LCD B[0..7] | R17 P18 N17 M17 M18 L16 L18 K17 |
| LCD G[0..7] | J17 J18 H16 H17 G18 G17 F18 E17 |
| LCD R[0..7] | E18 E16 D18 D17 C17 B18 B17 A18 |
| Buttons | A L6, B K6, X M6, Y N5, Up N16, Down L13, Left L14, Right M16, L K13, R K5, Start H6, Select H5 |
| DAC | mclk T15, wclk R15, bclk R16, din P15 |
| MCU QSPI | clk T14, cs_n U17, d0-3 V15 V16 U16 V17, irq_n N15 |
| HDMI (p/n) | clk C2/C1, d0 A4/A3, d1 B3/B2, d2 B1/A1 |
| PMOD[0..3] | V14 U14 U13 R13 |
| Test points | M14 M13 N14 P14 |

Cartridge, link, SDRAM and SRAM pins are in the same file.

## Prior art

- `AEW2015/gamebub-litex` (October 2026): a LiteX VexRiscv SoC for rev 4 built with
  Yosys, nextpnr-xilinx and prjxray; hardware-tested for LCD, buttons, SRAM and SDRAM,
  loaded over JTAG. No upstream `litex-boards` platform exists.
- MiSTer-style ports using the official Vivado flow: `florin-byte/gamebub-gg`,
  `Wenwald/gamebub-2600`, `alanswx/ColecoVision_GameBub` and others.

## Partial reconfiguration tooling

- Vivado: DFX needs no licence in any edition since 2019.1 (UG909), and the XC7A100T is in
  the free edition.
- openXC7: no flow that emits partial bitstreams was found. The F4PGA "ROI" flow uses a
  Vivado-built harness and merges FASM into a full bitstream.
