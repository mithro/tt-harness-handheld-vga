# SPDX-License-Identifier: Apache-2.0
"""Recording from a board, against a fake board host: no network and no board."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from board_fakes import BANNER, BOARD, PAYLOAD, UART, FakeHost, frame, probe, terminal

from tt_harness import verdict
from tt_harness.compare.recorder import frame_length, header
from tt_harness.host import board_host, run
from tt_harness.host import frame as host_frame


@pytest.fixture
def bitstream(tmp_path) -> Path:
    path = tmp_path / "arty_compare.bit"
    path.write_bytes(b"\xff" * 64 + b"bitstream")
    return path


def _record(host: FakeHost, bitstream: Path, tmp_path: Path, clocks: int = len(PAYLOAD)) -> Path:
    return run.record(
        BOARD, bitstream, clocks, 1_000_000, tmp_path / "rec.bin", timeout_s=5, runner=host
    )


# record: ordering --------------------------------------------------------------------


def test_record_loads_only_after_the_receiver_is_listening(bitstream, tmp_path):
    recording = b"\x00stale" + frame(PAYLOAD)
    host = FakeHost(uart_bytes=recording)
    out = _record(host, bitstream, tmp_path)
    assert out == tmp_path / "rec.bin"
    assert out.read_bytes() == recording
    assert host_frame.decode(out.read_bytes()).ok

    probe, start = host.index("probe"), host.index("receive")
    listening, load = host.index("listening"), host.index("openFPGALoader")
    assert probe < start < listening < load < host.index("received") < host.index("send")
    # The receiver is told the UART, the rate and how much to wait for.
    (_, argv) = host.events[start]
    assert argv[argv.index("--uart") + 1] == UART
    assert argv[argv.index("--baud") + 1] == "1000000"
    assert argv[argv.index("--length") + 1] == str(frame_length(len(PAYLOAD)))
    assert argv[argv.index("--header") + 1] == header(len(PAYLOAD)).hex()
    # The loader gets the board's arguments and the uploaded copy of the bitstream.
    (_, load_argv) = host.events[load]
    assert load_argv[:3] == ["openFPGALoader", "-b", "arty"]
    assert load_argv[3].endswith("/arty_compare.bit")


def test_record_writes_what_the_run_measured_beside_the_recording(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    out = _record(host, bitstream, tmp_path)
    meta = json.loads(run.meta_path(out).read_text())
    assert meta["receiver"]["complete"] is True
    assert meta["bitstream_sha256"] == hashlib.sha256(bitstream.read_bytes()).hexdigest()
    assert meta["load_s"] >= 0
    assert "Done" in meta["loader_output"]
    assert meta["activity"]["busy"] is False


def test_loader_is_not_run_when_the_receiver_never_listens(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    host.listens = False
    with pytest.raises(run.RunError, match="listening"):
        _record(host, bitstream, tmp_path)
    assert not host.ran("openFPGALoader")
    assert host.streams[0].killed
    assert host.files == {} and host.dirs == set()
    assert not (tmp_path / "rec.bin").exists()


# record: a busy board ------------------------------------------------------------------


def test_busy_board_is_refused_with_the_reasons_and_nothing_is_uploaded(bitstream, tmp_path):
    host = FakeHost(probe=probe(terminals=[terminal(70_000), terminal(120, "/dev/pts/5")]))
    with pytest.raises(run.BoardBusy) as e:
        _record(host, bitstream, tmp_path)
    assert e.value.reasons
    assert "/dev/pts/5" in e.value.reasons[0] and "/dev/pts/5" in str(e.value)
    assert [ev[1][:3] for ev in host.events] == [["python3", "-", "probe"]]


# record: cleaning up ------------------------------------------------------------------


def test_remote_files_are_removed_on_success(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    _record(host, bitstream, tmp_path)
    assert host.files == {} and host.dirs == set()
    assert host.events[-1][1][:3] == ["rm", "-rf", "--"]


def test_remote_files_are_removed_when_the_loader_fails(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    host.loader_rc = 1
    with pytest.raises(run.RunError, match="JTAG init failed"):
        _record(host, bitstream, tmp_path)
    assert host.streams[0].killed
    assert host.files == {} and host.dirs == set()
    assert not (tmp_path / "rec.bin").exists()


def test_remote_files_are_removed_when_the_fetch_is_corrupt(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    host.corrupt_fetch = True
    with pytest.raises(run.RunError, match="sha256"):
        _record(host, bitstream, tmp_path)
    assert host.files == {} and host.dirs == set()
    assert not (tmp_path / "rec.bin").exists()


def test_the_run_directory_is_private_to_the_run_and_under_the_users_home(bitstream, tmp_path):
    first, second = FakeHost(uart_bytes=frame(PAYLOAD)), FakeHost(uart_bytes=frame(PAYLOAD))
    _record(first, bitstream, tmp_path)
    _record(second, bitstream, tmp_path)
    dirs = [h.events[h.index("mkdir")][1][-1] for h in (first, second)]
    assert dirs[0] != dirs[1]
    for d in dirs:
        assert d.startswith("/home/pi/tt-harness-run-")
    remote_paths = [p for ev in first.events if ev[0] in ("run", "start") for p in ev[1]]
    assert not any("/tmp" in p for p in remote_paths)


# record: a short recording ------------------------------------------------------------


def test_a_receiver_timeout_gives_a_short_recording_that_is_inconclusive(bitstream, tmp_path):
    short = frame(PAYLOAD)[:3000]
    host = FakeHost(uart_bytes=short)
    out = _record(host, bitstream, tmp_path)
    assert out.read_bytes() == short
    assert json.loads(run.meta_path(out).read_text())["receiver"]["complete"] is False
    rec = host_frame.decode(out.read_bytes())
    assert not rec.ok and rec.problem == "short"
    assert verdict.compare(rec, PAYLOAD, flagged=False).kind == "inconclusive"
    assert host.files == {} and host.dirs == set()


def test_nothing_received_is_an_empty_recording_that_is_inconclusive(bitstream, tmp_path):
    host = FakeHost(uart_bytes=b"")
    out = _record(host, bitstream, tmp_path)
    assert out.read_bytes() == b""
    assert verdict.compare(host_frame.decode(b""), PAYLOAD, flagged=False).kind == "inconclusive"


# record: nothing from outside reaches a shell -----------------------------------------


@pytest.mark.parametrize(
    "name",
    ["x;reboot.bit", "$(id).bit", "a b.bit", "-x.bit", "x'.bit", "x.bin", "..bit", "x\n.bit"],
)
def test_a_bitstream_name_with_unexpected_characters_is_rejected(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"bits")
    host = FakeHost()
    with pytest.raises(ValueError, match="bitstream"):
        _record(host, path, tmp_path)
    assert host.events == []


@pytest.mark.parametrize(
    "uart",
    [
        "/dev/serial/by-id/x;reboot",
        "/dev/serial/by-id/$(id)",
        "/dev/serial/by-id/a b",
        "/dev/serial/by-id/../../sda",
        "/dev/serial/by-id/",
        "/dev/serial/by-id/x\n",
        "/dev/ttyUSB1",
        "serial/by-id/x",
        "/dev/serial/by-id/-x",
    ],
)
def test_a_uart_path_with_unexpected_characters_is_rejected(bitstream, tmp_path, uart):
    board = run.Board(host="ps1.fpgas.online", port=10722, uart=uart)
    host = FakeHost()
    with pytest.raises(ValueError, match="UART"):
        run.record(board, bitstream, 10, 1_000_000, tmp_path / "r.bin", 5, runner=host)
    with pytest.raises(ValueError, match="UART"):
        run.board_activity(board, runner=host)
    assert host.events == []


@pytest.mark.parametrize(
    "fields",
    [
        {"host": "-oProxyCommand=id"},
        {"host": "a b"},
        {"host": "a;b"},
        {"host": ""},
        {"user": "pi;id"},
        {"user": "-l"},
        {"port": 0},
        {"port": 70000},
        {"loader_args": ("-b", "arty;id")},
        {"loader_args": ("-b", "$(id)")},
    ],
)
def test_board_fields_with_unexpected_characters_are_rejected(bitstream, tmp_path, fields):
    base = {"host": "ps1.fpgas.online", "port": 10722, "uart": UART}
    board = run.Board(**{**base, **fields})
    host = FakeHost()
    with pytest.raises(ValueError):
        run.record(board, bitstream, 10, 1_000_000, tmp_path / "r.bin", 5, runner=host)
    with pytest.raises(ValueError):
        run.SshRunner(board)
    assert host.events == []


@pytest.mark.parametrize(("clocks", "baud"), [(0, 1_000_000), (-1, 1_000_000), (10, 0)])
def test_nonsense_numbers_are_rejected(bitstream, tmp_path, clocks, baud):
    host = FakeHost()
    with pytest.raises(ValueError):
        run.record(BOARD, bitstream, clocks, baud, tmp_path / "r.bin", 5, runner=host)
    assert host.events == []


def test_every_remote_command_is_built_from_checked_words(bitstream, tmp_path):
    host = FakeHost(uart_bytes=frame(PAYLOAD))
    _record(host, bitstream, tmp_path)
    for event in host.events:
        if event[0] in ("run", "start"):
            for word in event[1]:
                assert run.SAFE_WORD.fullmatch(word), (word, event)


# fetching the recording ---------------------------------------------------------------


def test_sent_file_is_read_back_exactly_despite_banner_and_nul_bytes(tmp_path, capsys):
    data = bytes(range(256)) * 300 + b"\x00" * 5000
    path = tmp_path / "recording.bin"
    path.write_bytes(data)
    assert board_host.main(["send", str(path)]) == 0
    printed = capsys.readouterr().out.encode()
    assert run.parse_sent(BANNER + printed + b"\x00trailing\n") == data


def test_a_sent_file_that_arrives_damaged_is_refused(tmp_path, capsys):
    path = tmp_path / "recording.bin"
    path.write_bytes(b"abc" * 1000)
    board_host.main(["send", str(path)])
    lines = capsys.readouterr().out.splitlines()
    with pytest.raises(run.RunError):
        run.parse_sent("\n".join(lines[:-1]).encode())  # the end marker never came
    doc = json.loads(lines[0].removeprefix("TTHR begin "))
    doc["sha256"] = "0" * 64
    lines[0] = "TTHR begin " + json.dumps(doc)
    with pytest.raises(run.RunError, match="sha256"):
        run.parse_sent("\n".join(lines).encode())


def test_the_script_uploaded_is_the_board_host_module():
    assert Path(board_host.__file__).resolve() == run.REMOTE_SCRIPT.resolve()


def test_record_keeps_the_pre_header_bytes_in_the_run_json(bitstream, tmp_path):
    host = FakeHost(uart_bytes=b"\x00\xfe" + frame(PAYLOAD))
    out = _record(host, bitstream, tmp_path)
    receiver = json.loads(run.meta_path(out).read_text())["receiver"]
    assert receiver["pre_header_count"] == 2 and receiver["pre_header_hex"] == "00fe"
    assert out.read_bytes().startswith(b"\x00\xfe")
    clean = _record(FakeHost(uart_bytes=frame(PAYLOAD)), bitstream, tmp_path / "clean")
    assert json.loads(run.meta_path(clean).read_text())["receiver"]["pre_header_count"] == 0
