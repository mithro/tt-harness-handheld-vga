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
- Runtime link is QSPI (clk, cs_n, d[3:0]) plus an interrupt pin, N15. On rev 2-4 that pin
  is **push-pull, active high**: it drives the gate of an n-FET (Q801, AO3400A,
  `gamebub-pcb/handheld_rev4_1/fpga_io_14.kicad_sch`) that pulls the MCU's active-low,
  level-triggered interrupt line (`firmware/handheld/src/device/interrupt.rs:85-91`); the
  framework drives it that way (`framework/verilog/handheld/top.sv:404-410`). Only rev 1
  had an open-drain `mcu_irq_n`. There is no UART.
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

## SD card access for a core, through the MCU (stock firmware)

Checked 2026-11-23 against `elipsitz/gamebub` branch `v1.1-beta` @8b1135b (a branch, not a
tag). Firmware paths are under `firmware/handheld/src/`.

- A core's `files.json` (`core/info.rs`) declares up to 16 files. Each entry has exactly
  one of `filename` (a fixed path inside `/sdcard/cores/<id>/`, loaded with no picker),
  `user_selected` or `dependent_on_0`, plus `address`, `max_size`, `read_only`,
  `on_demand` and others.
- **Preload:** before the core runs, the MCU writes the whole file over QSPI to `address`
  in 16 KiB chunks, bracketed by `FILE_WRITE_START` (0x0300) and `FILE_WRITE_END` (0x0301)
  (`core/files.rs`).
- **On demand, started by the core** (`core/command.rs`, `core/files_on_demand.rs`): the
  core raises a request (IRQ bit 4); the MCU reads six words (command plus five
  parameters) from 0xF000_1000, acts, writes three result words back and sets status bits
  in 0xF100_1104. Commands: `FILE_GET_SIZE` 0x0400, `FILE_SET_SIZE` 0x0401, `FILE_READ`
  0x0402 (`[id, dest_addr, off_lo, off_hi, len]`; the MCU QSPI-writes the data to
  `dest_addr`), `FILE_WRITE` 0x0403, `FILE_FLUSH` 0x0404.
- So a core **can** read any declared file at any offset whenever it chooses, on stock
  firmware. It cannot open a name it invents at runtime; the file must be declared in
  `files.json`. Opening arbitrary names would need a small firmware change (a `FILE_OPEN`
  command).
- An SPI error during `FILE_READ` makes the firmware panic (`files_on_demand.rs` L195).
  Real QSPI throughput on the board is unmeasured.

### What a core must implement for stock firmware

**The authority for this is `docs/gamebub-mcu-protocol.md`**, written from the firmware
source with line citations; the outline below is a summary and defers to it.

The official framework's top level (Chisel, `HandheldTop.scala`) implements the QSPI slave
and the `0xF1` registers, and hands everything else to the core. A design that replaces
that top level, as this harness does, must implement all of it itself:

- **QSPI framing** (`device/drivers/fpga/mod.rs` L252-296): one command byte on a single
  line, then a 32-bit big-endian address in quad mode, then data in quad mode. Command
  byte: bit 0 read, bits 1-2 word size, bit 3 byte swap, bit 4 auto-increment, bits 5-6
  line width. Reads have 8 dummy bytes (16 clocks). Register reads are capped at 16 MHz
  (`MAX_SPI_READ_CLOCK`, `device/drivers/fpga/mod.rs:46`) but actually run at **10 MHz**:
  the firmware's devices are 40/20/10/4/2 MHz and it takes the first not above the cap
  (`device/mod.rs:372`, `device/drivers/fpga/mod.rs:219-237`).
- **Address map:** 0xF1xx_xxxx registers (buttons at 0xF100_2000; host command status at
  **0xF100_1100**, `device/drivers/fpga/mod.rs:31`, `core/command.rs:80-109`; 0xF100_1104 is
  the core request channel, `core/command.rs:120-192`; interrupt enable and pending), 0xF2
  overlay; everything else is core memory. The firmware never addresses 0xF3 (the
  framework's framebuffer region; no 0xF3 address appears in `firmware/handheld/src` at
  8b1135b, and the firmware's framebuffer output goes to the 0xF2 overlay,
  `device/mod.rs:469-474`). A host mailbox at 0xF000_0000 and a core mailbox at
  0xF000_1000.
- **Handshakes:** `GET_STATUS` (reply SETUP=2, then HALT=3), `SETUP_COMPLETE`, `CORE_RUN`,
  `CORE_HALT`, `NOTIFY_FOCUS`. `core-example/rtl/demo_core.sv` L63-160 shows the core side.
- **Timeouts:** 100 ms for setup, 10 ms for notify (`core/mod.rs:36-37`). The 100 ms starts
  at the firmware's first status poll (`core/mod.rs:278`, `core/command.rs:44-63`), after
  the dock write, interrupt-enable write and button read that follow configuration
  (`bitstream/mod.rs:56-58`). A failure during load shows an error and reloads the boot
  bitstream. CORE_RUN and NOTIFY_FOCUS failures are ignored even during load
  (`core/mod.rs:344-345`, `:256-260`); a SETUP_COMPLETE failure aborts it
  (`core/mod.rs:331`).
- The MCU initialises the LCD and the DAC (fixed 48 kHz, MCLK = 256 x fs) at boot whatever
  core is loaded, turns the backlight off while programming, and powers the DAC up on
  focus. The core must generate the I2S clocks.
- No command lets a core ask for a different bitstream, and there is no UART or logging
  channel for cores.

The framework RTL is CERN-OHL-S-2.0 (or GPL-2.0-or-later), and there is no Verilog version
of the bus slave. This project implements the protocol above from this description, not
from that source.

## Display

- ILI9806E controller, 24-bit parallel RGB (dot clock, hsync, vsync, DE, 8/8/8) driven
  directly by the FPGA. The MCU does the SPI initialisation, reset and backlight
  (`firmware/handheld/src/device/drivers/ili9806e.rs`).
- Scanned as 480x800 portrait and rotated in RTL, dot clock 25.8 to 35 MHz
  (`platform/handheld/display/ILI9806E.scala`). The exact visible area (720x480 per the
  docs, 800x480 logical in the RTL with `displayOffsetX=-28`) is *unverified*.
- The upstream display RTL is Chisel under CERN-OHL-S-2.0, so it is not reused here.

## Buttons

- Twelve on the FPGA, active low (each switch pulls its line to ground): A, B, X, Y, Up,
  Down, Left, Right, L, R, Start, Select.
- **The FPGA's internal pull-ups are required.** On rev 4.1 the external pull-up packs
  RN1601, RN1602, RN1603 and R1602 are marked DNP (not fitted) in
  `gamebub-pcb/handheld_rev4_1/buttons.kicad_sch`, whose note gives the Artix-7 internal
  pull-up current instead (90 to 330 uA at 3.3 V). Upstream sets `PULLTYPE PULLUP` on the
  button ports (`framework/verilog/handheld/rev_4.xdc:131`). Only R1601, on the MCU's Home
  button, is fitted.
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
| MCU QSPI | clk T14, cs_n U17, d0-3 V15 V16 U16 V17, irq N15 (push-pull, active high into an n-FET; the XDC names it `mcu_irq_n`) |
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
