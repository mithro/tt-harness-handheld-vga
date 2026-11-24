# First hardware run of the compare build, 2026-11-24

The compare build ran on a real FPGA for the first time. Two designs, each built with
Vivado and with openXC7, were loaded on an Arty A7-35T; each sent its 6,000,000 `uo_out`
samples over the board's UART at 1,000,000 baud. **All four recordings are `exact`**: byte
for byte the shared reference simulation, with a good frame CRC, on the first attempt.

## Board

- Digilent Arty A7-35T on the public fpgas.online PS1 site, host `pi7`
  (`ssh -p 10722 pi@ps1.fpgas.online`), Digilent USB serial `210319A764F5`, JTAG
  `idcode 0x362d093`.
- UART: `/dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0`
  (FT2232 channel B), read on the Raspberry Pi with pyserial 3.5.
- Before every run the board was checked for other users (`run.board_activity`): one other
  login, a `tmux` shell idle for 19 to 20 hours, nothing holding the UART, no loader or
  serial tool running. Not busy each time.

## Command

Per run, from this repository (`<P>` is the project, `<T>` the toolchain):

```
uv run tt-harness run-compare <P> --toolchain <T> --clocks 6000000 \
    --host ps1.fpgas.online --port 10722 \
    --uart /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0 \
    --known-hosts <file with the site's host key>
```

It builds the bitstream (`build.build_compare`, timing gate included) or reuses the one
already built for the same settings, then on the board host, in this order:

1. `python3 - probe <uart>`: the activity check (script on stdin; nothing is written).
2. `mkdir -m 700 /home/pi/tt-harness-run-<random>`, then `dd of=<dir>/board_host.py` and
   `dd of=<dir>/arty_compare.bit` (contents on stdin), then `sha256sum` of both.
3. `python3 <dir>/board_host.py receive --uart <uart> --baud 1000000 --header <hex>
   --length 6000013 --timeout 105.0 --idle-timeout 5.0 --out <dir>/recording.bin`, left
   running; it opens the UART, discards buffered input and prints `listening`.
4. After that line: `openFPGALoader -b arty <dir>/arty_compare.bit` (no sudo).
5. The receiver stops once 6,000,013 bytes from the frame header have arrived.
   `python3 <dir>/board_host.py send <dir>/recording.bin` returns them (gzip, base64,
   SHA-256 checked on arrival).
6. `rm -rf -- <dir>`.

The recording is decoded (`host.frame.decode`) and compared with the reference
(`verdict.compare`) exactly as `tt-harness verdict` does.

## Results

All four: 1,000,000 baud, 6,000,000 project clocks, reference span 6,000,000 clocks.

| # | Design | Toolchain | Bitstream | Started (UTC) | Load | Bytes received | Frame time | Run time | Frame CRC | Verdict | First differing clock |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `tt08/tt_um_tinytapeout_logo_screensaver` | Vivado 2025.2 | 2,192,137 bytes, sha256 `9b3197e15e10…` | 06:38:27 | 2.48 s | 6,000,013 | 60.07 s | 86.8 s | good | **exact** | none |
| 2 | `tt08/tt_um_tinytapeout_logo_screensaver` | openXC7 | 2,192,284 bytes, sha256 `47e9d7c7cab2…` | 06:40:29 | 2.48 s | 6,000,013 | 60.07 s | 85.9 s | good | **exact** | none |
| 3 | `ttsky26a/tt_um_algofoogle_dottee` | Vivado 2025.2 | 2,192,137 bytes, sha256 `5a71bd6eb5b5…` | 06:46:26 | 2.48 s | 6,000,013 | 60.07 s | 86.8 s | good | **exact** | none |
| 4 | `ttsky26a/tt_um_algofoogle_dottee` | openXC7 | 2,192,277 bytes, sha256 `7beab0169a56…` | 06:48:22 | 2.48 s | 6,000,013 | 60.07 s | 86.9 s | good | **exact** | none |

- "Load" is the `openFPGALoader` command as timed from here, SSH connection included.
  "Frame time" is from the header to the last byte at the receiver. "Run time" is the whole
  recording step: activity check, uploads, load, reception, fetch and clean-up.
- Each time the receiver got exactly 6,000,013 bytes, the header at offset 0, with no
  stale bytes in the UART buffer beforehand. The first byte arrived 2.47 s after the
  receiver reported it was listening.
- The Vivado builds read copies of three project sources each with `` `default_nettype
  none `` rewritten to `wire` (the expected Synth 8-6735 retry): `palette.v`,
  `tt_um_tinytapeout_logo_screensaver.v`, `vga_sync_generator.v`; and `audio.v`,
  `hvsync_generator.v`, `project.v`.
- dottee makes a clock in its own logic (pre-screen: 1 derived clock, flagged), so a
  difference would have been reported as `nondeterministic`. There was none.

### Checked independently of the verdict code

- The SHA-256 of each recording's 6,000,000 payload bytes equals the reference's
  `dump_sha256`: `84414107…acb2a5e0` (logo, both toolchains) and `55134628…f1e1b424`
  (dottee, both toolchains). The two toolchains' recordings of a design are the same file.
- Negative control: run 1's recording judged against dottee's reference gives a difference
  at clock 0 (`uo_out` 0x00 against 0x40), exit status 1. The comparison is not vacuous.

## What the hardware showed

- **1,000,000 baud is clean on this board's FT2232 at this length.** 24,000,052 bytes in
  four frames, every CRC-32 good. The kernel's error counters for the port
  (`TIOCGICOUNT`: frame, overrun, parity, break, buffer overrun) did not move during any
  frame, and its `rx` count advanced by exactly 6,000,013 each time.
- **The Pi keeps up.** A plain pyserial read loop, no overruns; a frame takes 60.07 s
  against 60.00 s of bit time (the receiver's timestamps are per read, not per byte).
- **Configuration.** The load takes 2.5 s and `openFPGALoader` ends with `done 1`. The
  design starts by itself at configuration (the top has no reset input) and the frame is
  sent once, so the receiver must be reading before the load; it was, and the header was
  the first byte seen.
- **The `done` LED (LD4) was not confirmed.** The site's camera shows the board in a dark
  room, out of focus. Frames taken across the end of run 2 show a reddish light in the
  board area go out between the frames stamped about 06:41:40 and 06:41:50 (mean red of
  the region 80 of 255 in five frames before, 20 in seven after); the UART frame was due
  to end at about 06:41:48. That fits the USB-serial activity LED stopping when the
  frame ends. LD4 itself cannot be told apart in the picture.
- The timings repeated to within 10 ms from run to run (2.48 s load, 2.47 s to the first
  byte, 60.07 s frame in all four).

## What was and was not shown

Shown:

- The compare build works on real hardware: the stepped project clock, the per-clock
  sampling, the UART framing and the CRC, built by either toolchain, reproduce the
  reference simulation exactly over 6,000,000 clocks for these two designs.
- For dottee that includes a design that clocks flip-flops from its own logic.
- The host-side runner loads only after the receiver is listening, and leaves the board
  host without files or processes.

Not shown:

- Anything at the project's real clock rate. The compare build steps the project clock at
  the UART byte rate (100 kHz here); timing at 25.175 MHz is a matter for the timing
  reports and the live build, not this test.
- More than one board, one site, one day: pi3's Arty was not used. Each bitstream was run
  once; repeatability across loads rests on four frames, not on repeats of one.
- Other baud rates. None was needed; 1,000,000 was not pushed further.
- The `done` LED, as above.
- Any design beyond these two, and nothing about `uio` outputs, which the frame does not
  carry.

## State the board was left in

- FPGA: the run 4 bitstream (dottee, openXC7) stays loaded until someone loads another or
  the board loses power. It finished its frame and is idle (`done`); it sends nothing more.
- Board host: no `tt-harness-run-*` directory in `/home/pi`, no process of ours, the UART
  unheld, disk use unchanged (3.2 MB of 458 MB). Nothing was installed or configured and
  `sudo` was not used.

## Files

The recordings and their measurements are in `work/runs/` (git-ignored):
`<name>.bin` and `<name>.bin.json` for `logo-vivado-1`, `logo-openxc7-1`,
`dottee-vivado-1`, `dottee-openxc7-1`; console logs and the verdict JSON in `tmp/runs/`.
