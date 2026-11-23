# Game Bub MCU-to-core protocol

This document specifies what the stock Game Bub MCU firmware does on the QSPI link to a
loaded FPGA core, and what a core must do in return so that the firmware accepts it. It is
written from the firmware source so that a core can be built without the official core
framework. It describes the interface in our own words; no firmware or framework source is
reproduced.

**Authority.** `elipsitz/gamebub`, branch `v1.1-beta`, commit `8b1135b`. All paths below are
relative to `firmware/handheld/src/` at that commit, and `file:N` or `file:N-M` cite lines.
Where the firmware leaves something to a library (the ESP-IDF SPI driver, `esp-idf-hal`
0.45.2 per `firmware/handheld/Cargo.lock`), the point is listed under
[Unverified](#unverified).

Terms: **MCU** is the ESP32-S3 running the firmware. **Core** is the FPGA design. A
**word** is the unit selected by the command byte's word-size field. "LE" is
little-endian.

## 1. Physical link

| Signal | Direction | Notes |
|---|---|---|
| `clk` | MCU to FPGA | SPI clock. Shares the net with configuration CCLK. |
| `cs_n` | MCU to FPGA | Active low. Driven by software as a GPIO (`device/mod.rs:384-389`). |
| `dq[3:0]` | bidirectional | `dq[0]` shares the net with configuration DIN. |
| `irq_n` | FPGA to MCU | Shared, active-low, level-triggered MCU interrupt line (`device/interrupt.rs:85-91`). Must be open-drain. |

- One ESP SPI host (`spi2`) drives the link in quad mode, half duplex, with DMA and a
  32 KiB maximum transfer (`device/mod.rs:218-228`, `device/mod.rs:377-379`).
- On rev 4 the LCD uses a separate SPI host, so no other device's clock appears on this
  bus outside FPGA configuration (`device/mod.rs:233-243`). During configuration the same
  bus carries the bitstream at 80 MHz with no chip select (`device/mod.rs:394-399`).
- **Clock rates.** The firmware creates one SPI device per rate: 40, 20, 10, 4 and
  2 MHz, in that order (`device/mod.rs:372`). Every transaction names an optional maximum
  rate and uses the first device in that list whose rate does not exceed it
  (`device/drivers/fpga/mod.rs:219-237`). Hence:
  - register writes (`write_u32`, no maximum) run at **40 MHz**
    (`device/drivers/fpga/mod.rs:298-302`);
  - register reads (`read_u32`, maximum 16 MHz, `MAX_SPI_READ_CLOCK`) run at
    **10 MHz** (`device/drivers/fpga/mod.rs:46`, `:316-321`); a core must still work at
    the 16 MHz cap, which is what the firmware's constant promises;
  - overlay writes have a maximum of `system_clock * 16 / 8`, where `system_clock`
    defaults to 8 MiHz and is changed only by the built-in cores, so they run at
    **10 MHz** (`device/drivers/fpga/mod.rs:127`, `:323-329`);
  - file transfers are capped at `2 * max_transfer_speed` kHz (default 5000 KB/s, so
    10 MHz) and file reads additionally at 16 MHz (`core/files.rs:142`, `:276-277`,
    `core/info.rs:98-100`).

## 2. QSPI framing

A transaction is one `cs_n` low period (`device/drivers/fpga/mod.rs:263-270`,
`:287-295`). The three or four phases are separate driver operations under one chip
select, so the clock may pause between phases while `cs_n` stays low.

1. **Command**: 1 byte, single line on `dq[0]`, most significant bit first: 8 clocks.
2. **Address**: 4 bytes, big-endian (`to_be_bytes`), quad: 8 clocks
   (`device/drivers/fpga/mod.rs:262`, `:284`).
3. Writes: **data**, quad, as many bytes as given: 2 clocks per byte.
4. Reads: **dummy**, 8 bytes read in quad and discarded: **16 clocks**
   (`device/drivers/fpga/mod.rs:285-292`); then **data**, quad, 2 clocks per byte
   (`:293`).

In quad phases each byte is sent as its high nibble then its low nibble, nibble bit 3 on
`dq[3]` (see Unverified U2). SPI mode 0: the clock idles low, the receiver samples on the
rising edge, the transmitter changes data after the falling edge (U1).

```
            command (dq0 only)      address (quad)    dummy (16 clk)    data (quad)
cs_n  ‾‾‾\______________________________________________________________________ ... /‾‾
clk   ____/‾\_/‾\_ ... _/‾\_/‾\_ ... _/‾\_/‾\_/‾\_ ... _/‾\_/‾\_/‾\_/‾\_ ...
           c7  c6        c0  A31..28   A3..0  (MCU releases dq)  D0hi D0lo D1hi ...
edge no.   1   2         8   9         16     17 ...        32   33   34   35
                                                  FPGA drives dq: first data nibble
                                                  valid at rising edge 33
```

Rising edges counted from 1 after `cs_n` falls: command bits on edges 1-8, address
nibbles on edges 9-16, dummy edges 17-32, read data nibble *k* (from 0) on edge 33+*k*.
The FPGA may drive `dq` at any time from the dummy phase onwards and must stop driving
when `cs_n` rises.

### 2.1 Command byte

Built by `SpiCommand::as_read_command`/`as_write_command` and `spi_write`/`spi_read`
(`device/drivers/fpga/mod.rs:367-379`, `:259-261`, `:281-283`):

| Bits | Field | Values the firmware sends |
|---|---|---|
| 0 | read | 1 read, 0 write |
| 2:1 | word size | 0 = 8 bit, 1 = 16 bit, 2 = 32 bit, 3 = 64 bit (`:342-349`); 3 is never sent |
| 3 | byte swap | always 1 (`SpiCommand::new`, `:358-365`) |
| 4 | auto-increment | always 1 (`:358-365`) |
| 6:5 | line width | always quad (`LineWidth::Quad`, numeric value 2, U3) |
| 7 | — | 0 |

So the firmware sends only `0x54`/`0x55` (8 bit), `0x56`/`0x57` (16 bit) and
`0x58`/`0x59` (32 bit) write/read commands.

### 2.2 Data, words, byte order and address increment

The data stream is a sequence of words of the selected size. The word at stream position
*w* lives at byte address `A + w * size` with auto-increment, or at `A` for every word
without it.

- **Byte swap = 1**: the bytes of a word are sent lowest address first. The firmware
  serialises a `u32` with `to_le_bytes` and reads it back with `from_le_bytes`
  (`device/drivers/fpga/mod.rs:300`, `:320`), so with byte swap set, memory is
  little-endian: byte `A+0` holds bits 7:0 of a 32-bit register.
- **Byte swap = 0**: the bytes of a word are sent highest address first (the word as a
  big-endian number). The firmware never sends this; a core may support it.
- Bulk transfers (overlay, files, the boot bitstream's CPU memory) pass a byte buffer
  directly; with 16-bit words and byte swap that buffer lands in memory in order
  (`ui/mod.rs:126-130`, `core/files.rs:143-147`).
- A write's length need not be a multiple of the word size in the firmware's API, but
  every call site writes whole words: `u32` registers, 16-bit overlay pixels, and file
  chunks (`core/files.rs:140-147`; file sizes are not padded, U7).
- Bursts: file loads write up to 16 KiB per transaction (`bitstream/mod.rs:20`,
  `core/files.rs:140-147`); overlay writes one display line, at most 720 bytes, per
  transaction (`ui/mod.rs:116-131`, `:55`).

There is no error signalling on the link. A write cannot fail from the FPGA's point of
view; the firmware treats a driver error on a `read_u32` as fatal (`unwrap` at e.g.
`core/command.rs:91`), and ignores or unwraps write results depending on the call site.

## 3. Address map

| Range | Region | Firmware use |
|---|---|---|
| `0x0000_0000`-`0xEFFF_FFFF` | core memory | files, settings (only if `files.json`/`settings.json` exist); addresses must be below `0xF000_0000` and 4-byte aligned (`core/info.rs:20`, `:433-438`, `:449-454`) |
| `0xF000_0000`-`0xF000_0FFF` | host command mailbox | `core/command.rs:72-79`, `:99-107` |
| `0xF000_1000`-`0xF000_1FFF` | core request mailbox | `core/command.rs:129-145`, `:171-187` |
| `0xF100_0000`-`0xF1FF_FFFF` | framework registers | section 4 |
| `0xF200_0000`-`0xF2FF_FFFF` | overlay | section 7 |
| other `0xF3`-`0xFF` | unused by the firmware | the official framework puts a framebuffer at `0xF3`; the firmware at 8b1135b never addresses it |

## 4. Registers at 0xF1xx_xxxx

All are 32-bit, accessed by the firmware only with `write_u32`/`read_u32` (32-bit word,
byte swap, auto-increment, so four bytes LE). "Reset" is the value the core must present
after configuration; where the firmware does not depend on it, our choice is given in
brackets. Constants are from `device/drivers/fpga/mod.rs:18-41`.

| Address | Name | Dir | Bits and meaning | Reset | Firmware access |
|---|---|---|---|---|---|
| `0xF100_0000` | INFO_FRAMEWORK_VER | R | framework version | [0] | never read |
| `0xF100_0004` | INFO_SYSCLK_HZ | R | core system clock in Hz | [core's sys clock] | never read |
| `0xF100_0100` | INFO_VIDEO_DIM | R | video dimensions | [0] | never read |
| `0xF100_0104` | INFO_VIDEO_DEPTH | R | colour depth | [0] | never read |
| `0xF100_0108` | INFO_INPUT_CAPS | R | 3:0 number of input slots (clamped to 4); 6 rumble; 7 extended slot format (`input/mod.rs:139-141`, `:265-283`) | 0 recommended | read once during load (`core/mod.rs:283-287`) |
| `0xF100_1000` | CTRL_IRQ_ENABLE | RW | one enable bit per interrupt source, numbered as in section 6 | 0 | written whole from the MCU's own copy (`device/drivers/fpga/mod.rs:209-217`) |
| `0xF100_1004` | CTRL_IRQ_PENDING | R/W1C | one pending bit per source; writing 1 clears | 0 | read and written back on `irq_n` (`device/interrupt.rs:180-187`) |
| `0xF100_100C` | CTRL_DOCK | W | 0 docked; 1 use built-in screen while docked (`device/drivers/fpga/mod.rs:336-339`) | 0 | written after every configuration and on display-mode change (`device/mod.rs:647-654`, `:657-684`) |
| `0xF100_1010` | CTRL_FOCUS | W | 0: 1 = the core has focus (menu closed) | 0 | `core/mod.rs:245-247` |
| `0xF100_1014` | CTRL_VIBRATE | W | firmware writes 1 as the core starts (`core/mod.rs:342`); meaning not stated in the firmware (U8) | 0 | once |
| `0xF100_1018` | CTRL_VIDEO_FILTER | W | 1 = darken video (core lost focus), 0 = normal (`core/mod.rs:249-253`) | 0 | on every focus change |
| `0xF100_1100` | CTRL_CMD_HOST | RW | host command channel, section 5.1 | 0 | every command |
| `0xF100_1104` | CTRL_CMD_CORE | RW | core request channel, section 5.3 | 0 | only after IRQ 4 |
| `0xF100_1400` + 0x10*n | CTRL_INPUT_SLOT n | W | three words per slot, n < 4 (`device/drivers/fpga/mod.rs:304-314`) | — | only if INPUT_CAPS slots > 0 (`input/mod.rs:289-322`) |
| `0xF100_1500` | CTRL_INPUT_RUMBLE_BASE | — | defined, never accessed (`device/drivers/fpga/mod.rs:35`) | — | never |
| `0xF100_2000` | STATUS_BUTTON | R | buttons, 1 = pressed: 11 A, 10 B, 9 X, 8 Y, 7 Up, 6 Down, 5 Left, 4 Right, 3 L, 2 R, 1 Start, 0 Select (`device/input.rs:7-24`) | live | after every configuration (`bitstream/mod.rs:58`) and on every Button IRQ or MCU-button event (`device/interrupt.rs:195-198`) |
| `0xF100_2004` | STATUS_CART_SWITCH | R | 0: cartridge switch | live | only on boards without an MCU cart-switch pin; rev 4 has one (`device/mod.rs:699-705`, `device/board.rs:321`) |

Input slot format, for completeness (only used when INPUT_CAPS has slots): basic mode
writes one word to `0xF100_1400` with the merged external-gamepad buttons packed as
15 A, 14 B, 13 X, 12 Y, 11 Up, 10 Down, 9 Left, 8 Right, 7 L1, 6 R1, 5 L2, 4 R2, 3 L3,
2 R3, 1 Start, 0 Select (`input/mod.rs:146-163`, `:292-301`); extended mode writes three
words per slot: device type in 31:28 (2 digital, 3 analogue) with the same button
packing, then four signed stick bytes LE (LX, LY, RX, RY), then triggers (L in 7:0,
R in 15:8) (`input/mod.rs:143-144`, `:165-184`, `:303-320`). The firmware writes a slot
only when its value changes from the previous write, starting from 0, so with no gamepad
it writes nothing (`input/mod.rs:296-299`, `:315-318`).

The firmware touches no other `0xF1` address. A core should ignore writes to undefined
addresses and return 0 for reads of them (our rule; the firmware never does either).

## 5. Command channels and mailboxes

### 5.1 Host command channel (MCU to core)

Host mailbox: words at `0xF000_0000 + 4*i`. The firmware writes at most four words
(command plus three arguments, `core/files.rs:189-200`) and reads back at most one result
word, from the same addresses (`core/command.rs:99-107`). Our core provides eight words
(`0xF000_0000`-`0xF000_001F`), read/write.

CTRL_CMD_HOST bits (firmware view, `core/command.rs:80`, `:92`, `:109`, `:113`):

| Bit | Name | Written by | Meaning |
|---|---|---|---|
| 3 | request | MCU | MCU writes `0b1000` to start a command, 0 to end it |
| 2 | busy | core | optional; the firmware does not look at it |
| 1 | done | core | command finished |
| 0 | error | core | command finished with an error |

The firmware treats the command as finished when bit 0 **or** bit 1 is set, and as
failed when bit 0 is set. Our core reports success as `done` (`0b1010` read back with the
request bit) and failure as `done | error` (`0b1011`), the same encodings the firmware
itself writes to the core channel (`core/command.rs:158`, `:190`).

**One command** (`run_core_command`, `core/command.rs:65-118`), with a per-command
timeout *T*:

1. Write each request word *i* to `0xF000_0000 + 4*i` (`:73-79`). Write errors ignored.
2. Write `0x0000_0008` to CTRL_CMD_HOST (`:80`).
3. Note the start time. Loop: if more than *T* has elapsed, give up with
   `CommandTimeout(cmd)` **without** clearing CTRL_CMD_HOST (`:84-89`); read
   CTRL_CMD_HOST at 10 MHz (`:91`); if bit 0 or 1 is set, leave the loop; otherwise sleep
   5 ms (`CORE_POLL_INTERVAL`, `core/mod.rs:41`) and repeat (`:96`). The first read
   happens immediately after step 2.
4. Read the result words from `0xF000_0000 + 4*i` (`:100-107`).
5. Write 0 to CTRL_CMD_HOST (`:109`).
6. If bit 0 was set: `CommandFailed(cmd)` (`:113-117`).

Because step 3 can leave the request bit set after a timeout, a core must start a new
command on every write of CTRL_CMD_HOST byte 0 with bit 3 set, regardless of the channel's
previous state (our rule). A write with bit 3 clear ends the command and returns the
status bits to 0.

**Status poll** (`poll_core_status(expected, T)`, `core/command.rs:44-63`): loop: if more
than *T* has elapsed since the poll began, give up with `CoreReadyTimeout`; run
GET_STATUS with timeout *T*; any failure of that command (timeout or error) gives up with
`CommandFailed(GET_STATUS)`; if the result equals `expected`, succeed; else sleep 5 ms.

### 5.2 Host commands

Command codes are full 32-bit words (`core/command.rs:9-25`); arguments follow in mailbox
words 1 onwards.

| Code | Name | Args | Result | Firmware use |
|---|---|---|---|---|
| `0x0000` | GET_STATUS | — | word 0: core status | status polls |
| `0x0100` | CORE_RUN | — | — | start or restart the core |
| `0x0101` | CORE_HALT | — | — | stop the core (before exit, and in reset) |
| `0x0102` | SETUP_COMPLETE | — | — | end of setup |
| `0x0200` | NOTIFY_FOCUS | 1: focus (0/1) | — | focus change |
| `0x0300` | FILE_WRITE_START | 1: file id | — | before preloading a file (`core/files.rs:107-111`, `:328-332`) |
| `0x0301` | FILE_WRITE_END | 1: id, 2: bytes written, 3: 0 | — | after preloading (`core/files.rs:189-202`, `:348-361`) |
| `0x0302` | FILE_READ_START | 1: id | word 0: size to save | before saving a file at exit (`core/files.rs:251-255`) |
| `0x0303` | FILE_READ_END | 1: id | — | after saving (`core/files.rs:294-298`) |

Core status values (`core/command.rs:27-35`): 0 UNKNOWN, 1 INITIALIZE, 2 SETUP,
3 CORE_HALT, 4 CORE_RUN. The firmware waits for 2 after configuration and for 3 after
SETUP_COMPLETE; it never waits for 4.

Our core's status state machine (the firmware only requires the two transitions it waits
for): reset value SETUP; SETUP_COMPLETE moves SETUP to CORE_HALT; CORE_RUN moves CORE_HALT
to CORE_RUN; CORE_HALT moves CORE_RUN to CORE_HALT; each of these and NOTIFY_FOCUS
complete with `done` in every state. Any other code completes with `done | error`.

### 5.3 Core request channel (core to MCU)

The core asks for service by setting CTRL_CMD_CORE bit 3 and raising interrupt 4
(CoreRequest). On that interrupt the MCU (`core/command.rs:120-192`,
`worker/mod.rs:97-99`):

1. reads CTRL_CMD_CORE; returns if bit 3 is clear;
2. reads six words (command, five parameters) from `0xF000_1000` in one 24-byte
   transaction at 10 MHz;
3. for an unknown command writes `0b1011` (request, done, error) to CTRL_CMD_CORE and
   stops; otherwise writes `0b1100` (request, busy);
4. runs the command; writes three result words to `0xF000_1000` (one transaction, at most
   16 MHz); writes `0b1010` (success) or `0b1011` (failure) to CTRL_CMD_CORE.

The firmware never clears the request bit itself; the core does so on seeing done.

| Code | Name | Params | Results |
|---|---|---|---|
| `0x0400` | FILE_GET_SIZE | 0: id | 0: status, 1: size low, 2: size high |
| `0x0401` | FILE_SET_SIZE | 0: id, 1: size low, 2: size high | 0: status |
| `0x0402` | FILE_READ | 0: id, 1: core address, 2: offset low, 3: offset high, 4: length | 0: status, 1: bytes transferred; data is QSPI-written to the address in up to 16 KiB chunks |
| `0x0403` | FILE_WRITE | as FILE_READ | as FILE_READ; data is QSPI-read from the address |
| `0x0404` | FILE_FLUSH | 0: id | 0: status |

(`core/command.rs:20-25`, `core/files_on_demand.rs:86-286`.) File status: 0 OK,
1 invalid file, 2 I/O error, 3 out of bounds (`core/command.rs:37-41`). Only files
declared `on_demand` in `files.json` can be used this way (`core/files_on_demand.rs:49-51`).
An SPI driver error during FILE_READ panics the firmware (`core/files_on_demand.rs:195`).

## 6. Interrupts

Sources (`device/drivers/fpga/mod.rs:68-82`): bit 0 ModuleVblank, 1 Button,
2 SpiRequestOverflow, 3 SpiResponseUnderflow, 4 CoreRequest. The firmware keeps its own
copy of the enable mask and writes the whole mask on every change
(`device/drivers/fpga/mod.rs:209-217`); the copy survives reconfiguration, so the first
write after configuration normally contains only Button.

- Button is enabled after every configuration (`bitstream/mod.rs:57`, `main.rs:98`);
  CoreRequest just before SETUP_COMPLETE (`core/mod.rs:328`) and disabled again on exit
  (`core/mod.rs:227-229`). Vblank is used only by the built-in cores. Bits 2 and 3 are
  never enabled by the firmware.
- `irq_n` must be low while `(pending & enable) != 0` (the firmware relies on level
  semantics: it re-arms a low-level GPIO interrupt after servicing,
  `device/interrupt.rs:85-91`, `:192`). The line is shared with the DAC and fuel gauge
  (`device/interrupt.rs:165-177`).
- Service: read IRQ_PENDING; if non-zero write the same value back to clear it and
  dispatch; if the Button bit was set, read STATUS_BUTTON (`device/interrupt.rs:180-198`).
- What sets Button pending is the core's choice; the firmware only needs it whenever
  STATUS_BUTTON changes, so the menu sees FPGA buttons (`device/interrupt.rs:188-198`).

## 7. Overlay

The firmware draws its whole user interface (menus, load progress, load errors) into the
overlay (`ui/mod.rs:107-131`, `device/mod.rs:469-474`):

- 360 x 240 pixels on rev 3/4 (`ui/mod.rs:50-58`), one 16-bit ARGB1555 pixel per address
  pair: bit 15 alpha (1 opaque), 14:10 red, 9:5 green, 4:0 blue (`ui/slint.rs:98-107`,
  `:153-160`); 0 is transparent (`ui/slint.rs:102`).
- Pixel (x, y) is at `0xF200_0000 + 2 * (360 * y + x)`; writes use 16-bit words, byte
  swap and auto-increment, one span of a line per transaction, at 10 MHz
  (`ui/mod.rs:122-130`, `device/drivers/fpga/mod.rs:323-329`).
- Writes come from the UI thread whenever the UI redraws, interleaved with the core
  manager's transactions (each transaction holds the device lock); the core manager sends
  redraw requests during load (`core/mod.rs:279-280`).

## 8. Load sequence: end of configuration to "core running"

For a third-party core (`CoreHandlerImpl::None`, `core/mod.rs:112-117`) after the user
picks it and any user-selected files (`core/file_selection.rs:12-27`):

| # | Step | Transaction | Failure |
|---|---|---|---|
| 0 | LCD off, sleep 10 ms, configure the FPGA (`bitstream/mod.rs:34-55`) | slave serial, 80 MHz; `DONE` is logged, not checked (`device/drivers/fpga/mod.rs:196-200`) | config errors panic (`unwrap`) |
| 1 | dock state (`bitstream/mod.rs:56`) | W `CTRL_DOCK` = docked \| builtin<<1 | panic only on driver error |
| 2 | enable Button IRQ (`bitstream/mod.rs:57`) | W `CTRL_IRQ_ENABLE` = mask (normally `0x2`) | as above |
| 3 | input state (`bitstream/mod.rs:58`) | R `STATUS_BUTTON` | as above |
| 4 | LCD on (`bitstream/mod.rs:61-63`) | LCD bus only; sleeps at least 5 ms (`device/drivers/ili9806e.rs:217-229`) | — |
| 5 | wait for SETUP (`core/mod.rs:278`) | status poll, expected 2, *T* = 100 ms | abort |
| 6 | UI enters game view and redraws (`core/mod.rs:279-280`) | overlay writes, asynchronous | — |
| 7 | input caps (`core/mod.rs:283-287`) | R `INFO_INPUT_CAPS`; slot writes only if caps say so | panic only on driver error |
| 8 | preload files (`core/mod.rs:300`, `core/files.rs:29-218`) | per file: FILE_WRITE_START (*T* = 100 ms), data writes, FILE_WRITE_END (*T* = 10 ms) | abort |
| 9 | initial settings (`core/mod.rs:302-321`, `core/settings.rs:107-115`) | per setting: optional R then W of the setting address | — |
| 10 | enable CoreRequest IRQ (`core/mod.rs:328`) | W `CTRL_IRQ_ENABLE` = mask \| `0x10` | ignored |
| 11 | SETUP_COMPLETE (`core/mod.rs:331`) | command, *T* = 10 ms | abort |
| 12 | wait for CORE_HALT (`core/mod.rs:334`) | status poll, expected 3, *T* = 100 ms | abort |
| 13 | stage is Running (`core/mod.rs:338-339`) | — | — |
| 14 | vibrate (`core/mod.rs:342`) | W `CTRL_VIBRATE` = 1 | ignored |
| 15 | CORE_RUN (`core/mod.rs:344`) | command, *T* = 10 ms | **ignored** |
| 16 | focus (`core/mod.rs:345`, `:241-265`) | W `CTRL_FOCUS` = 1, W `CTRL_VIDEO_FILTER` = 0, NOTIFY_FOCUS(1) with *T* = 10 ms | NOTIFY_FOCUS failure ignored |

Timeouts are `SETUP_TIMEOUT` = 100 ms and `NOTIFY_TIMEOUT` = 10 ms (`core/mod.rs:36-37`).
The 100 ms of step 5 starts when that poll starts, after steps 1-4; steps 1-3 arrive
immediately after the last bitstream byte, so the link must work from the end of
configuration even though their results do not decide success.

**Abort** (`core/mod.rs:267-272`, `:193-231`): any error in steps 5-12 makes the firmware
skip CORE_HALT (the stage is not yet Running), write `CTRL_IRQ_ENABLE` without the
CoreRequest bit, power down the DAC, reconfigure the FPGA with the boot bitstream, and
show the error text (`core/error.rs:15-20`):

| Cause | Reported error | Text |
|---|---|---|
| status never reached in *T* | `CoreReadyTimeout` | "Timed out waiting for core to be ready" |
| GET_STATUS timed out or failed during a poll | `CommandFailed(0x0000)` | "Core command 0000 failed" |
| other command did not finish in *T* | `CommandTimeout(code)` | "Core command XXXX timed out" |
| other command finished with error | `CommandFailed(code)` | "Core command XXXX failed" |

**While running**: focus changes repeat step 16 with the new value (and darken video on
loss of focus); reset sends CORE_HALT then CORE_RUN, failures ignored
(`core/mod.rs:233-239`); exit sends CORE_HALT (failure ignored), saves files with
FILE_READ_START/FILE_READ_END and reads, then reconfigures with the boot bitstream
(`core/mod.rs:193-215`, `core/files.rs:220-306`).

## 9. Requirements for a core, summarised

1. Answer QSPI from the end of configuration, at 40 MHz for writes and up to 16 MHz for
   reads, with read data on rising edge 33 onwards.
2. Report status SETUP to GET_STATUS within 100 ms of the first poll, then CORE_HALT
   within 100 ms after SETUP_COMPLETE, finishing each command before its timeout (with a
   5 ms poll interval, a command that is not done by the first status read costs at least
   5 ms).
3. Return 0 from INFO_INPUT_CAPS unless it implements input slots.
4. Provide STATUS_BUTTON in the order of section 4 and, for the menu to see FPGA buttons,
   the Button interrupt.
5. Accept overlay writes (and display them, or the firmware's menus and errors are
   invisible).

## Unverified

Points the firmware source does not settle, with what this project assumes:

- **U1. SPI mode.** The firmware sets only baud rate and half duplex
  (`device/mod.rs:377-379`); the mode comes from the `esp-idf-hal` 0.45.2 default, taken
  to be mode 0 (clock idle low, sample on rising edge, MSB first). Consistent with the
  official framework's receiver.
- **U2. Quad nibble order.** High nibble first with bit 3 on `dq[3]` (ESP32-S3 SPI
  hardware convention), not stated in the firmware.
- **U3. `LineWidth::Quad` numeric value.** Assumed 2 (the `esp-idf-hal` enum order
  single, dual, quad), so bits 6:5 = `0b10` and the commands of section 2.1. The
  firmware casts the enum (`device/drivers/fpga/mod.rs:261`, `:283`).
- **U4. Idle lines in the command phase.** Whether the ESP drives `dq[3:1]` (and to what)
  while sending the single-line command byte. A core must ignore them.
- **U5. Undriven bus value.** What the MCU reads if the FPGA does not drive `dq` during a
  read (no pull configuration in the firmware). If it reads ones, an unresponsive core
  makes the status poll fail with an error rather than time out.
- **U6. Timing between operations.** Gaps between transactions (software chip select),
  pauses of the clock between phases of one transaction, chip-select setup and hold
  times, and the real granularity of the 5 ms sleep under FreeRTOS. All only lengthen the
  firmware's timing; none is relied on.
- **U7. Unaligned file tails.** A file whose size is not a multiple of the transfer word
  size is sent with a short last word (`core/files.rs:140-147`); how the official
  framework treats a partial word is not defined by the firmware.
- **U8. CTRL_VIBRATE meaning** (written 1 as the core starts) and the meaning and format
  of the INFO registers the firmware never reads.
- **U9. Startup after configuration.** Whether the FPGA has finished its startup sequence
  (global write enable released) when the first register write arrives, given the
  firmware stops clocking CCLK at the last bitstream byte. Assumed yes (the bitstream's
  trailing padding clocks startup).
- **U10. FreeRTOS scheduling.** The UI thread's overlay writes can interleave with the
  load sequence at any transaction boundary; their exact timing is not determined.
