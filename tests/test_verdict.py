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
from tt_harness.manifest import Project
from tt_harness.verdict import ReferenceInvalid, Verdict

PROJECT = "tt08/tt_um_example"
REF = bytes((i * 13 + i // 7) & 0xFF for i in range(20_000))
REPO = "https://github.com/example/tt08-example"
COMMIT = "a" * 40


def ok(samples: bytes) -> Recording:
    return Recording(clocks=len(samples), samples=samples, ok=True, problem=None)


def write_reference(ref_dir: Path, data: bytes, **overrides) -> Path:
    """A small reference in the layout of tt-harness-fpga-vga's schema 3 `reference.json`."""
    shuttle, macro = PROJECT.split("/")
    d = ref_dir / shuttle / macro / "ideal"
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(data))
    doc = {
        "schema": 3,
        "id": PROJECT,
        "profile": "ideal",
        "clock_hz": 25_175_000,
        "clock_declared": True,
        "reset_clocks": 10,
        "ui_in": 0x30,
        "uio_in": 0x01,
        "ui_in_requested": 0x30,
        "ui_in_applied": 0x30,
        "ui_in_unmet": 0,
        "uio_in_requested": 0x01,
        "uio_in_applied": 0x01,
        "uio_in_unmet": 0,
        "clocks": len(data),
        "dump": "uo_out.bin.gz",
        "dump_sha256": hashlib.sha256(data).hexdigest(),
        "uio_dump": "uio.bin.gz",
        "uio_dump_sha256": "0" * 64,
        "mode": "640x480@60",
        "frames": [],
        "key": {"repo": REPO, "commit": COMMIT, "sources_sha256": "b" * 64},
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


@pytest.mark.parametrize("flagged", [False, True])
def test_empty_reference_span_is_inconclusive(flagged):
    v = verdict.compare(ok(b"\x01\x02\x03\x04\x05"), b"", flagged=flagged)
    assert v.kind == "inconclusive"
    assert v.first_diff_clock is None
    assert "empty" in v.detail


def test_reference_with_zero_clocks_raises(tmp_path):
    write_reference(tmp_path, REF, clocks=0)
    with pytest.raises(ReferenceInvalid, match="clocks"):
        verdict.load_reference(tmp_path, PROJECT)


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
    assert ref.path == tmp_path / "tt08" / "tt_um_example" / "ideal" / "reference.json"
    assert ref.sha256 == hashlib.sha256(REF).hexdigest()
    assert (ref.repo, ref.commit) == (REPO, COMMIT)


@pytest.mark.parametrize(
    "key",
    [
        None,  # no key at all
        {"repo": REPO},
        {"commit": COMMIT},
        {"repo": "", "commit": COMMIT},
        {"repo": REPO, "commit": "a" * 39},
        {"repo": REPO, "commit": 7},
        "not an object",
    ],
)
def test_a_reference_without_repo_and_commit_is_refused(tmp_path, key):
    write_reference(tmp_path, REF, key=key)
    with pytest.raises(ReferenceInvalid, match="key"):
        verdict.load_reference(tmp_path, PROJECT)


def _project(**changes) -> Project:
    fields = {
        "shuttle": "tt08",
        "macro": "tt_um_example",
        "repo": REPO,
        "commit": COMMIT,
        "top_module": "tt_um_example",
        "source_files": ("project.v",),
        "clock_hz": 25_175_000,
        "clock_declared": 25_175_000,
        "pmods": (),
        "address": None,
        "ui_in": 0x30,
        "uio_in": 0x01,
        "patch": None,
    }
    return Project(**{**fields, **changes})


def test_check_matches_accepts_the_reference_of_this_build(tmp_path):
    write_reference(tmp_path, REF)
    verdict.check_matches(verdict.load_reference(tmp_path, PROJECT), _project(), 10)


@pytest.mark.parametrize(
    "change, reset_clocks, named",
    [
        ({"commit": "c" * 40}, 10, "commit"),
        ({"repo": "https://github.com/example/other"}, 10, "repo"),
        ({"ui_in": 0x31}, 10, "ui_in"),
        ({"uio_in": 0x00}, 10, "uio_in"),
        ({}, 11, "reset_clocks"),
    ],
)
def test_check_matches_refuses_a_reference_made_for_something_else(
    tmp_path, change, reset_clocks, named
):
    write_reference(tmp_path, REF)
    ref = verdict.load_reference(tmp_path, PROJECT)
    with pytest.raises(verdict.ReferenceMismatch, match=named) as e:
        verdict.check_matches(ref, _project(**change), reset_clocks)
    assert str(ref.path) in str(e.value)


@pytest.mark.parametrize("schema", [1, 2])
def test_old_schemas_are_refused(tmp_path, schema):
    write_reference(tmp_path, REF, schema=schema)
    with pytest.raises(ReferenceInvalid, match="schema"):
        verdict.load_reference(tmp_path, PROJECT)


def test_wrong_profile_is_refused(tmp_path):
    write_reference(tmp_path, REF, profile="fpga-4")
    with pytest.raises(ReferenceInvalid, match="profile.*fpga-4"):
        verdict.load_reference(tmp_path, PROJECT)


@pytest.mark.parametrize("name", ["ui_in_unmet", "uio_in_unmet"])
def test_unmet_inputs_are_refused(tmp_path, name):
    write_reference(tmp_path, REF, **{name: 0xC0})
    with pytest.raises(ReferenceInvalid, match=f"{name}.*192"):
        verdict.load_reference(tmp_path, PROJECT)


@pytest.mark.parametrize("name", ["ui_in_unmet", "uio_in_unmet"])
@pytest.mark.parametrize("bad", [None, "0", -1, 256, True, 0.0])
def test_unmet_inputs_must_be_bytes(tmp_path, name, bad):
    write_reference(tmp_path, REF, **{name: bad})
    with pytest.raises(ReferenceInvalid, match=name):
        verdict.load_reference(tmp_path, PROJECT)


def test_old_layout_tree_is_refused_naming_what_was_looked_for(tmp_path):
    d = tmp_path / "tt08" / "tt_um_example"
    d.mkdir(parents=True)
    (d / "reference.json").write_text(json.dumps({"schema": 2, "id": PROJECT}))
    with pytest.raises(ReferenceInvalid, match="no reference") as e:
        verdict.load_reference(tmp_path, PROJECT)
    assert str(d / "ideal" / "reference.json") in str(e.value)


def _write_fail(tmp_path, content: str | bytes):
    d = tmp_path / "tt08" / "tt_um_example" / "ideal"
    d.mkdir(parents=True)
    data = content.encode() if isinstance(content, str) else content
    (d / "reference-fail.json").write_bytes(data)


def test_reference_fail_gives_a_distinct_message(tmp_path):
    _write_fail(
        tmp_path,
        json.dumps({"id": PROJECT, "status": "sim-unsettled", "reason": "pad at clock -1: uio[3]"}),
    )
    with pytest.raises(ReferenceInvalid, match="reference simulation failed") as e:
        verdict.load_reference(tmp_path, PROJECT)
    assert "sim-unsettled" in str(e.value) and "uio[3]" in str(e.value)


@pytest.mark.parametrize(
    "text",
    ["{not json", "[]", '"x"', "{}", '{"status": 3, "reason": null}', b"\xff\xfe"],
)
def test_malformed_reference_fail_is_reference_invalid(tmp_path, text):
    _write_fail(tmp_path, text)
    with pytest.raises(ReferenceInvalid, match="reference-fail.json"):
        verdict.load_reference(tmp_path, PROJECT)


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
        {"schema": 4},
        {"profile": None},
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
    with pytest.raises(ReferenceInvalid, match="no reference"):
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
REAL_PROJECT = "tt08/tt_um_tinytapeout_logo_screensaver"


@pytest.mark.skipif(
    not (REAL_REFERENCE_DIR / REAL_PROJECT / "ideal" / "reference.json").is_file(),
    reason=f"shared reference files absent ({REAL_REFERENCE_DIR})",
)
def test_real_tt08_logo_reference():
    ref = verdict.load_reference(REAL_REFERENCE_DIR, "tt08/tt_um_tinytapeout_logo_screensaver")
    assert ref.clocks == 6_000_000
    assert len(ref.samples) >= ref.clocks
    assert ref.reset_clocks == 10
    span = ref.samples[: ref.clocks]
    assert verdict.compare(ok(span), span, flagged=False).kind == "exact"
