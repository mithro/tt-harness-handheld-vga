# SPDX-License-Identifier: Apache-2.0
"""The `run-compare` sub-command, with the build and the board replaced: no tools, no network."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from board_fakes import UART, frame

from tt_harness import build, cli, fetch, manifest, prescreen
from tt_harness.host import run

ID = "tt07/tt_um_tinytapeout_dvd_screensaver"
REF = bytes((i * 5 + 1) & 0xFF for i in range(3000))
MANIFEST = Path(__file__).resolve().parent / "data" / "manifest"
BOARD_ARGS = ["--host", "ps1.fpgas.online", "--port", "10722", "--uart", UART]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A reference, and fetch, pre-screen, build and the board replaced by fakes."""
    d = tmp_path / "reference" / "tt07" / "tt_um_tinytapeout_dvd_screensaver"
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(REF))
    doc = {
        "schema": 1,
        "id": ID,
        "clock_hz": 25_175_000,
        "reset_clocks": 10,
        "ui_in": 0,
        "uio_in": 0,
        "clocks": len(REF),
        "dump": "uo_out.bin.gz",
        "dump_sha256": hashlib.sha256(REF).hexdigest(),
    }
    (d / "reference.json").write_text(json.dumps(doc))
    monkeypatch.setenv("TT_REFERENCE_DIR", str(tmp_path / "reference"))
    monkeypatch.setenv("TT_VIDEOS_REPO", str(MANIFEST))

    state = SimpleNamespace(
        builds=[], records=[], uart_bytes=frame(REF), busy=None, existing=None, tmp=tmp_path
    )

    def fake_fetch(project, cache, videos_repo=None):
        return fetch.Sources(project=project, root=tmp_path, files=(), needs_sv=False)

    def fake_build(project, sources, board, toolchain, out_dir, clocks, **kwargs):
        state.builds.append((project.id, board, toolchain, clocks, kwargs["baud"]))
        out_dir.mkdir(parents=True, exist_ok=True)
        bit = out_dir / "arty_compare.bit"
        bit.write_bytes(b"bits")
        return SimpleNamespace(bitstream=bit)

    def fake_record(board, bitstream, clocks, baud, out, timeout_s, runner=None):
        state.records.append((board, bitstream, clocks, baud, timeout_s))
        if state.busy:
            raise run.BoardBusy(board, state.busy)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(state.uart_bytes)
        counters = dict.fromkeys(("frame", "overrun", "parity", "brk", "buf_overrun"), 0)
        meta = {
            "bitstream_sha256": hashlib.sha256(bitstream.read_bytes()).hexdigest(),
            "load_s": 2.5,
            "receive_s": 3.5,
            "receiver": {
                "stopped": "complete",
                "icount_before": dict(counters, rx=5),
                "icount_after": dict(counters, rx=3018, overrun=2),
            },
        }
        run.meta_path(out).write_text(json.dumps(meta))
        return out

    monkeypatch.setattr(cli.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(
        cli.prescreen, "check", lambda s, o=None: prescreen.Prescreen(0, 0, 0, False, tmp_path)
    )
    monkeypatch.setattr(cli.build, "build_compare", fake_build)
    monkeypatch.setattr(cli.hw, "record", fake_record)
    monkeypatch.setattr(cli, "_existing_build", lambda *a: state.existing)
    return state


def _run(env, capsys, *extra: str, project: str = ID, board=BOARD_ARGS):
    argv = ["run-compare", project, *board, "--clocks", str(len(REF))]
    argv += ["--build-dir", str(env.tmp / "build"), "--out", str(env.tmp / "run" / "rec.bin")]
    code = cli.main([*argv, *extra])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_exact_run_builds_records_and_exits_0(env, capsys):
    code, doc, err = _run(env, capsys)
    assert code == 0, err
    assert doc["verdict"] == "exact" and doc["first_diff_clock"] is None
    assert doc["recording"]["ok"] is True
    assert env.builds == [(ID, "arty", "vivado", len(REF), 1_000_000)]
    (board, bitstream, clocks, baud, timeout_s) = env.records[0]
    assert board == run.Board(host="ps1.fpgas.online", port=10722, uart=UART)
    assert (clocks, baud) == (len(REF), 1_000_000)
    assert timeout_s > 30  # the frame's own time and time for the load
    assert doc["run"]["toolchain"] == "vivado" and doc["run"]["bitstream"] == str(bitstream)
    assert doc["run"]["bitstream_reused"] is False
    assert doc["run"]["bytes_received"] == len(frame(REF))
    assert doc["run"]["load_s"] == 2.5
    assert doc["run"]["uart_errors"] == {
        "frame": 0,
        "overrun": 2,
        "parity": 0,
        "brk": 0,
        "buf_overrun": 0,
    }


def test_toolchain_and_baud_reach_the_build_and_the_board(env, capsys):
    code, doc, _ = _run(env, capsys, "--toolchain", "openxc7", "--baud", "500000")
    assert code == 0
    assert env.builds == [(ID, "arty", "openxc7", len(REF), 500_000)]
    assert env.records[0][3] == 500_000
    assert doc["run"]["toolchain"] == "openxc7" and doc["run"]["baud"] == 500_000


def test_an_existing_build_is_reused_unless_rebuild_is_asked(env, capsys):
    bit = env.tmp / "arty_compare.bit"
    bit.write_bytes(b"old bits")
    env.existing = bit
    code, doc, _ = _run(env, capsys)
    assert code == 0 and env.builds == []
    assert doc["run"]["bitstream_reused"] is True and env.records[0][1] == bit
    code, doc, _ = _run(env, capsys, "--rebuild")
    assert code == 0 and len(env.builds) == 1
    assert doc["run"]["bitstream_reused"] is False


def test_a_mismatch_exits_1_with_the_first_differing_clock(env, capsys):
    env.uart_bytes = frame(REF[:777] + bytes([REF[777] ^ 0x80]) + REF[778:])
    code, doc, _ = _run(env, capsys)
    assert code == 1
    assert doc["verdict"] == "mismatch" and doc["first_diff_clock"] == 777


def test_a_short_recording_is_inconclusive_and_exits_2(env, capsys):
    env.uart_bytes = frame(REF)[:1000]
    code, doc, _ = _run(env, capsys)
    assert code == 2
    assert doc["verdict"] == "inconclusive" and doc["recording"]["problem"] == "short"
    assert doc["run"]["bytes_received"] == 1000


def test_a_busy_board_is_an_error_with_the_reasons_and_no_verdict(env, capsys):
    env.busy = ("terminal /dev/pts/5 (pi) was active 40 s ago", "process 9 (pi) runs tio: tio")
    code, doc, err = _run(env, capsys)
    assert code == 2 and doc is None
    assert "board busy" in err and "/dev/pts/5" in err and "runs tio" in err


def test_a_bad_uart_path_is_refused_before_anything_is_built(env, capsys):
    board = ["--host", "ps1.fpgas.online", "--port", "10722", "--uart", "/dev/ttyUSB1;id"]
    code, doc, err = _run(env, capsys, board=board)
    assert code == 2 and doc is None
    assert "UART" in err and err.count("\n") == 1
    assert env.builds == [] and env.records == []


def test_a_failed_run_names_the_step(env, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise run.RunError("loading the bitstream failed with status 1: JTAG init failed")

    monkeypatch.setattr(cli.hw, "record", broken)
    code, doc, err = _run(env, capsys)
    assert code == 2 and doc is None
    assert ": run: " in err and "JTAG init failed" in err


def test_a_failed_build_is_an_error_and_the_board_is_not_touched(env, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise build.BuildError("vivado failed", env.tmp / "build.log")

    monkeypatch.setattr(cli.build, "build_compare", broken)
    code, doc, err = _run(env, capsys)
    assert code == 2 and doc is None
    assert ": build: " in err
    assert env.records == []


def test_fewer_clocks_than_the_reference_span_is_refused(env, capsys):
    code = cli.main(["run-compare", ID, *BOARD_ARGS, "--clocks", "100"])
    out, err = capsys.readouterr()
    assert code == 2 and out == ""
    assert "--clocks 100" in err
    assert env.builds == [] and env.records == []


# reusing a build ------------------------------------------------------------------------


def test_existing_build_matches_only_the_same_settings(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    out_dir = tmp_path / "b"
    board_mod = build.BOARDS["arty"]
    top = board_mod.Top(project, 1_000_000, 5000, settle=build.SETTLE)
    top_v = board_mod.to_verilog(top, out_dir / "arty_compare.v")
    assert cli._existing_build(project, "arty", 5000, 1_000_000, out_dir) is None  # no bitstream
    bit = out_dir / "arty_compare.bit"
    bit.write_bytes(b"bits")
    assert cli._existing_build(project, "arty", 5000, 1_000_000, out_dir) == bit
    assert cli._existing_build(project, "arty", 5001, 1_000_000, out_dir) is None
    assert cli._existing_build(project, "arty", 5000, 500_000, out_dir) is None
    assert sorted(p.name for p in out_dir.iterdir()) == ["arty_compare.bit", "arty_compare.v"]
    # A bitstream older than the top it sits beside is not that top's.
    older = top_v.stat().st_mtime - 100
    os.utime(bit, (older, older))
    assert cli._existing_build(project, "arty", 5000, 1_000_000, out_dir) is None
