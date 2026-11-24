# SPDX-License-Identifier: Apache-2.0
"""The script `tt_harness.host.run` runs on a board host (the Raspberry Pi beside the FPGA).

It is uploaded as a file and run with the host's own `python3`, so it uses only the standard
library and, for `receive`, pyserial. Every line meant for the runner starts with `TTHR `;
anything else on the output (a login banner, a warning) is ignored there.

    probe UART              facts about who is using the host, as one JSON line
    receive ...             read the UART into a file until a whole frame has arrived
    send FILE               print FILE gzipped and base64-encoded between two marker lines

`probe` only reports; whether the board counts as busy is decided by the runner.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import gzip
import hashlib
import json
import os
import pwd
import struct
import sys
import time
import zlib

PREFIX = "TTHR "
TOOLS = ("openFPGALoader", "picocom", "tio", "minicom")
TIOCGICOUNT = 0x545D
ICOUNT_FIELDS = (
    "cts",
    "dsr",
    "rng",
    "dcd",
    "rx",
    "tx",
    "frame",
    "overrun",
    "parity",
    "brk",
    "buf_overrun",
)
EXIT_INCOMPLETE = 3


def say(kind: str, doc: dict) -> None:
    print(f"{PREFIX}{kind} {json.dumps(doc, sort_keys=True)}", flush=True)


# probe --------------------------------------------------------------------------------


def _user(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _ancestors(pid: int) -> set[int]:
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                # The command name may contain spaces and parentheses; the parent follows
                # the last ")".
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return seen


def probe(uart: str) -> dict:
    """Terminals, serial-tool processes and processes with the UART open, as plain facts."""
    terminals = []
    try:
        names = sorted(n for n in os.listdir("/dev/pts") if n.isdigit())
    except OSError:
        names = []
    for name in names:
        try:
            st = os.stat(f"/dev/pts/{name}")
        except OSError:
            continue
        terminals.append(
            {
                "name": f"/dev/pts/{name}",
                "user": _user(st.st_uid),
                "read": st.st_atime,  # the last keystroke read from the terminal
                "written": st.st_mtime,  # the last output written to it
            }
        )

    uart_real = os.path.realpath(uart)
    uart_exists = os.path.exists(uart)
    # The JTAG side of the same adapter shows up as the if00 serial port.
    devices = {uart_real}
    if "-if01-" in uart:
        jtag = uart.replace("-if01-", "-if00-")
        if os.path.exists(jtag):
            devices.add(os.path.realpath(jtag))

    mine = _ancestors(os.getpid())
    processes = []
    uninspected = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in mine:
            continue
        base = f"/proc/{entry}"
        try:
            with open(f"{base}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
            with open(f"{base}/comm") as f:
                comm = f.read().strip()
            uid = os.stat(base).st_uid
        except OSError:
            continue  # the process ended meanwhile
        if not args:
            continue  # a kernel thread
        why = []
        names = {comm, os.path.basename(args[0])}
        tool = next((t for t in TOOLS if t in names), None)
        if tool:
            why.append(f"runs {tool}")
        try:
            for fd in os.listdir(f"{base}/fd"):
                try:
                    target = os.readlink(f"{base}/fd/{fd}")
                except OSError:
                    continue
                if target in devices:
                    why.append(f"has {target} open")
                    break
        except OSError:
            uninspected += 1
        if why:
            processes.append(
                {"pid": int(entry), "user": _user(uid), "args": " ".join(args)[:200], "why": why}
            )
    return {
        "now": time.time(),
        "uart": uart,
        "uart_exists": uart_exists,
        "uart_device": uart_real,
        "terminals": terminals,
        "processes": processes,
        "uninspected": uninspected,
    }


# receive ------------------------------------------------------------------------------


def icount(fd: int) -> dict | None:
    """The kernel's error counters for a serial port, or None if the driver has none."""
    try:
        raw = fcntl.ioctl(fd, TIOCGICOUNT, bytes(80))
    except OSError:
        return None
    return dict(zip(ICOUNT_FIELDS, struct.unpack("11i", raw[:44]), strict=True))


def receive(port, header: bytes, length: int, timeout_s: float, idle_s: float, **hooks) -> dict:
    """Read `port` until `length` bytes starting at `header` have arrived.

    Returns facts about the reception, with the bytes under "data". Reading stops when the
    frame is whole, when `timeout_s` has passed since the start, when nothing has arrived
    for `idle_s` after the header was seen (the frame is sent once, so a gap that long means
    bytes were lost), or when `alive()` turns false. Everything received is kept, including
    bytes before the header: the host-side decoder is what judges the recording.
    """
    clock = hooks.get("clock", time.monotonic)
    alive = hooks.get("alive", lambda: True)
    data = bytearray()
    start = clock()
    last = start
    first_byte_s = header_s = None
    header_at = -1
    stopped = "timeout"
    while True:
        now = clock()
        if header_at >= 0 and len(data) - header_at >= length:
            stopped = "complete"
            break
        if now - start >= timeout_s:
            break
        if header_at >= 0 and now - last >= idle_s:
            stopped = "idle"
            break
        if not alive():
            stopped = "abandoned"
            break
        chunk = port.read(max(1, min(65536, getattr(port, "in_waiting", 1))))
        if not chunk:
            continue
        last = clock()
        if first_byte_s is None:
            first_byte_s = last - start
        searched = max(0, len(data) - len(header) + 1)
        data += chunk
        if header_at < 0:
            header_at = data.find(header, searched)
            if header_at >= 0:
                header_s = last - start
    complete = stopped == "complete"
    crc_ok = None
    if complete:
        payload = data[header_at + len(header) : header_at + length - 4]
        (crc,) = struct.unpack_from("<I", data, header_at + length - 4)
        crc_ok = crc == zlib.crc32(payload)
    return {
        "data": bytes(data),
        "bytes": len(data),
        "header_at": header_at,
        "after_header": len(data) - header_at if header_at >= 0 else 0,
        "wanted": length,
        "complete": complete,
        "crc_ok": crc_ok,
        "stopped": stopped,
        "first_byte_s": first_byte_s,
        "header_s": header_s,
        "last_byte_s": last - start,
        "elapsed_s": clock() - start,
    }


def _cmd_receive(args: argparse.Namespace) -> int:
    import serial  # pyserial, present on the board host; not needed for probe or send

    header = bytes.fromhex(args.header)
    here = os.path.dirname(os.path.abspath(__file__))
    port = serial.Serial(args.uart, args.baud, timeout=0.2)
    try:
        before = icount(port.fileno())
        # Whatever arrived before now is not this run's frame.
        time.sleep(0.2)
        stale = port.in_waiting
        port.reset_input_buffer()
        say("listening", {"uart": args.uart, "baud": port.baudrate, "stale_bytes": stale})
        # The runner removes this script's directory when it gives up; stop then.
        info = receive(
            port,
            header,
            args.length,
            args.timeout,
            args.idle_timeout,
            alive=lambda: os.path.isdir(here),
        )
        after = icount(port.fileno())
    finally:
        port.close()
    data = info.pop("data")
    if info["stopped"] != "abandoned":
        with open(args.out, "wb") as f:
            f.write(data)
    info["sha256"] = hashlib.sha256(data).hexdigest()
    info["icount_before"] = before
    info["icount_after"] = after
    say("received", info)
    return 0 if info["complete"] else EXIT_INCOMPLETE


# send ---------------------------------------------------------------------------------


def sent_lines(data: bytes):
    """The lines `send` prints for `data`: a begin marker, base64 of its gzip, an end marker."""
    doc = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    yield f"{PREFIX}begin {json.dumps(doc, sort_keys=True)}"
    text = base64.b64encode(gzip.compress(data, 6)).decode()
    for i in range(0, len(text), 76):
        yield text[i : i + 76]
    yield f"{PREFIX}end {{}}"


def _cmd_send(args: argparse.Namespace) -> int:
    with open(args.file, "rb") as f:
        data = f.read()
    for line in sent_lines(data):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="board_host.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("uart")
    p.set_defaults(func=lambda a: say("probe", probe(a.uart)) or 0)
    r = sub.add_parser("receive")
    r.add_argument("--uart", required=True)
    r.add_argument("--baud", type=int, required=True)
    r.add_argument("--header", required=True, help="the frame's first bytes, in hex")
    r.add_argument("--length", type=int, required=True, help="frame bytes, from the header on")
    r.add_argument("--timeout", type=float, required=True)
    r.add_argument("--idle-timeout", type=float, required=True)
    r.add_argument("--out", required=True)
    r.set_defaults(func=_cmd_receive)
    s = sub.add_parser("send")
    s.add_argument("file")
    s.set_defaults(func=_cmd_send)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
