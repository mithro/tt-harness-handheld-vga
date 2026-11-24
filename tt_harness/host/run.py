# SPDX-License-Identifier: Apache-2.0
"""Run a compare build on a real board and bring back what its UART sent.

The board is reached through its host (a Raspberry Pi with the FPGA board on USB) over
OpenSSH. One recording is, in order:

1. `python3 - probe UART` (the script on stdin): who is using the host. A busy board is
   refused and nothing is uploaded.
2. `mkdir -m 700 /home/<user>/tt-harness-run-<random>`; the script (`board_host.py`) and the
   bitstream are written there with `dd of=...` and checked with `sha256sum`.
3. `python3 <dir>/board_host.py receive ...` is started and left running. It opens the UART,
   discards what is already buffered and prints a `listening` line.
4. Only after that line: `openFPGALoader <loader_args> <dir>/<bitstream>`. The compare build
   has no reset input; it starts sending its one frame as soon as it is configured, so the
   receiver has to be reading first.
5. The receiver stops when the whole frame has arrived (or on its timeouts) and writes the
   raw bytes to `<dir>/recording.bin`; `python3 <dir>/board_host.py send` returns them
   gzipped and base64-encoded, with a SHA-256 that is checked here.
6. `rm -rf -- <dir>`, whatever happened. A receiver still running sees its directory go and
   stops, so nothing is left behind and no process has to be killed by number.

Every remote command is a list of words. Each word must match `SAFE_WORD` (no quote, space,
`$`, `;` or other shell syntax), and is quoted as well before OpenSSH hands the line to the
remote shell. Board fields, the bitstream's file name and the UART path are checked before
anything is run.

Host keys are checked as the user's OpenSSH configuration says; a known-hosts file given to
`SshRunner` makes the check stricter (only keys in that file), never looser.
"""

from __future__ import annotations

import json
import queue
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tt_harness.host import board_host

REMOTE_SCRIPT = Path(board_host.__file__)
PREFIX = board_host.PREFIX
ACTIVE_WINDOW_S = 600  # terminal activity this recent means someone is using the board
IDLE_TIMEOUT_S = 5.0  # a started frame that goes quiet this long has lost bytes
COMMAND_TIMEOUT_S = 60.0
LOAD_TIMEOUT_S = 180.0
LISTEN_TIMEOUT_S = 30.0

SAFE_WORD = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_UART = re.compile(r"/dev/serial/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*")
_BITSTREAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.bit")
_LOADER_ARG = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)$")


class RunError(RuntimeError):
    """A recording could not be made."""


class BoardBusy(RunError):
    """Someone else is using the board; `reasons` says what was seen."""

    def __init__(self, board: Board, reasons: tuple[str, ...]) -> None:
        super().__init__(f"{board.host}:{board.port} is in use: {'; '.join(reasons)}")
        self.reasons = reasons


@dataclass(frozen=True)
class Board:
    host: str
    port: int
    uart: str  # a /dev/serial/by-id/... path on the board host
    user: str = "pi"
    loader_args: tuple[str, ...] = ("-b", "arty")


@dataclass(frozen=True)
class Activity:
    busy: bool
    reasons: tuple[str, ...]  # why the board is busy; empty when it is not
    notes: tuple[str, ...] = ()  # what else was seen, and what could not be


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes


class Stream(Protocol):
    """A remote command left running, whose `TTHR` lines can be waited for."""

    def expect(self, kind: str, timeout: float) -> dict: ...
    def wait(self, timeout: float) -> int: ...
    def kill(self) -> None: ...


class Runner(Protocol):
    """Runs commands, given as lists of words, on the board host."""

    def run(self, argv: Sequence[str], *, input: bytes | None = None, timeout: float) -> Result: ...
    def start(self, argv: Sequence[str]) -> Stream: ...


def check_board(board: Board) -> None:
    """Raise ValueError unless every field of `board` is plainly what it should be."""
    if not isinstance(board.host, str) or not _HOST.fullmatch(board.host):
        raise ValueError(f"board host {board.host!r} is not a host name or address")
    if type(board.port) is not int or not 1 <= board.port <= 65535:
        raise ValueError(f"board port {board.port!r} is not 1..65535")
    if not isinstance(board.user, str) or not _USER.fullmatch(board.user):
        raise ValueError(f"board user {board.user!r} is not a plain user name")
    if not isinstance(board.uart, str) or not _UART.fullmatch(board.uart) or ".." in board.uart:
        raise ValueError(
            f"UART {board.uart!r} is not a /dev/serial/by-id/<name> path of plain characters"
        )
    for arg in board.loader_args:
        if not isinstance(arg, str) or not _LOADER_ARG.fullmatch(arg):
            raise ValueError(f"loader argument {arg!r} has unexpected characters")


def _checked(argv: Sequence[str]) -> list[str]:
    words = list(argv)
    for word in words:
        if not isinstance(word, str) or not SAFE_WORD.fullmatch(word):
            raise ValueError(f"refusing remote command word {word!r} in {words!r}")
    return words


def _text(data: bytes) -> str:
    return data.decode(errors="replace").replace("\0", "")


def _marked(output: bytes, kind: str) -> dict | None:
    """The JSON object of the first `TTHR <kind> {...}` line, or None."""
    lead = f"{PREFIX}{kind} "
    for line in _text(output).splitlines():
        if line.startswith(lead):
            try:
                doc = json.loads(line[len(lead) :])
            except ValueError:
                return None
            return doc if isinstance(doc, dict) else None
    return None


# OpenSSH ------------------------------------------------------------------------------


class _SshStream:
    def __init__(self, cmd: list[str]) -> None:
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._seen: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for raw in self._proc.stdout:
            self._lines.put(_text(raw).rstrip("\r\n"))
        self._lines.put(None)

    def expect(self, kind: str, timeout: float) -> dict:
        lead = f"{PREFIX}{kind} "
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise RunError(
                    f"no '{kind}' line within {timeout:.0f} s; output so far: {self._seen[-5:]}"
                ) from None
            if line is None:
                self._lines.put(None)
                raise RunError(
                    f"the remote command ended without a '{kind}' line: {self._seen[-5:]}"
                )
            self._seen.append(line)
            if line.startswith(lead):
                try:
                    doc = json.loads(line[len(lead) :])
                except ValueError as e:
                    raise RunError(f"unreadable '{kind}' line {line!r}: {e}") from e
                if not isinstance(doc, dict):
                    raise RunError(f"unreadable '{kind}' line {line!r}")
                return doc

    def wait(self, timeout: float) -> int:
        try:
            return self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise RunError(f"the remote command did not end within {timeout:.0f} s") from None

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc.stdout.close()


class SshRunner:
    """Runs each command with `ssh`, non-interactively."""

    def __init__(
        self, board: Board, known_hosts: Path | None = None, connect_timeout_s: int = 15
    ) -> None:
        check_board(board)
        self.board = board
        self._options = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(connect_timeout_s)}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
        ]
        if known_hosts is not None:
            known_hosts = Path(known_hosts).resolve()
            if not known_hosts.is_file():
                raise ValueError(f"known-hosts file {known_hosts} does not exist")
            if not re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", str(known_hosts)):
                # OpenSSH splits this option's value on spaces and expands % and ~.
                raise ValueError(f"known-hosts path {str(known_hosts)!r} has unexpected characters")
            self._options += [
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "StrictHostKeyChecking=yes",
            ]

    def command(self, argv: Sequence[str]) -> list[str]:
        """The local command line: ssh options, the destination, one quoted remote command.

        OpenSSH passes the remote command to the user's shell as one string; quoting every
        word makes that shell see exactly `argv`.
        """
        remote = " ".join(shlex.quote(word) for word in argv)
        return [
            "ssh",
            *self._options,
            "-p",
            str(self.board.port),
            "--",
            f"{self.board.user}@{self.board.host}",
            remote,
        ]

    def run(self, argv: Sequence[str], *, input: bytes | None = None, timeout: float) -> Result:
        try:
            done = subprocess.run(
                self.command(argv),
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise RunError(f"{argv[0]} on the board host did not end within {timeout:.0f} s") from e
        return Result(done.returncode, done.stdout, done.stderr)

    def start(self, argv: Sequence[str]) -> _SshStream:
        return _SshStream(self.command(argv))


# who is using the board ---------------------------------------------------------------


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def judge(probe: dict) -> Activity:
    """Decide from a `board_host.probe` document whether the board is in use."""
    now = float(probe["now"])
    reasons, notes = [], []
    if not probe["uart_exists"]:
        reasons.append(f"the UART {probe['uart']} does not exist on the board host")
    for process in probe["processes"]:
        reasons.append(
            f"process {process['pid']} ({process['user']}) {' and '.join(process['why'])}: "
            f"{process['args']}"
        )
    for terminal in probe["terminals"]:
        idle = now - max(float(terminal["read"]), float(terminal["written"]))
        who = f"terminal {terminal['name']} ({terminal['user']})"
        if idle < ACTIVE_WINDOW_S:
            reasons.append(
                f"{who} was active {_ago(max(idle, 0))} ago, within the last "
                f"{ACTIVE_WINDOW_S // 60} minutes"
            )
        else:
            notes.append(f"{who} is open but idle for {_ago(idle)}: a login, not use")
    if not probe["terminals"]:
        notes.append("no terminal sessions are open")
    if not probe["processes"]:
        notes.append(
            f"no process holds {probe['uart_device']} or runs {', '.join(board_host.TOOLS)}"
        )
    if probe["uninspected"]:
        notes.append(
            f"the open files of {probe['uninspected']} processes of other users could not be "
            "read; they were checked by name only"
        )
    return Activity(bool(reasons), tuple(reasons), tuple(notes))


def board_activity(board: Board, runner: Runner | None = None) -> Activity:
    """Look at the board host and say whether someone is using the board, and why.

    Busy: a terminal with input or output in the last ten minutes, a process with the UART
    (or the same adapter's JTAG serial port) open, or one running openFPGALoader, picocom,
    tio or minicom. An open login that has been idle is not use. If the check itself fails
    or cannot be read, the board is reported busy, with the failure as the reason.
    """
    check_board(board)
    runner = runner or SshRunner(board)
    argv = _checked(["python3", "-", "probe", board.uart])
    try:
        result = runner.run(argv, input=REMOTE_SCRIPT.read_bytes(), timeout=COMMAND_TIMEOUT_S)
    except RunError as e:
        return Activity(True, (f"the activity check failed: {e}",))
    if result.returncode != 0:
        said = " ".join((_text(result.stderr) + _text(result.stdout)).split())[-300:]
        return Activity(
            True, (f"the activity check failed with status {result.returncode}: {said}",)
        )
    probe = _marked(result.stdout, "probe")
    try:
        if probe is None:
            raise KeyError("no probe line")
        return judge(probe)
    except (KeyError, TypeError, ValueError) as e:
        return Activity(True, (f"the activity check gave no usable answer ({e!r})",))
