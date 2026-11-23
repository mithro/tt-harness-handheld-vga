# SPDX-License-Identifier: Apache-2.0
"""Decode the compare build's UART frame and write the recording in the shared formats.

The frame format is defined in `tt_harness.compare.recorder`. A recording is written as

- a per-clock dump: one byte per clock, byte i being `uo_out` after the rising edge of
  clock i (the format of the shared `uo_out.bin`), gzipped when the name ends `.gz`;
- a vgacap capture stream (`include/vgacap/stream.h` in the vgacap repository): a `VGCH`
  header chunk then `RAW ` chunks of packed 8-bit samples.
"""

from __future__ import annotations

import gzip
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from tt_harness.compare.recorder import HEADER_LENGTH, MAGIC, TRAILER_LENGTH, VERSION


@dataclass(frozen=True)
class Recording:
    clocks: int  # as declared by the frame header; 0 if the header was not read
    samples: bytes  # the payload bytes received, possibly fewer than `clocks` if not ok
    ok: bool
    problem: str | None  # "bad magic", "bad version", "short" or "bad crc" when not ok


def decode(data: bytes) -> Recording:
    """Decode a frame from raw UART bytes; anything before the first magic is skipped."""
    start = data.find(MAGIC)
    if start < 0:
        return Recording(0, b"", False, "bad magic")
    frame = data[start:]
    if len(frame) <= len(MAGIC):
        return Recording(0, b"", False, "short")
    if frame[len(MAGIC)] != VERSION:
        return Recording(0, b"", False, "bad version")
    if len(frame) < HEADER_LENGTH:
        return Recording(0, b"", False, "short")
    (clocks,) = struct.unpack_from("<I", frame, len(MAGIC) + 1)
    samples = bytes(frame[HEADER_LENGTH : HEADER_LENGTH + clocks])
    trailer = frame[HEADER_LENGTH + clocks : HEADER_LENGTH + clocks + TRAILER_LENGTH]
    if len(samples) < clocks or len(trailer) < TRAILER_LENGTH:
        return Recording(clocks, samples, False, "short")
    if struct.unpack("<I", trailer)[0] != zlib.crc32(samples):
        return Recording(clocks, samples, False, "bad crc")
    return Recording(clocks, samples, True, None)


def write_dump(rec: Recording, path: Path) -> None:
    """Write the samples, one byte per clock; gzip when the name ends `.gz`."""
    path = Path(path)
    if path.name.endswith(".gz"):
        with gzip.open(path, "wb") as f:
            f.write(rec.samples)
    else:
        path.write_bytes(rec.samples)


# vgacap stream constants, from include/vgacap/stream.h.
VGACAP_VERSION = 1
VGACAP_MODE_EXTCLK = 0
VGACAP_FLAG_FIRST_SAMPLE_MSB = 0x01
VGACAP_DESC_MAX = 255
# Sample bit per signal (hsync, vsync, R1, R0, G1, G0, B1, B0) for the Tiny VGA Pmod.
TINYVGA_SIGNAL_MAP = (7, 3, 0, 4, 1, 5, 2, 6)
SAMPLE_BITS = 8
SAMPLES_PER_WORD = 4
# Samples per RAW chunk: a multiple of SAMPLES_PER_WORD, so only the last chunk is padded,
# and a payload (1 MiB + 4 bytes) far below VGACAP_MAX_CHUNK_LEN (16 MiB).
RAW_CHUNK_SAMPLES = 1 << 20


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload


def _pack(samples: bytes) -> bytes:
    """Four 8-bit samples per little-endian u32, the first in the most significant byte."""
    padded = samples + bytes(-len(samples) % SAMPLES_PER_WORD)
    words = len(padded) // SAMPLES_PER_WORD
    return struct.pack(f"<{words}I", *struct.unpack(f">{words}I", padded))


def write_vgacap(rec: Recording, path: Path, clock_hz: int, desc: str) -> None:
    """Write the samples as a vgacap stream sampled on the project clock (external clock)."""
    desc_bytes = desc.encode()
    if len(desc_bytes) > VGACAP_DESC_MAX:
        raise ValueError(f"vgacap desc is {len(desc_bytes)} bytes, at most {VGACAP_DESC_MAX}")
    if not 0 <= clock_hz < 2**32:
        raise ValueError(f"vgacap clock_hz {clock_hz} does not fit in 32 bits")
    head = struct.pack(
        "<HBBI8sBBH",
        VGACAP_VERSION,
        SAMPLE_BITS,
        VGACAP_MODE_EXTCLK,
        clock_hz,
        bytes(TINYVGA_SIGNAL_MAP),
        SAMPLES_PER_WORD,
        VGACAP_FLAG_FIRST_SAMPLE_MSB,
        len(desc_bytes),
    )
    with Path(path).open("wb") as f:
        f.write(_chunk(b"VGCH", head + desc_bytes))
        for i in range(0, len(rec.samples), RAW_CHUNK_SAMPLES):
            part = rec.samples[i : i + RAW_CHUNK_SAMPLES]
            f.write(_chunk(b"RAW ", struct.pack("<I", len(part)) + _pack(part)))
