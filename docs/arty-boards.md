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
