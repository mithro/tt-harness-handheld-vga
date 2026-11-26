# SPDX-License-Identifier: Apache-2.0
"""The `run-compare` sub-command, with the build and the board replaced: no tools, no network."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from board_fakes import UART, frame

from tt_harness import build, cli, fetch, manifest, prescreen
from tt_harness.compare.recorder import header
from tt_harness.host import board_host, run

ID = "tt07/tt_um_tinytapeout_dvd_screensaver"
REF = bytes((i * 5 + 1) & 0xFF for i in range(3000))
MANIFEST = Path(__file__).resolve().parent / "data" / "manifest"
BOARD_ARGS = ["--host", "ps1.fpgas.online", "--port", "10722", "--uart", UART]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A reference, and fetch, pre-screen, build and the board replaced by fakes."""
    d = tmp_path / "reference" / "tt07" / "tt_um_tinytapeout_dvd_screensaver" / "ideal"
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(REF))
    doc = {
        "schema": 3,
        "id": ID,
        "profile": "ideal",
        "ui_in_unmet": 0,
        "uio_in_unmet": 0,
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
        return SimpleNamespace(bitstream=bit, source_rewrites=(("src/built.v", 2),))

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
                **board_host.pre_header(state.uart_bytes, state.uart_bytes.find(header(clocks))),
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
    env.existing = (bit, {"source_rewrites": []})
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


def _built(tmp_path, project, toolchain="vivado", clocks=5000, baud=1_000_000):
    """A build directory as `build_compare` leaves it, with sources of one file."""
    root = tmp_path / "src-root"
    (root / "src").mkdir(parents=True, exist_ok=True)
    source = root / "src" / "project.v"
    source.write_text("module tt_um_x; endmodule\n")
    sources = fetch.Sources(project=project, root=root, files=(source,), needs_sv=False)
    out_dir = tmp_path / "b"
    board_mod = build.BOARDS["arty"]
    top = board_mod.Top(project, baud, clocks, settle=build.SETTLE)
    board_mod.to_verilog(top, out_dir / "arty_compare.v")
    bit = out_dir / "arty_compare.bit"
    bit.write_bytes(b"bits")
    result = build.BuildResult(
        bit,
        toolchain,
        board_mod.PARTS[toolchain],
        {},
        out_dir / "build.log",
        source_rewrites=(("src/project.v", 1),),
    )
    record = build.build_record(project, sources, "arty", toolchain, clocks, baud, result)
    build.write_record(out_dir, "arty", record)
    return sources, out_dir, bit


def test_existing_build_matches_only_the_same_settings(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    sources, out_dir, bit = _built(tmp_path, project)

    def existing(project=project, sources=sources, toolchain="vivado", clocks=5000, baud=10**6):
        return cli._existing_build(project, sources, "arty", toolchain, clocks, baud, out_dir)

    reused = existing()
    assert reused is not None
    assert reused[0] == bit and reused[1]["source_rewrites"] == [["src/project.v", 1]]
    assert existing(clocks=5001) is None
    assert existing(baud=500_000) is None
    assert existing(toolchain="openxc7") is None  # a Vivado build in the same directory
    other = dataclasses.replace(project, commit="f" * 40)
    assert existing(project=other, sources=dataclasses.replace(sources, project=other)) is None
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "arty_compare.bit",
        "arty_compare.build.json",
        "arty_compare.v",
    ]


def test_existing_build_refuses_an_edited_source(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    sources, out_dir, _ = _built(tmp_path, project)
    sources.files[0].write_text("module tt_um_x; wire w; endmodule\n")
    assert cli._existing_build(project, sources, "arty", "vivado", 5000, 10**6, out_dir) is None


def test_existing_build_refuses_another_source_list(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    sources, out_dir, _ = _built(tmp_path, project)
    extra = sources.root / "src" / "more.v"
    extra.write_text("module more; endmodule\n")
    more = dataclasses.replace(sources, files=(*sources.files, extra))
    assert cli._existing_build(project, more, "arty", "vivado", 5000, 10**6, out_dir) is None


def test_existing_build_refuses_an_edited_bitstream(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    sources, out_dir, bit = _built(tmp_path, project)
    bit.write_bytes(b"other bits")
    assert cli._existing_build(project, sources, "arty", "vivado", 5000, 10**6, out_dir) is None


def test_existing_build_without_a_record_is_rebuilt(tmp_path):
    project = manifest.get(manifest.load(MANIFEST), ID)
    sources, out_dir, _ = _built(tmp_path, project)
    build.record_path(out_dir, "arty").unlink()
    assert cli._existing_build(project, sources, "arty", "vivado", 5000, 10**6, out_dir) is None


def test_the_run_names_the_commit_and_the_source_rewrites(env, capsys):
    code, doc, _ = _run(env, capsys)
    assert code == 0
    project = manifest.get(manifest.load(MANIFEST), ID)
    assert doc["run"]["commit"] == project.commit
    assert doc["run"]["source_rewrites"] == [["src/built.v", 2]]
    bit = env.tmp / "arty_compare.bit"
    bit.write_bytes(b"old bits")
    env.existing = (bit, {"source_rewrites": [["src/reused.v", 1]]})
    code, doc, _ = _run(env, capsys)
    assert code == 0 and doc["run"]["source_rewrites"] == [["src/reused.v", 1]]


def test_bytes_before_the_header_are_counted_printed_and_do_not_change_the_verdict(env, capsys):
    env.uart_bytes = b"\x00" + frame(REF)
    code, doc, err = _run(env, capsys)
    assert code == 0 and doc["verdict"] == "exact"
    assert doc["run"]["pre_header_count"] == 1 and doc["run"]["pre_header_hex"] == "00"
    assert doc["run"]["bytes_received"] == len(frame(REF)) + 1
    assert "tt-harness run-compare: bytes before the frame header: 1 (00)" in err


def test_a_clean_run_prints_zero_bytes_before_the_header(env, capsys):
    code, doc, err = _run(env, capsys)
    assert code == 0 and doc["run"]["pre_header_count"] == 0
    assert "tt-harness run-compare: bytes before the frame header: 0\n" in err


def test_a_recording_without_a_header_has_an_unknown_count(env, capsys):
    env.uart_bytes = b"\x00\x00\x00"
    code, doc, err = _run(env, capsys)
    assert code == 2 and doc["verdict"] == "inconclusive"
    assert doc["run"]["pre_header_count"] is None
    assert "bytes before the frame header: unknown" in err
