# SPDX-License-Identifier: Apache-2.0
"""`scripts/clock_probe_run.py` against a fake board host: no network and no board."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import board_fakes
import pytest

from tt_harness.boards import arty_clock_probe as probe
from tt_harness.boards.arty_clock_probe import Reading
from tt_harness.host import run

REPO = Path(__file__).resolve().parent.parent


# The run script, against a fake board host ------------------------------------------------------


def _script():
    path = REPO / "scripts" / "clock_probe_run.py"
    spec = importlib.util.spec_from_file_location("clock_probe_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lines(gates, free: int = 25_174_825, **fields) -> bytes:
    return b"".join(
        probe.format_line(Reading(free + (n & 1), free, 50_000_000, 1, 0, n, **fields))
        for n in gates
    )


def test_run_script_measures_the_new_bitstream_and_cleans_up(tmp_path: Path) -> None:
    script = _script()
    bitstream = tmp_path / "top.bit"
    bitstream.write_bytes(b"\xaa\x99\x55\x66" * 100)
    # Two lines of whatever was loaded before, then this load's gates 0 to 13.
    capture = b"\x00" + _lines([200, 201], free=1) + _lines(range(14))
    host = board_fakes.FakeHost(uart_bytes=capture)
    doc = script.measure(board_fakes.BOARD, bitstream, 10, host)
    assert host.dirs == set() and host.files == {}, "files were left on the board host"
    assert host.index("listening") < host.index("openFPGALoader")
    assert doc["remote_dir"].startswith("/home/pi/tt-harness-probe-")
    summary = script.summarise(doc["capture"], 10)
    assert summary["complete"] and summary["lines_before_this_load"] == 2
    assert [g["gate"] for g in summary["gates"]] == list(range(1, 11))
    assert (summary["free_min"], summary["free_max"]) == (25_174_825, 25_174_826)
    assert summary["locked_all"] and summary["losses_max"] == 0
    assert script.locked_word(summary["gates"]) == "always"
    assert summary["lines_damaged"] == []


def test_run_script_refuses_a_busy_board_before_uploading(tmp_path: Path) -> None:
    script = _script()
    bitstream = tmp_path / "top.bit"
    bitstream.write_bytes(b"x")
    busy = board_fakes.probe(terminals=[board_fakes.terminal(30)])
    host = board_fakes.FakeHost(probe=busy)
    with pytest.raises(run.BoardBusy):
        script.measure(board_fakes.BOARD, bitstream, 10, host, wait_busy_s=0)
    assert not host.ran("mkdir") and not host.ran("openFPGALoader")


def test_summary_shows_damage_gaps_and_lock_loss() -> None:
    script = _script()
    lines = _lines(range(12)).split(b"\n")
    lines[4] = lines[4].replace(b"F 0", b"F 8")  # a damaged line: its checksum no longer fits
    summary = script.summarise(b"\n".join(lines), 10)
    assert not summary["complete"] and not summary["gate_numbers_consecutive"]
    assert len(summary["lines_damaged"]) == 1
    lost = _lines([0, 1]) + probe.format_line(Reading(5, 0, 0, 0, 1, 2)) + _lines([3])
    summary = script.summarise(lost, 3)
    assert summary["complete"]
    assert not summary["locked_all"] and summary["losses_max"] == 1
    assert script.locked_word(summary["gates"]) == "not always (2 of 3)"
    assert script.locked_word([{"locked": 0}, {"locked": 0}]) == "never"
    assert (summary["free_min"], summary["gated_min"]) == (5, 0)
    assert script.summarise(b"", 10)["complete"] is False
