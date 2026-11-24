# Arty A7 boards on fpgas.online

## Survey of the Welland site, 2026-11-23: no Arty is live

A read-only survey (nothing loaded, probed over JTAG or written remotely) found no Arty on
any reachable Raspberry Pi. All 23 Pis with a DHCP lease on `tweed` were checked with
`lsusb`; none shows the Arty's FTDI device (`0403:6010`). The fleet page
(`https://welland.fpgas.online/fleet/`) lists six Arty A7-35 boards, all "missing" for 36
to 39 days and "on no port".

| Arty DNA | Host Pi serial | Last port | USB serial (from old docs) |
|---|---|---|---|
| 0x0004e4897221085c | 0000000080863963 (3B+) | sw2 p11 | unknown |
| 0x00628502251ea85c | 10000000ce8e3593 (4B) | sw2 p24 | 210319B301DE |
| 0x006cc522278a2854 | 100000008483b266 (4B) | sw2 p37 | unknown |
| 0x0074c522278a2854 | 10000000302bb73d (4B) | sw2 p48 | unknown |
| 0x0144cd2a47442854 | 10000000613a4524 (4B) | sw2 p48 | 210319B0C238 |
| 0x002c8d02251ea854 | 10000000f77b8415 (4B) | sw2 p48 | 210319B3E5C5 |

The ports in the October 2026 documents (sw2 p9, p10, p12, p15) now hold Tiny Tapeout
boards (p9, p10), a Pi that does not finish booting (p12), or nothing (p15). One
unregistered port, sw2 p30, has link and draws 5.0 W but did not answer SSH.

Not verified: where the boards physically are and whether they are powered.

## Using an Arty once one is back (from the documents; untested)

`<s>.<p>` is the switch and port.

```
ssh -J tweed.welland.mithis.com pi@10.21.<s>.<p> 'cat > /home/pi/x.bit' < x.bit
ssh -J tweed.welland.mithis.com pi@10.21.<s>.<p> 'sudo flock -n /run/fpgas-online/arty.lock openFPGALoader -b arty /home/pi/x.bit'
ssh -t -J tweed.welland.mithis.com pi@10.21.<s>.<p> 'picocom -b 115200 /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_<SN>-if01-port0'
```

- The UART is `/dev/ttyUSB1`; JTAG is interface 0.
- The Pi image has openFPGALoader, picocom, tio, minicom and pyserial. Its root is a
  tmpfs overlay, so uploads vanish on reboot.
- `/run/fpgas-online/arty.lock` is taken with `flock` by `fpgas-verify` and
  `fpgas-arty-debug`; take it for every load.
- PMOD HAT wiring differs between boards (three boards: HAT JA to Arty JC, JB to JD, JC to
  JB; one: JA to JC, JB to JB, JC to JD; one reads nothing). Arty JA is free on all.

Raw logs of the survey are in `tmp/arty-survey/` (not committed).

## Survey of the PS1 site, 2026-11-24: three hosts are up

Checked from outside, without logging in: each host's public page, whether its SSH port
answers with a banner, and whether its camera stream playlist is being updated.

| Host | SSH port on `ps1.fpgas.online` | Camera stream | State |
|---|---|---|---|
| pi3 | 10322 answers (OpenSSH banner) | live, segments seconds old | up |
| pi7 | 10722 answers | live, segments seconds old | up |
| pi21 | 12122 answers | live, segments seconds old | up |
| pi2, pi5, pi9, pi11, pi13 | no route to host | 404 | down |
| pi23 | timed out | 404 | down |
| pi17 | no page (404) | n/a | not registered |

- The site's notes list pi2, pi3, pi5, pi7, pi9, pi11, pi13 and pi17 as Arty A7-35T hosts;
  pi21 and pi23 are not in that list, so pi21's board type is unconfirmed.
- "Up" means the Raspberry Pi answers and its camera is streaming. Whether the Arty is
  attached and programmable was not checked, because that needs a login.
- Access is by SSH as user `pi` on the port above (the site's own instructions; the
  password is shown in the login banner) or through each host's web page, which has a
  file upload. This is the public service: other people may be using a board at any time.

### Logged-in check of the PS1 hosts, 2026-11-24

With the owner's permission to use any fpgas.online device, a read-only login (existing SSH
key, no password needed) found:

| Host | Board | Other logins at the time | Notes |
|---|---|---|---|
| pi7 | Arty A7-35T, Digilent USB serial 210319A764F5 | none | JTAG identify: `idcode 0x362d093`, `family artix a7 35t` |
| pi3 | Arty (FTDI 0403:6010), Digilent USB serial 210319A43AD3 | one other session, three hours old | not probed over JTAG because someone else was logged in |
| pi21 | a Tiny Tapeout board (MicroPython USB device), not an Arty | three sessions from the site's own gateway | not useful for this project |

- JTAG is `/dev/ttyUSB0` (interface 0) and the UART `/dev/ttyUSB1`; stable names are under
  `/dev/serial/by-id/`.
- Each host has `openFPGALoader`, `picocom`, `tio` and Python with pyserial 3.5.
- The root filesystem is a 458 MB RAM overlay with about 450 MB free, and the Pi has about
  730 MB of memory available; a 2.2 MB bitstream and a 6 MB recording fit, but keep
  uploads small and delete them afterwards.
- There is no `/run/fpgas-online/` lock at PS1, so nothing serialises users. Check `who`
  and for processes holding the serial ports before each use.

pi7 is the first choice for test runs.

### Deciding whether a board is in use

A login is not evidence of use: sessions stay open when nobody is active (owner's
instruction, 2026-11-24). Before treating a board as busy, look for activity:

- `w` for each session's idle time, and the last-activity time of each terminal
  (`stat -c %y /dev/pts/N`);
- processes holding the serial ports or running `openFPGALoader`, `picocom`, `tio`;
- how recently anything changed in the home directory.

Rechecked this way, pi3's other session turned out to be idle: opened three minutes after
the host booted, a `tmux` session with a bare shell, no terminal activity in the three
hours since, and nothing holding the serial ports. pi3 is therefore usable too. If a
session shows activity within the last several minutes, or a tool is running, leave the
board alone and use another.

`tt_harness.host.run.board_activity` makes this check (terminal activity within ten
minutes, a process with the UART or the adapter's JTAG serial port open, or one running
`openFPGALoader`, `picocom`, `tio` or `minicom`) and `tt-harness run-compare` refuses a busy
board before uploading anything.

### What the first hardware runs found, 2026-11-24 (pi7)

The runs themselves are in `docs/results/2026-11-first-hardware-run.md`. About the hosts:

- **Host key.** `ps1.fpgas.online` ports 10322, 10722 and 12122 present one ED25519 key,
  `SHA256:pDFcD3r8o4wazB5vGN24X8mWlcWtrRBh5zoUcw5+jfI`. It is not in the user's
  `~/.ssh/known_hosts`, so a batch-mode `ssh` fails with "Host key verification failed"
  until it is added there or a known-hosts file holding it is given
  (`tt-harness run-compare --known-hosts FILE`, which also sets
  `StrictHostKeyChecking=yes`).
- **Login.** Every user is `pi`; the login shell is `zsh`, so a command line is parsed by
  zsh (an unmatched glob is an error and ends the command). The line of text printed at
  login (the site's password) is the SSH banner: it arrives on stderr, not on stdout.
- **No sudo is needed.** `pi` is in `plugdev`; `openFPGALoader -b arty FILE` and
  `openFPGALoader -b arty --detect` work as `pi`, and the UART `/dev/ttyUSB1` is
  `crw-rw-r-- root plugdev`.
- **Loading** a 2.2 MB bitstream with `openFPGALoader -b arty` takes 2.5 s including the
  SSH connection, and ends with `done 1`. The design runs as soon as it is configured: a
  build that transmits from configuration had its first byte at the receiver 2.47 s
  after the receiver reported it was listening, the load having been started right
  after that report.
- **UART at 1,000,000 baud is clean.** Four frames of 6,000,013 bytes each (60 s each)
  arrived with a good CRC-32 and the kernel's counters (`TIOCGICOUNT` on the open port:
  frame, overrun, parity, break, buffer overrun) all unchanged; `rx` advanced by exactly
  the frame length. A plain pyserial read loop on the Pi keeps up.
  `/proc/tty/driver/` is readable only by root, so `TIOCGICOUNT` is the way to read the
  counters.
- **Tools.** `fuser`, `gzip`, `timeout`, `setsid`, `sha256sum` and Python 3.13 with
  pyserial 3.5 are there; `lsof` is not. The open files of other users' processes cannot
  be read without root, but every login is `pi`, so a UART held by another session shows.
- **Home directory.** `/home/pi` holds other people's files (`Demos/`, `Uploads/`, a
  `.bit`); the runner works in its own `/home/pi/tt-harness-run-<random>/` (mode 700) and
  removes it. The filesystem is RAM: a 2.2 MB bitstream plus a 6 MB recording is fine.
- **Clock.** The Pi's clock is UTC and agreed with this machine to within a few seconds.
- **Camera.** `https://ps1.fpgas.online/live/pi7.m3u8` shows the board with a clock
  overlay (UTC) and lags by about 35 s. On 2026-11-24 the room was dark and the picture
  out of focus: individual LEDs cannot be told apart, though the light of the USB-serial
  activity LED going out when a frame ends is measurable.
- **State left.** After a run the last bitstream loaded stays in the FPGA until someone
  loads another or the board loses power; there is no earlier state to restore.
