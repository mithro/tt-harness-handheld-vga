# SPDX-License-Identifier: Apache-2.0
"""Compare build system simulation: the bytes on the UART equal the reference dump.

`CompareTop` is converted to Verilog and built with Verilator together with the project's own
sources; `uart_capture_main.cpp` decodes the UART line and writes the received bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tt_harness import fetch, manifest, reference, tools, verdict
from tt_harness.compare import recorder, sim, top
from tt_harness.compare.top import CompareTop
from tt_harness.core.stimulus import RESET_CLOCKS
from tt_harness.fetch import Sources
from tt_harness.host import frame
from tt_harness.manifest import Project

DATA = Path(__file__).resolve().parent / "data" / "projects"
SYS_CLK_FREQ = 100_000_000
BAUD = SYS_CLK_FREQ // 4  # divider 4, for speed

needs_verilator = [
    pytest.mark.toolchain,
    pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed"),
]


def _local(name: str, top_module: str, ui_in: int = 0) -> Sources:
    files = (f"{top_module}.v",)
    project = Project(
        shuttle="tt00",
        macro=top_module,
        repo="/nonexistent",
        commit="0" * 40,
        top_module=top_module,
        source_files=files,
        clock_hz=25_175_000,
        clock_declared=25_175_000,
        pmods=(),
        address=None,
        ui_in=ui_in,
        uio_in=0,
        patch=None,
    )
    root = DATA / name
    return Sources(project, root, tuple(root / "src" / f for f in files), needs_sv=False)


def _real(project_id: str) -> Sources:
    videos = manifest.default_videos_repo()
    if not (videos / "data" / "targets.json").is_file():
        pytest.skip(f"no tinytapeout-vga-videos checkout at {videos} (set TT_VIDEOS_REPO)")
    project = manifest.get(manifest.load(videos), project_id)
    try:
        return fetch.fetch(project, tools.REPO / "work" / "sources")
    except fetch.FetchError as e:
        pytest.skip(f"cannot fetch {project_id} sources (empty cache and no network?): {e}")


def _assert_same(payload: bytes, expected: bytes, what: str) -> None:
    assert len(payload) == len(expected), f"{len(payload)} bytes against {len(expected)} ({what})"
    if payload != expected:
        first = next(i for i, (a, b) in enumerate(zip(payload, expected, strict=True)) if a != b)
        pytest.fail(
            f"first difference from {what} at clock {first}: "
            f"FPGA model {payload[first]:#04x}, {what} {expected[first]:#04x}"
        )


def _check_against_reference(
    sources: Sources, clocks: int, tmp_path: Path, shared: bool = False
) -> None:
    """The full captured payload equals the local reference (and the shared one, if any)."""
    data = sim.simulate(sources, clocks, tmp_path / "compare", SYS_CLK_FREQ, BAUD)
    # The exact bytes on the line: one frame and nothing else.
    assert len(data) == recorder.frame_length(clocks)
    assert data[: recorder.HEADER_LENGTH] == recorder.header(clocks)
    rec = frame.decode(data)
    assert rec.ok, f"frame not decoded: {rec.problem}"  # magic, version, length and CRC
    assert rec.clocks == clocks
    expected = reference.simulate(sources, clocks, tmp_path / "ref").read_bytes()
    _assert_same(rec.samples, expected, "local reference")
    if shared:
        ref_dir = verdict.default_reference_dir()
        if not (ref_dir / sources.project.id / "reference.json").is_file():
            # The local reference matched; the shared comparison cannot run.
            pytest.skip(f"no shared reference for {sources.project.id} under {ref_dir}")
        try:
            ref = verdict.load_reference(ref_dir, sources.project.id)
        except verdict.ReferenceInvalid as e:
            pytest.fail(f"shared reference present but unusable: {e}")
        assert (ref.reset_clocks, ref.ui_in, ref.uio_in) == (
            RESET_CLOCKS,
            sources.project.ui_in,
            sources.project.uio_in,
        )
        if len(ref.samples) < clocks:
            pytest.fail(f"shared reference has {len(ref.samples)} samples, fewer than {clocks}")
        _assert_same(rec.samples, ref.samples[:clocks], "shared reference")


def test_verilog_instantiates_project_by_name(tmp_path: Path) -> None:
    sources = _local("counter", "tt_um_test_counter", ui_in=0xA5)
    path = top.to_verilog(CompareTop(sources.project, SYS_CLK_FREQ, BAUD, 100), tmp_path / "t.v")
    text = path.read_text()
    header = re.search(r"module compare_top\s*\((.*?)\);", text, re.DOTALL)
    assert header, "no compare_top module"
    ports = set(re.findall(r"\b(?:input|output)\s+(?:wire|reg)?\s*(?:\[[^]]*\])?\s*(\w+)", text))
    assert {"clk", "rst", "tx", "done"} <= ports
    # The project is a black box instantiated by its module name, clocked by pclk directly
    # when no clock buffer is given (simulation).
    assert re.search(r"\btt_um_test_counter\s+\w+\s*\(", text)
    assert "module tt_um_test_counter" not in text


def test_clock_buffer_hook_is_used(tmp_path: Path) -> None:
    from migen import Instance

    sources = _local("counter", "tt_um_test_counter")

    def bufg(i, o):
        return Instance("BUFG", name="pclk_bufg", i_I=i, o_O=o)

    t = CompareTop(sources.project, SYS_CLK_FREQ, BAUD, 100, clock_buffer=bufg)
    text = top.to_verilog(t, tmp_path / "t.v").read_text()
    assert re.search(r"\bBUFG\s+pclk_bufg\s*\(", text)


@pytest.mark.parametrize("settle", [0, 1])
def test_rejects_short_settle(settle: int) -> None:
    sources = _local("counter", "tt_um_test_counter")
    with pytest.raises(ValueError):
        CompareTop(sources.project, SYS_CLK_FREQ, BAUD, 100, settle=settle)


class TestSimulation:
    pytestmark = needs_verilator

    def test_counter_matches_reference(self, tmp_path: Path) -> None:
        _check_against_reference(_local("counter", "tt_um_test_counter", 0xA5), 5000, tmp_path)

    def test_both_edge_design_matches_reference(self, tmp_path: Path) -> None:
        _check_against_reference(_local("edges", "tt_um_test_edges"), 300, tmp_path)

    def test_logo_screensaver_matches_reference(self, tmp_path: Path) -> None:
        _check_against_reference(
            _real("tt08/tt_um_tinytapeout_logo_screensaver"), 450_000, tmp_path, shared=True
        )

    def test_dottee_matches_reference(self, tmp_path: Path) -> None:
        # SystemVerilog syntax, and a second clock derived from vsync inside the project.
        _check_against_reference(
            _real("ttsky26a/tt_um_algofoogle_dottee"), 450_000, tmp_path, shared=True
        )

    def test_cycle_limit_is_a_clear_error(self, tmp_path: Path) -> None:
        sources = _local("counter", "tt_um_test_counter")
        with pytest.raises(sim.CompareSimError, match="cycle limit"):
            sim.simulate(sources, 100, tmp_path / "compare", SYS_CLK_FREQ, BAUD, max_cycles=500)
