# SPDX-License-Identifier: Apache-2.0
"""The script that runs on the board host: receiving a frame, probing for activity."""

from __future__ import annotations

import json
import os
import time

from board_fakes import PAYLOAD, frame

from tt_harness.compare.recorder import frame_length, header
from tt_harness.host import board_host


class FakePort:
    """A serial port that hands out `chunks` one per read, then nothing; time passes per read."""

    def __init__(self, chunks: list[bytes], tick: float = 0.2) -> None:
        self.chunks = list(chunks)
        self.now = 0.0
        self.tick = tick
        self.in_waiting = 1

    def read(self, n: int) -> bytes:
        self.now += self.tick
        return self.chunks.pop(0) if self.chunks else b""

    def clock(self) -> float:
        return self.now


def _receive(chunks, length, timeout_s=10.0, idle_s=2.0, head=None, **hooks):
    port = FakePort(chunks)
    info = board_host.receive(
        port, head or header(len(PAYLOAD)), length, timeout_s, idle_s, clock=port.clock, **hooks
    )
    return info, port


def test_receive_stops_as_soon_as_the_frame_is_whole():
    data = b"\x00\x00junk" + frame(PAYLOAD)
    chunks = [data[i : i + 700] for i in range(0, len(data), 700)] + [b"later"] * 5
    info, port = _receive(chunks, frame_length(len(PAYLOAD)))
    assert info["complete"] and info["stopped"] == "complete" and info["crc_ok"] is True
    assert info["header_at"] == 6
    assert info["data"].startswith(data)
    assert len(port.chunks) >= 4  # it did not keep reading after the frame


def test_receive_finds_a_header_split_across_reads():
    data = b"xx" + frame(PAYLOAD)
    info, _ = _receive([data[:5], data[5:9], data[9:]], frame_length(len(PAYLOAD)))
    assert info["complete"] and info["header_at"] == 2


def test_receive_reports_a_bad_crc_but_keeps_the_bytes():
    data = bytearray(frame(PAYLOAD))
    data[100] ^= 0x40
    info, _ = _receive([bytes(data)], frame_length(len(PAYLOAD)))
    assert info["complete"] and info["crc_ok"] is False
    assert info["data"] == bytes(data)


def test_receive_gives_up_at_the_timeout_when_no_header_comes():
    info, port = _receive([b"noise"], frame_length(len(PAYLOAD)), timeout_s=3.0)
    assert not info["complete"] and info["stopped"] == "timeout"
    assert info["header_at"] == -1 and info["data"] == b"noise"
    assert 3.0 <= port.now < 3.5


def test_receive_stops_early_when_a_started_frame_goes_quiet():
    data = frame(PAYLOAD)[:-10]  # ten bytes lost on the way
    info, port = _receive([data], frame_length(len(PAYLOAD)), timeout_s=100.0, idle_s=2.0)
    assert not info["complete"] and info["stopped"] == "idle"
    assert info["after_header"] == len(data)
    assert port.now < 3.0


def test_receive_stops_when_its_run_is_abandoned():
    info, port = _receive([], frame_length(len(PAYLOAD)), timeout_s=100.0, alive=lambda: False)
    assert info["stopped"] == "abandoned"
    assert port.now < 1.0


def test_probe_reports_this_machine_without_counting_itself():
    doc = board_host.probe("/dev/serial/by-id/absent-if01-port0")
    assert doc["uart_exists"] is False
    assert abs(doc["now"] - time.time()) < 60
    assert all(p["pid"] != os.getpid() for p in doc["processes"])
    json.dumps(doc)


# bytes before the header ----------------------------------------------------------------


def test_receive_counts_no_bytes_before_a_header_that_comes_first():
    info, _ = _receive([frame(PAYLOAD)], frame_length(len(PAYLOAD)))
    assert info["pre_header_count"] == 0 and info["pre_header_hex"] == ""


def test_receive_records_a_stray_byte_before_the_header():
    info, _ = _receive([b"\x00", frame(PAYLOAD)], frame_length(len(PAYLOAD)))
    assert info["complete"] and info["crc_ok"] is True
    assert info["pre_header_count"] == 1 and info["pre_header_hex"] == "00"


def test_receive_reports_at_most_sixteen_values_but_the_whole_count():
    noise = bytes(range(0x80, 0x80 + 40))
    info, _ = _receive([noise + frame(PAYLOAD)], frame_length(len(PAYLOAD)))
    assert info["pre_header_count"] == 40
    assert info["pre_header_hex"] == noise[: board_host.PRE_HEADER_KEPT].hex()
    assert board_host.PRE_HEADER_KEPT == 16
    assert info["data"].startswith(noise)  # the recording still has all of them


def test_receive_without_a_header_has_no_pre_header_count():
    info, _ = _receive([b"noise"], frame_length(len(PAYLOAD)), timeout_s=3.0)
    assert info["pre_header_count"] is None and info["pre_header_hex"] == ""
