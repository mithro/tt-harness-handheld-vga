# SPDX-License-Identifier: Apache-2.0
"""A fake board host for the hardware-runner tests: no network and no board."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import zlib

from tt_harness.compare.recorder import header
from tt_harness.host import board_host, run

UART = "/dev/serial/by-id/usb-Digilent_Digilent_USB_Device_210319A764F5-if01-port0"
BOARD = run.Board(host="ps1.fpgas.online", port=10722, uart=UART)
PAYLOAD = bytes((i * 7 + 3) & 0xFF for i in range(5000))
NOW = 1_795_500_000.0


def frame(payload: bytes) -> bytes:
    return header(len(payload)) + payload + struct.pack("<I", zlib.crc32(payload))


def probe(terminals=(), processes=(), **fields) -> dict:
    doc = {
        "now": NOW,
        "uart": UART,
        "uart_exists": True,
        "uart_device": "/dev/ttyUSB1",
        "terminals": list(terminals),
        "processes": list(processes),
        "uninspected": 40,
    }
    doc.update(fields)
    return doc


def terminal(idle_s: float, name: str = "/dev/pts/2") -> dict:
    return {"name": name, "user": "pi", "read": NOW - idle_s - 5, "written": NOW - idle_s}


IDLE = probe(terminals=[terminal(70_000)])
BANNER = b"ship7ohT\n\x00\x00"  # what a login may print before the command's own output


def line(kind: str, doc: dict) -> bytes:
    return f"TTHR {kind} {json.dumps(doc)}\n".encode()


class FakeStream:
    def __init__(self, host: FakeHost, argv: list[str]) -> None:
        self.host = host
        self.argv = argv
        self.killed = False

    def expect(self, kind: str, timeout: float) -> dict:
        self.host.events.append(("expect", kind))
        if kind == "listening":
            if not self.host.listens:
                raise run.RunError("receiver: no 'listening' line: ModuleNotFoundError: serial")
            return {"uart": UART, "baud": 1_000_000, "stale_bytes": 0}
        assert kind == "received"
        data = self.host.uart_bytes if self.host.loaded else b""
        out = self.argv[self.argv.index("--out") + 1]
        self.host.files[out] = data
        length = int(self.argv[self.argv.index("--length") + 1])
        at = data.find(bytes.fromhex(self.argv[self.argv.index("--header") + 1]))
        complete = at >= 0 and len(data) - at >= length
        self.host.receiver_rc = 0 if complete else board_host.EXIT_INCOMPLETE
        return {
            "bytes": len(data),
            **board_host.pre_header(data, at),
            "complete": complete,
            "stopped": "complete" if complete else "timeout",
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def wait(self, timeout: float) -> int:
        self.host.events.append(("wait",))
        return self.host.receiver_rc

    def kill(self) -> None:
        self.host.events.append(("kill",))
        self.killed = True


class FakeHost:
    """Stands in for `run.SshRunner`: a board host with files, a UART and a loader."""

    def __init__(self, probe: dict = IDLE, uart_bytes: bytes = b"") -> None:
        self.probe = probe
        self.uart_bytes = uart_bytes
        self.events: list[tuple] = []
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.listens = True
        self.loader_rc = 0
        self.loaded = False
        self.receiver_rc = 0
        self.corrupt_fetch = False
        self.streams: list[FakeStream] = []

    def start(self, argv):
        argv = list(argv)
        self.events.append(("start", argv))
        self.streams.append(FakeStream(self, argv))
        return self.streams[-1]

    def run(self, argv, *, input=None, timeout):
        argv = list(argv)
        self.events.append(("run", argv))
        out = b""
        rc = 0
        if argv[:3] == ["python3", "-", "probe"]:
            assert input == run.REMOTE_SCRIPT.read_bytes()
            out = line("probe", self.probe)
        elif argv[0] == "mkdir":
            assert argv[-1] not in self.dirs
            self.dirs.add(argv[-1])
        elif argv[0] == "dd":
            (path,) = (a.removeprefix("of=") for a in argv if a.startswith("of="))
            assert os.path.dirname(path) in self.dirs
            self.files[path] = input
        elif argv[0] == "sha256sum":
            out = b"".join(
                f"{hashlib.sha256(self.files[p]).hexdigest()}  {p}\n".encode() for p in argv[1:]
            )
        elif argv[0] == "openFPGALoader":
            assert argv[-1] in self.files
            rc = self.loader_rc
            self.loaded = rc == 0
            out = b"Load SRAM: [====] 100.00%\nDone\n" if rc == 0 else b"JTAG init failed\n"
        elif argv[0] == "python3" and argv[2] == "send":
            data = self.files[argv[3]]
            if self.corrupt_fetch:
                data = data[:-1] + bytes([data[-1] ^ 1])
            out = "".join(f"{line}\n" for line in board_host.sent_lines(data)).encode()
        elif argv[:3] == ["rm", "-rf", "--"]:
            self.dirs.discard(argv[3])
            self.files = {p: d for p, d in self.files.items() if not p.startswith(argv[3] + "/")}
        else:
            raise AssertionError(f"unexpected remote command {argv}")
        return run.Result(rc, BANNER + out, b"")

    def index(self, what: str) -> int:
        """Position of the first command containing the word `what`, or `expect` of it."""
        for i, event in enumerate(self.events):
            if event[0] == "expect" and event[1] == what:
                return i
            if event[0] in ("run", "start") and what in event[1]:
                return i
        raise AssertionError(f"no {what} in {self.events}")

    def ran(self, what: str) -> bool:
        return any(e[0] in ("run", "start") and what in e[1] for e in self.events)
