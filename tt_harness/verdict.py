# SPDX-License-Identifier: Apache-2.0
"""Read a shared reference dump and compare a recording against it, clock by clock.

The reference is `tt-harness-fpga-vga`'s `<ref_dir>/<shuttle>/<macro>/reference.json`
(schema 1 or 2; the fields read here are the same in both) with its dump beside it: one
byte per clock, byte i being `uo_out` after the rising edge of clock i, clock 0 being the
first reset clock. `dump_sha256` is of the uncompressed bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tt_harness.host.frame import Recording

SCHEMAS = (1, 2)
DEFAULT_REFERENCE_DIR = "~/github/TinyTapeout/tt-harness-fpga-vga/work/reference"
_COMPARE_BLOCK = 1 << 16


def default_reference_dir() -> Path:
    """The shared reference files: `$TT_REFERENCE_DIR`, else the conventional sibling path."""
    return Path(os.environ.get("TT_REFERENCE_DIR", DEFAULT_REFERENCE_DIR)).expanduser()


class ReferenceInvalid(ValueError):
    """A reference file is missing, malformed, or does not match its dump."""


@dataclass(frozen=True)
class Reference:
    clocks: int  # the reference span: compare samples[:clocks]
    reset_clocks: int
    ui_in: int
    uio_in: int
    clock_hz: int
    samples: bytes  # the whole uncompressed dump; len(samples) >= clocks
    path: Path  # the reference.json read
    sha256: str  # dump_sha256, verified


@dataclass(frozen=True)
class Verdict:
    kind: Literal["exact", "mismatch", "inconclusive", "nondeterministic"]
    first_diff_clock: int | None
    detail: str


def _int_field(doc: dict, name: str, path: Path, lo: int, hi: int | None = None) -> int:
    value = doc.get(name)
    if type(value) is not int or value < lo or (hi is not None and value > hi):
        bound = f"{lo}..{hi}" if hi is not None else f">= {lo}"
        raise ReferenceInvalid(f"{path}: {name} is {value!r}, expected an integer {bound}")
    return value


def load_reference(ref_dir: Path, project_id: str) -> Reference:
    """Load and verify the reference for `project_id` ("<shuttle>/<macro>")."""
    parts = project_id.split("/")
    if len(parts) != 2 or not all(parts) or any(p in (".", "..") for p in parts):
        raise ReferenceInvalid(f"project id {project_id!r} is not <shuttle>/<macro>")
    path = Path(ref_dir) / parts[0] / parts[1] / "reference.json"
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise ReferenceInvalid(f"{project_id}: no reference.json at {path}") from None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ReferenceInvalid(f"{path}: unreadable: {e}") from e
    if not isinstance(doc, dict):
        raise ReferenceInvalid(f"{path}: not a JSON object")
    if type(doc.get("schema")) is not int or doc["schema"] not in SCHEMAS:
        raise ReferenceInvalid(f"{path}: unknown schema {doc.get('schema')!r}, expected {SCHEMAS}")
    if doc.get("id") != project_id:
        raise ReferenceInvalid(f"{path}: id is {doc.get('id')!r}, expected {project_id!r}")

    clocks = _int_field(doc, "clocks", path, 0)
    reset_clocks = _int_field(doc, "reset_clocks", path, 0)
    ui_in = _int_field(doc, "ui_in", path, 0, 255)
    uio_in = _int_field(doc, "uio_in", path, 0, 255)
    clock_hz = _int_field(doc, "clock_hz", path, 1)

    dump = doc.get("dump")
    if not isinstance(dump, str) or not dump or Path(dump).name != dump or dump in (".", ".."):
        raise ReferenceInvalid(f"{path}: dump {dump!r} is not a file name beside reference.json")
    sha = doc.get("dump_sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise ReferenceInvalid(f"{path}: dump_sha256 {sha!r} is not a SHA-256 hex digest")
    dump_path = path.parent / dump
    try:
        raw = dump_path.read_bytes()
        samples = gzip.decompress(raw) if dump.endswith(".gz") else raw
    except (OSError, EOFError, zlib.error, gzip.BadGzipFile) as e:
        raise ReferenceInvalid(f"{path}: cannot read dump {dump_path}: {e}") from e
    actual = hashlib.sha256(samples).hexdigest()
    if actual != sha.lower():
        raise ReferenceInvalid(
            f"{path}: dump_sha256 {sha} does not match the uncompressed {dump} ({actual})"
        )
    if clocks > len(samples):
        raise ReferenceInvalid(f"{path}: clocks {clocks} exceeds the dump's {len(samples)} bytes")
    return Reference(clocks, reset_clocks, ui_in, uio_in, clock_hz, samples, path, actual)


def _first_difference(a: bytes, b: bytes) -> int | None:
    """Index of the first byte where equal-length `a` and `b` differ, or None."""
    for start in range(0, len(a), _COMPARE_BLOCK):
        end = start + _COMPARE_BLOCK
        if a[start:end] != b[start:end]:
            return next(i for i in range(start, min(end, len(a))) if a[i] != b[i])
    return None


def compare(rec: Recording, ref_samples: bytes, flagged: bool) -> Verdict:
    """Compare a recording with the reference span `ref_samples`, byte for byte.

    A recording that is not intact, or does not cover the whole span, is `inconclusive`
    whatever its bytes are: only a complete, checksummed recording can be `exact` or not.
    """
    if not rec.ok:
        return Verdict("inconclusive", None, f"recording not usable: {rec.problem}")
    span = len(ref_samples)
    if len(rec.samples) < span:
        return Verdict(
            "inconclusive",
            None,
            f"recording has {len(rec.samples)} clocks, the reference span is {span}",
        )
    diff = _first_difference(rec.samples[:span], ref_samples)
    if diff is None:
        return Verdict("exact", None, "")
    got, want = rec.samples[diff], ref_samples[diff]
    detail = f"first difference at clock {diff}: uo_out 0x{got:02x}, reference 0x{want:02x}"
    if flagged:
        return Verdict("nondeterministic", diff, f"{detail} (pre-screen flagged the design)")
    return Verdict("mismatch", diff, detail)
