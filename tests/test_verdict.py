# SPDX-License-Identifier: Apache-2.0
"""Reference reader and per-clock verdicts."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest

from tt_harness import verdict
from tt_harness.host.frame import Recording
from tt_harness.verdict import ReferenceInvalid, Verdict

PROJECT = "tt08/tt_um_example"
REF = bytes((i * 13 + i // 7) & 0xFF for i in range(20_000))


def ok(samples: bytes) -> Recording:
    return Recording(clocks=len(samples), samples=samples, ok=True, problem=None)


def write_reference(ref_dir: Path, data: bytes, **overrides) -> Path:
    """A small reference in the layout of tt-harness-fpga-vga's schema 1 `reference.json`."""
    shuttle, macro = PROJECT.split("/")
    d = ref_dir / shuttle / macro
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(data))
    doc = {
        "schema": 1,
        "id": PROJECT,
        "clock_hz": 25_175_000,
        "clock_declared": True,
        "reset_clocks": 10,
        "ui_in": 0x30,
        "uio_in": 0x01,
        "clocks": len(data),
        "dump": "uo_out.bin.gz",
        "dump_sha256": hashlib.sha256(data).hexdigest(),
        "uio_dump": "uio.bin.gz",
        "uio_dump_sha256": "0" * 64,
        "mode": "640x480@60",
        "frames": [],
    }
    doc.update(overrides)
    doc = {k: v for k, v in doc.items() if v is not None}
    (d / "reference.json").write_text(json.dumps(doc))
    return d / "reference.json"


# compare -----------------------------------------------------------------------------


def test_equal_is_exact():
    assert verdict.compare(ok(REF), REF, flagged=False) == Verdict("exact", None, "")


def test_equal_flagged_is_still_exact():
    assert verdict.compare(ok(REF), REF, flagged=True).kind == "exact"


def differ_at(data: bytes, index: int) -> bytes:
    return data[:index] + bytes([data[index] ^ 0x5A]) + data[index + 1 :]


def test_one_differing_byte_is_mismatch_at_that_clock():
    v = verdict.compare(ok(differ_at(REF, 12345)), REF, flagged=False)
    assert v.kind == "mismatch"
    assert v.first_diff_clock == 12345
    assert "12345" in v.detail


def test_first_of_several_differences_is_reported():
    rec = differ_at(differ_at(REF, 19_999), 4097)
    assert verdict.compare(ok(rec), REF, flagged=False).first_diff_clock == 4097


def test_difference_at_clock_zero():
    assert verdict.compare(ok(differ_at(REF, 0)), REF, flagged=False).first_diff_clock == 0


def test_flagged_difference_is_nondeterministic():
    v = verdict.compare(ok(differ_at(REF, 12345)), REF, flagged=True)
    assert v.kind == "nondeterministic"
    assert v.first_diff_clock == 12345


@pytest.mark.parametrize("problem", ["bad magic", "bad version", "short", "bad crc"])
@pytest.mark.parametrize("flagged", [False, True])
def test_not_ok_recording_is_inconclusive_even_if_bytes_match(problem, flagged):
    rec = Recording(clocks=len(REF), samples=REF, ok=False, problem=problem)
    v = verdict.compare(rec, REF, flagged=flagged)
    assert v.kind == "inconclusive"
    assert v.first_diff_clock is None
    assert problem in v.detail


def test_not_ok_recording_is_inconclusive_even_if_bytes_differ():
    rec = Recording(len(REF), differ_at(REF, 5), ok=False, problem="bad crc")
    assert verdict.compare(rec, REF, flagged=False) == Verdict(
        "inconclusive", None, "recording not usable: bad crc"
    )


@pytest.mark.parametrize("flagged", [False, True])
def test_shorter_recording_is_inconclusive(flagged):
    v = verdict.compare(ok(REF[:-1]), REF, flagged=flagged)
    assert v.kind == "inconclusive"
    assert v.first_diff_clock is None
    assert str(len(REF) - 1) in v.detail and str(len(REF)) in v.detail


def test_shorter_recording_with_early_difference_is_still_inconclusive():
    rec = differ_at(REF, 3)[:100]
    assert verdict.compare(ok(rec), REF, flagged=False).kind == "inconclusive"


def test_longer_recording_compared_over_reference_span_only():
    assert verdict.compare(ok(REF + b"\x99" * 50), REF, flagged=False).kind == "exact"
    v = verdict.compare(ok(differ_at(REF, 77) + b"\x99" * 50), REF, flagged=False)
    assert v.kind == "mismatch" and v.first_diff_clock == 77


# load_reference ----------------------------------------------------------------------


def test_load_reference_reads_fields(tmp_path):
    write_reference(tmp_path, REF)
    ref = verdict.load_reference(tmp_path, PROJECT)
    assert ref.clocks == len(REF)
    assert ref.reset_clocks == 10
    assert ref.ui_in == 0x30
    assert ref.uio_in == 0x01
    assert ref.clock_hz == 25_175_000
    assert ref.samples == REF
    assert ref.path == tmp_path / "tt08" / "tt_um_example" / "reference.json"
    assert ref.sha256 == hashlib.sha256(REF).hexdigest()


def test_load_reference_schema_2(tmp_path):
    write_reference(tmp_path, REF, schema=2, key={"repo": "x"}, key_sha256="0" * 64)
    assert verdict.load_reference(tmp_path, PROJECT).clocks == len(REF)


def test_clocks_shorter_than_dump_is_accepted(tmp_path):
    write_reference(tmp_path, REF, clocks=1000)
    ref = verdict.load_reference(tmp_path, PROJECT)
    assert ref.clocks == 1000
    assert ref.samples == REF


def test_wrong_sha256_raises(tmp_path):
    write_reference(tmp_path, REF, dump_sha256=hashlib.sha256(REF[:-1]).hexdigest())
    with pytest.raises(ReferenceInvalid, match="sha256"):
        verdict.load_reference(tmp_path, PROJECT)


def test_sha256_of_compressed_file_is_not_accepted(tmp_path):
    path = write_reference(tmp_path, REF)
    gz = (path.parent / "uo_out.bin.gz").read_bytes()
    doc = json.loads(path.read_text())
    doc["dump_sha256"] = hashlib.sha256(gz).hexdigest()
    path.write_text(json.dumps(doc))
    with pytest.raises(ReferenceInvalid, match="sha256"):
        verdict.load_reference(tmp_path, PROJECT)


def test_clocks_larger_than_dump_raises(tmp_path):
    write_reference(tmp_path, REF, clocks=len(REF) + 1)
    with pytest.raises(ReferenceInvalid, match="clocks"):
        verdict.load_reference(tmp_path, PROJECT)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": 3},
        {"schema": None},
        {"id": "tt08/tt_um_other"},
        {"clocks": None},
        {"clocks": "20000"},
        {"clocks": -1},
        {"clocks": True},
        {"clock_hz": 25.175e6},
        {"reset_clocks": None},
        {"ui_in": 256},
        {"uio_in": None},
        {"dump": "../uo_out.bin.gz"},
        {"dump": "missing.bin.gz"},
        {"dump_sha256": None},
    ],
)
def test_malformed_reference_raises(tmp_path, overrides):
    write_reference(tmp_path, REF, **overrides)
    with pytest.raises(ReferenceInvalid):
        verdict.load_reference(tmp_path, PROJECT)


def test_missing_reference_raises(tmp_path):
    with pytest.raises(ReferenceInvalid, match="reference.json"):
        verdict.load_reference(tmp_path, PROJECT)


def test_unparsable_json_raises(tmp_path):
    path = write_reference(tmp_path, REF)
    path.write_text("{not json")
    with pytest.raises(ReferenceInvalid):
        verdict.load_reference(tmp_path, PROJECT)


def test_corrupt_gzip_raises(tmp_path):
    path = write_reference(tmp_path, REF)
    (path.parent / "uo_out.bin.gz").write_bytes(b"\x1f\x8b not really gzip")
    with pytest.raises(ReferenceInvalid):
        verdict.load_reference(tmp_path, PROJECT)


@pytest.mark.parametrize("bad_id", ["tt08", "../tt08/x", "tt08/x/y", "/tt08/x"])
def test_bad_project_id_raises(tmp_path, bad_id):
    with pytest.raises(ReferenceInvalid):
        verdict.load_reference(tmp_path, bad_id)


REAL_REFERENCE_DIR = Path(
    os.environ.get("TT_REFERENCE_DIR", "~/github/TinyTapeout/tt-harness-fpga-vga/work/reference")
).expanduser()


@pytest.mark.skipif(
    not (REAL_REFERENCE_DIR / "tt08/tt_um_tinytapeout_logo_screensaver").is_dir(),
    reason=f"shared reference files absent ({REAL_REFERENCE_DIR})",
)
def test_real_tt08_logo_reference():
    ref = verdict.load_reference(REAL_REFERENCE_DIR, "tt08/tt_um_tinytapeout_logo_screensaver")
    assert ref.clocks == 6_000_000
    assert len(ref.samples) >= ref.clocks
    assert ref.reset_clocks == 10
    span = ref.samples[: ref.clocks]
    assert verdict.compare(ok(span), span, flagged=False).kind == "exact"
