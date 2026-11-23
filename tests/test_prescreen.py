# SPDX-License-Identifier: Apache-2.0
"""Pre-screen for latches, combinational loops and logic-derived clocks (Yosys)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tt_harness import fetch, manifest, prescreen, tools
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

DATA = Path(__file__).resolve().parent / "data"
YOSYS = tools.find("yosys")
needs_yosys = pytest.mark.skipif(YOSYS is None, reason="yosys not installed")


def sources_for(tmp_path: Path, top: str, files: list[Path], needs_sv: bool = False) -> Sources:
    """A `Sources` for local fixture files, laid out as a fetched checkout (`<root>/src/`)."""
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    for f in files:
        shutil.copyfile(f, root / "src" / f.name)
    project = Project(
        shuttle="test",
        macro=top,
        repo="/nonexistent",
        commit="0" * 40,
        top_module=top,
        source_files=tuple(f.name for f in files),
        clock_hz=25_175_000,
        clock_declared=0,
        pmods=(),
        address=None,
        ui_in=0,
        uio_in=0,
        patch=None,
    )
    return Sources(project, root, tuple(root / "src" / f.name for f in files), needs_sv)


def fixture(tmp_path: Path, name: str) -> Sources:
    return sources_for(tmp_path, name, [DATA / "prescreen" / f"{name}.v"])


def run(sources: Sources, tmp_path: Path) -> prescreen.Prescreen:
    return prescreen.check(sources, out_dir=tmp_path / "out")


# These need no Yosys -------------------------------------------------------------------


def test_missing_yosys_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(prescreen.tools, "find", lambda name: None)
    with pytest.raises(prescreen.PrescreenError, match="yosys"):
        run(fixture(tmp_path, "tt_um_test_ring"), tmp_path)


# These run Yosys -----------------------------------------------------------------------


@pytest.mark.toolchain
@needs_yosys
def test_counter_is_not_flagged(tmp_path):
    counter = DATA / "projects" / "counter" / "src" / "tt_um_test_counter.v"
    result = run(sources_for(tmp_path, "tt_um_test_counter", [counter]), tmp_path)
    assert (result.latches, result.comb_loops, result.derived_clocks) == (0, 0, 0)
    assert result.flagged is False
    assert result.sv_retried is False
    assert result.log.is_file()


@pytest.mark.toolchain
@needs_yosys
def test_latch_is_found(tmp_path):
    result = run(fixture(tmp_path, "tt_um_test_latch"), tmp_path)
    assert result.latches >= 1
    assert (result.comb_loops, result.derived_clocks) == (0, 0)
    assert result.flagged is True


@pytest.mark.toolchain
@needs_yosys
def test_inverter_ring_is_a_comb_loop(tmp_path):
    result = run(fixture(tmp_path, "tt_um_test_ring"), tmp_path)
    assert result.comb_loops >= 1
    assert (result.latches, result.derived_clocks) == (0, 0)
    assert result.flagged is True


@pytest.mark.toolchain
@needs_yosys
def test_flop_clocked_by_flop_is_a_derived_clock(tmp_path):
    result = run(fixture(tmp_path, "tt_um_test_derived_clock"), tmp_path)
    assert result.derived_clocks >= 1
    assert (result.latches, result.comb_loops) == (0, 0)
    assert result.flagged is True


@pytest.mark.toolchain
@needs_yosys
def test_clock_through_inverter_and_buffer_is_not_derived(tmp_path):
    result = run(fixture(tmp_path, "tt_um_test_inverted_clock"), tmp_path)
    assert (result.latches, result.comb_loops, result.derived_clocks) == (0, 0, 0)
    assert result.flagged is False


@pytest.mark.toolchain
@needs_yosys
def test_sv_syntax_in_v_file_is_retried_with_sv(tmp_path):
    result = run(fixture(tmp_path, "tt_um_test_sv_syntax"), tmp_path)
    assert result.sv_retried is True
    assert result.flagged is False


@pytest.mark.toolchain
@needs_yosys
def test_needs_sv_reads_with_sv_first_time(tmp_path):
    sources = sources_for(
        tmp_path, "tt_um_test_sv_syntax", [DATA / "prescreen" / "tt_um_test_sv_syntax.v"], True
    )
    result = run(sources, tmp_path)
    assert result.sv_retried is False
    assert result.flagged is False


@pytest.mark.toolchain
@needs_yosys
def test_syntax_error_raises_with_log(tmp_path):
    bad = tmp_path / "tt_um_test_broken.v"
    bad.write_text("module tt_um_test_broken(input wire clk); this is not verilog\nendmodule\n")
    with pytest.raises(prescreen.PrescreenError, match="tt_um_test_broken") as info:
        run(sources_for(tmp_path, "tt_um_test_broken", [bad]), tmp_path)
    message = str(info.value)
    assert "ERROR: syntax error" in message
    assert "also with -sv" in message
    assert "yosys.log" in message and "yosys-sv.log" in message


@pytest.mark.toolchain
@needs_yosys
def test_wrong_top_raises(tmp_path):
    sources = fixture(tmp_path, "tt_um_test_ring")
    sources = Sources(
        Project(**{**sources.project.__dict__, "top_module": "tt_um_absent"}),
        sources.root,
        sources.files,
        sources.needs_sv,
    )
    with pytest.raises(prescreen.PrescreenError):
        run(sources, tmp_path)


# Real designs, fetched at their pinned commits --------------------------------------------


def _real(project_id: str, tmp_path: Path) -> prescreen.Prescreen:
    videos = manifest.default_videos_repo()
    if not (videos / "data" / "targets.json").is_file():
        pytest.skip(f"videos checkout absent ({videos})")
    project = manifest.get(manifest.load(videos), project_id)
    try:
        sources = fetch.fetch(project, tools.REPO / "work" / "sources", videos)
    except fetch.FetchError as e:
        pytest.skip(f"sources not cached and not fetchable: {e}")
    return run(sources, tmp_path)


@pytest.mark.toolchain
@needs_yosys
def test_real_dottee_has_derived_clock_and_needs_sv(tmp_path):
    result = _real("ttsky26a/tt_um_algofoogle_dottee", tmp_path)
    assert result.derived_clocks >= 1
    assert result.flagged is True
    assert result.sv_retried is True


@pytest.mark.toolchain
@needs_yosys
def test_real_logo_screensaver_is_not_flagged(tmp_path):
    result = _real("tt08/tt_um_tinytapeout_logo_screensaver", tmp_path)
    assert (result.latches, result.comb_loops, result.derived_clocks) == (0, 0, 0)
    assert result.flagged is False
