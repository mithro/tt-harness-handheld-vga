# SPDX-License-Identifier: Apache-2.0
"""Host-side decode of the compare build's UART frame, and the dump and vgacap writers."""

from __future__ import annotations

import gzip
import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

from tt_harness.compare.recorder import header
from tt_harness.host import frame
from tt_harness.host.frame import Recording

VGACAP_STREAM_PY = Path("~/github/TinyTapeout/vgacap/python/vgacap/stream.py").expanduser()
PAYLOAD = bytes((i * 7 + 3) & 0xFF for i in range(301))


def build(payload: bytes) -> bytes:
    return header(len(payload)) + payload + struct.pack("<I", zlib.crc32(payload))


def test_hand_built_frame_round_trips():
    rec = frame.decode(build(PAYLOAD))
    assert rec == Recording(clocks=len(PAYLOAD), samples=PAYLOAD, ok=True, problem=None)


def test_empty_frame_is_ok():
    rec = frame.decode(build(b""))
    assert rec.ok and rec.clocks == 0 and rec.samples == b""


def test_trailing_bytes_after_frame_are_ignored():
    rec = frame.decode(build(PAYLOAD) + b"\x00\xff junk")
    assert rec.ok and rec.samples == PAYLOAD


def test_noise_before_magic_is_skipped():
    noise = bytes((i * 31 + 5) & 0xFF for i in range(37))
    assert b"TTHC" not in noise
    rec = frame.decode(noise + build(PAYLOAD))
    assert rec.ok and rec.samples == PAYLOAD and rec.clocks == len(PAYLOAD)


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"TTHX" + build(PAYLOAD)[4:], "bad magic"),
        (b"", "bad magic"),
        (b"TTH", "bad magic"),
        (build(PAYLOAD)[:4] + b"\x02" + build(PAYLOAD)[5:], "bad version"),
        (build(PAYLOAD)[:4], "short"),
        (build(PAYLOAD)[:7], "short"),
        (build(PAYLOAD)[: 9 + 100], "short"),  # truncated payload
        (build(PAYLOAD)[:-1], "short"),  # truncated trailer
        (build(PAYLOAD)[:-4], "short"),  # trailer missing
        (build(PAYLOAD)[:-4] + b"\x00\x00\x00\x00", "bad crc"),
        (build(PAYLOAD)[:20] + b"\x55" + build(PAYLOAD)[21:], "bad crc"),
    ],
)
def test_malformed_frames_are_not_ok(data, problem):
    rec = frame.decode(data)
    assert rec.ok is False
    assert rec.problem == problem


def test_truncated_payload_keeps_received_samples_and_declared_clocks():
    rec = frame.decode(build(PAYLOAD)[: 9 + 100])
    assert rec.clocks == len(PAYLOAD)
    assert rec.samples == PAYLOAD[:100]


def test_write_dump_raw_and_gzip(tmp_path):
    rec = frame.decode(build(PAYLOAD))
    frame.write_dump(rec, tmp_path / "uo_out.bin")
    frame.write_dump(rec, tmp_path / "uo_out.bin.gz")
    assert (tmp_path / "uo_out.bin").read_bytes() == PAYLOAD
    assert gzip.decompress((tmp_path / "uo_out.bin.gz").read_bytes()) == PAYLOAD


def read_vgacap_minimal(data: bytes) -> tuple[dict, bytes]:
    """A reader written from stream.h, independent of the writer under test."""
    chunks = []
    pos = 0
    while pos < len(data):
        tag = data[pos : pos + 4]
        (length,) = struct.unpack_from("<I", data, pos + 4)
        chunks.append((tag, data[pos + 8 : pos + 8 + length]))
        pos += 8 + length
    assert pos == len(data)
    tag, p = chunks[0]
    assert tag == b"VGCH"
    version, bits, mode, clock_hz = struct.unpack_from("<HBBI", p, 0)
    spw, flags, desc_len = p[16], p[17], struct.unpack_from("<H", p, 18)[0]
    hdr = {
        "version": version,
        "sample_bits": bits,
        "mode": mode,
        "clock_hz": clock_hz,
        "signal_map": tuple(p[8:16]),
        "samples_per_word": spw,
        "flags": flags,
        "desc": p[20 : 20 + desc_len].decode(),
    }
    assert len(p) == 20 + desc_len
    samples = bytearray()
    for tag, p in chunks[1:]:
        assert tag == b"RAW "
        (n,) = struct.unpack_from("<I", p, 0)
        words = struct.unpack_from(f"<{(len(p) - 4) // 4}I", p, 4)
        assert len(words) == -(-n // spw)
        for i in range(n):
            slot = spw - 1 - (i % spw) if flags & 1 else i % spw
            samples.append((words[i // spw] >> (8 * slot)) & 0xFF)
    return hdr, bytes(samples)


EXPECTED_HEADER = {
    "version": 1,
    "sample_bits": 8,
    "mode": 0,  # VGACAP_MODE_EXTCLK
    "clock_hz": 25_175_000,
    "signal_map": (7, 3, 0, 4, 1, 5, 2, 6),
    "samples_per_word": 4,
    "flags": 1,  # VGACAP_FLAG_FIRST_SAMPLE_MSB
    "desc": "tt08/tt_um_example fpga",
}


@pytest.mark.parametrize("chunk_samples", [None, 8, 12])
def test_write_vgacap_minimal_reader(tmp_path, monkeypatch, chunk_samples):
    if chunk_samples is not None:
        monkeypatch.setattr(frame, "RAW_CHUNK_SAMPLES", chunk_samples)
    rec = frame.decode(build(PAYLOAD))
    out = tmp_path / "rec.vgacap"
    frame.write_vgacap(rec, out, 25_175_000, "tt08/tt_um_example fpga")
    hdr, samples = read_vgacap_minimal(out.read_bytes())
    assert hdr == EXPECTED_HEADER
    assert samples == PAYLOAD


def test_write_vgacap_first_word_layout(tmp_path):
    rec = Recording(clocks=5, samples=bytes([1, 2, 3, 4, 5]), ok=True, problem=None)
    out = tmp_path / "rec.vgacap"
    frame.write_vgacap(rec, out, 0, "")
    data = out.read_bytes()
    raw = data[8 + 20 :]
    assert raw[:4] == b"RAW "
    assert struct.unpack_from("<I", raw, 4)[0] == 4 + 8
    assert struct.unpack_from("<I", raw, 8)[0] == 5
    # first sample in the most significant byte of each 32-bit little-endian word
    assert struct.unpack_from("<II", raw, 12) == (0x01020304, 0x05000000)


def test_write_vgacap_rejects_long_desc(tmp_path):
    rec = Recording(clocks=0, samples=b"", ok=True, problem=None)
    with pytest.raises(ValueError, match="desc"):
        frame.write_vgacap(rec, tmp_path / "x.vgacap", 0, "x" * 256)


def _load_vgacap_stream():
    if not VGACAP_STREAM_PY.is_file():
        pytest.skip(f"vgacap checkout absent ({VGACAP_STREAM_PY})")
    spec = importlib.util.spec_from_file_location("_vgacap_stream_under_test", VGACAP_STREAM_PY)
    module = importlib.util.module_from_spec(spec)
    # dataclasses looks the module up by name while the class body runs
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


@pytest.mark.parametrize("chunk_samples", [None, 12])
def test_write_vgacap_read_by_vgacap_checkout(tmp_path, monkeypatch, chunk_samples):
    stream = _load_vgacap_stream()
    if chunk_samples is not None:
        monkeypatch.setattr(frame, "RAW_CHUNK_SAMPLES", chunk_samples)
    rec = frame.decode(build(PAYLOAD))
    out = tmp_path / "rec.vgacap"
    frame.write_vgacap(rec, out, 25_175_000, "tt08/tt_um_example fpga")
    hdr, items = stream.read_stream(out.read_bytes())
    assert hdr == stream.Header(
        version=1,
        sample_bits=8,
        mode=0,
        clock_hz=25_175_000,
        signal_map=(7, 3, 0, 4, 1, 5, 2, 6),
        samples_per_word=4,
        flags=stream.FLAG_FIRST_SAMPLE_MSB,
        desc="tt08/tt_um_example fpga",
    )
    assert all(kind == "run" and run == 1 for kind, _, run in items)
    assert bytes(value for _, value, _ in items) == PAYLOAD
