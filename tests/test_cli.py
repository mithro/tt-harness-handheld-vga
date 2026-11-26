# SPDX-License-Identifier: Apache-2.0
"""CLI skeleton and tool-lookup helper tests."""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

import tt_harness
from tt_harness import cli, fetch, prescreen, tools
from tt_harness.compare.recorder import header


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tt_harness.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_prints_package_version():
    result = run_cli("--version")
    assert result.returncode == 0
    assert tt_harness.__version__ in result.stdout


def test_no_arguments_exits_2_with_usage():
    result = run_cli()
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_find_unknown_tool_is_none():
    assert tools.find("definitely-not-a-tool") is None


def test_find_locates_tool_on_path():
    found = tools.find("python3")
    assert found is not None
    assert found.name.startswith("python")


# verdict sub-command ------------------------------------------------------------------

VERDICT_ID = "tt07/tt_um_tinytapeout_dvd_screensaver"
VERDICT_REF = bytes((i * 5 + 1) & 0xFF for i in range(3000))
MANIFEST = Path(__file__).resolve().parent / "data" / "manifest"


def _frame(payload: bytes) -> bytes:
    return header(len(payload)) + payload + struct.pack("<I", zlib.crc32(payload))


@pytest.fixture
def verdict_env(tmp_path, monkeypatch):
    """A reference dir, and fetch/pre-screen replaced so no Git or Yosys runs."""
    ref_dir = tmp_path / "reference"
    d = ref_dir / "tt07" / "tt_um_tinytapeout_dvd_screensaver" / "ideal"
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(VERDICT_REF))
    doc = {
        "schema": 3,
        "id": VERDICT_ID,
        "profile": "ideal",
        "ui_in_unmet": 0,
        "uio_in_unmet": 0,
        "clock_hz": 25_175_000,
        "reset_clocks": 10,
        "ui_in": 0,
        "uio_in": 0,
        "clocks": len(VERDICT_REF),
        "dump": "uo_out.bin.gz",
        "dump_sha256": hashlib.sha256(VERDICT_REF).hexdigest(),
    }
    (d / "reference.json").write_text(json.dumps(doc))
    monkeypatch.setenv("TT_REFERENCE_DIR", str(ref_dir))
    monkeypatch.setenv("TT_VIDEOS_REPO", str(MANIFEST))

    state = {"flagged": False, "prescreen_error": None, "calls": []}
    log = tmp_path / "yosys.log"

    def fake_fetch(project, cache, videos_repo=None):
        state["calls"].append("fetch")
        return fetch.Sources(project=project, root=tmp_path, files=(), needs_sv=False)

    def fake_check(sources, out_dir=None):
        state["calls"].append("prescreen")
        if state["prescreen_error"]:
            raise prescreen.PrescreenError(state["prescreen_error"])
        f = state["flagged"]
        return prescreen.Prescreen(0, 0, int(f), f, log)

    monkeypatch.setattr(cli.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(cli.prescreen, "check", fake_check)
    return {"tmp": tmp_path, "state": state, "ref": d / "reference.json"}


def _run_verdict(argv: list[str], capsys) -> tuple[int, dict | None, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def _verdict(env, data: bytes, capsys) -> tuple[int, dict | None, str]:
    rec = env["tmp"] / "recording.bin"
    rec.write_bytes(data)
    return _run_verdict(["verdict", VERDICT_ID, str(rec)], capsys)


def test_verdict_exact_exits_0(verdict_env, capsys):
    code, doc, err = _verdict(verdict_env, b"\x00noise" + _frame(VERDICT_REF), capsys)
    assert code == 0, err
    assert doc["id"] == VERDICT_ID
    assert doc["verdict"] == "exact"
    assert doc["first_diff_clock"] is None
    assert doc["clocks"] == len(VERDICT_REF)
    assert doc["prescreen"] == {
        "latches": 0,
        "comb_loops": 0,
        "derived_clocks": 0,
        "flagged": False,
        "sv_retried": False,
        "log": str(verdict_env["tmp"] / "yosys.log"),
    }
    assert doc["reference"]["path"] == str(verdict_env["ref"])
    assert doc["reference"]["sha256"] == hashlib.sha256(VERDICT_REF).hexdigest()
    assert doc["recording"] == {
        "clocks": len(VERDICT_REF),
        "received": len(VERDICT_REF),
        "ok": True,
        "problem": None,
    }


def _differ(at: int) -> bytes:
    return VERDICT_REF[:at] + bytes([VERDICT_REF[at] ^ 1]) + VERDICT_REF[at + 1 :]


def test_verdict_mismatch_exits_1(verdict_env, capsys):
    code, doc, _ = _verdict(verdict_env, _frame(_differ(1234)), capsys)
    assert code == 1
    assert doc["verdict"] == "mismatch"
    assert doc["first_diff_clock"] == 1234


def test_verdict_flagged_difference_is_nondeterministic_exit_1(verdict_env, capsys):
    verdict_env["state"]["flagged"] = True
    code, doc, _ = _verdict(verdict_env, _frame(_differ(1234)), capsys)
    assert code == 1
    assert doc["verdict"] == "nondeterministic"
    assert doc["prescreen"]["flagged"] is True


@pytest.mark.parametrize(
    "data",
    [_frame(VERDICT_REF)[:-1], _frame(VERDICT_REF[:-1]), b"no frame here", b""],
)
def test_verdict_inconclusive_exits_2(verdict_env, capsys, data):
    code, doc, _ = _verdict(verdict_env, data, capsys)
    assert code == 2
    assert doc["verdict"] == "inconclusive"
    assert doc["first_diff_clock"] is None


def _assert_error(code, doc, err):
    assert code == 2
    assert doc is None
    assert err.startswith("tt-harness verdict: ")
    assert err.count("\n") == 1


def test_verdict_bad_reference_is_an_error(verdict_env, capsys):
    ref = verdict_env["ref"]
    ref.write_text(ref.read_text().replace('"clocks": 3000', '"clocks": 3001'))
    code, doc, err = _verdict(verdict_env, _frame(VERDICT_REF), capsys)
    _assert_error(code, doc, err)
    assert "clocks" in err
    assert verdict_env["state"]["calls"] == []  # refused before fetch and Yosys


def test_verdict_missing_reference_is_an_error(verdict_env, capsys):
    verdict_env["ref"].unlink()
    _assert_error(*_verdict(verdict_env, _frame(VERDICT_REF), capsys))
    assert verdict_env["state"]["calls"] == []


def test_verdict_runs_fetch_and_prescreen_when_reference_is_valid(verdict_env, capsys):
    code, _, _ = _verdict(verdict_env, _frame(VERDICT_REF), capsys)
    assert code == 0
    assert verdict_env["state"]["calls"] == ["fetch", "prescreen"]


def _edit_reference(env, **fields):
    ref = env["ref"]
    doc = json.loads(ref.read_text())
    doc.update(fields)
    ref.write_text(json.dumps(doc))


@pytest.mark.parametrize(
    ("fields", "names"),
    [
        ({"ui_in": 0x30}, ("ui_in", "0x30", "0x00")),
        ({"uio_in": 0x01}, ("uio_in", "0x01", "0x00")),
        ({"reset_clocks": 11}, ("reset_clocks", "11", "10")),
    ],
)
def test_verdict_stimulus_divergence_is_an_error_not_a_mismatch(verdict_env, capsys, fields, names):
    _edit_reference(verdict_env, **fields)
    code, doc, err = _verdict(verdict_env, _frame(_differ(5)), capsys)
    _assert_error(code, doc, err)
    for name in names:
        assert name in err


def test_verdict_prescreen_failure_is_an_error(verdict_env, capsys):
    verdict_env["state"]["prescreen_error"] = "tt07/x: pre-screen: yosys failed"
    code, doc, err = _verdict(verdict_env, _frame(VERDICT_REF), capsys)
    _assert_error(code, doc, err)
    assert "yosys failed" in err


def test_verdict_unknown_project_is_an_error(verdict_env, capsys):
    rec = verdict_env["tmp"] / "recording.bin"
    rec.write_bytes(_frame(VERDICT_REF))
    code, doc, err = _run_verdict(["verdict", "tt07/tt_um_absent", str(rec)], capsys)
    _assert_error(code, doc, err)
    assert "tt_um_absent" in err


def test_verdict_missing_recording_is_an_error(verdict_env, capsys):
    absent = str(verdict_env["tmp"] / "absent.bin")
    _assert_error(*_run_verdict(["verdict", VERDICT_ID, absent], capsys))
