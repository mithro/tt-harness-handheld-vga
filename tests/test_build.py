# SPDX-License-Identifier: Apache-2.0
"""Compare builds (`tt_harness.build`): timing-report parsers, the timing gate, real builds.

The fixtures under tests/data/timing/ are excerpts of real tool output from compare builds of
tt08/tt_um_tinytapeout_logo_screensaver for the Arty, on 2026-11-23:

- nextpnr-arty-logo.log: nextpnr-xilinx 0.8.2, two clocks ('clk0' is sys, 'project.clk' is
  pclk; the clock-name column is padded), the pre-route and the post-route report.
- nextpnr-arty-logo-pclk125-fail.log: the same netlist with pclk constrained at 125 MHz,
  which the project cannot meet (nextpnr's own ERROR line).
- vivado-arty-logo-timing-summary.rpt: Vivado 2025.2 report_timing_summary (check_timing
  and the summary tables).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from tt_harness import build, cli, fetch, manifest, tools
from tt_harness.boards import arty
from tt_harness.build import ClockPlan, TimingError
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

HERE = Path(__file__).resolve().parent
TIMING = HERE / "data" / "timing"
NEXTPNR_PASS = (TIMING / "nextpnr-arty-logo.log").read_text()
NEXTPNR_FAIL = (TIMING / "nextpnr-arty-logo-pclk125-fail.log").read_text()
VIVADO_PASS = (TIMING / "vivado-arty-logo-timing-summary.rpt").read_text()
PLAN = build.clock_plan(arty)
LOG = Path("build.log")

REAL_ID = "tt08/tt_um_tinytapeout_logo_screensaver"
REAL_CLOCKS = 6_000_000


def _post_route(text: str) -> tuple[str, str]:
    i = text.rindex(build._NEXTPNR_ROUTED)
    return text[:i], text[i:]


# Plan ----------------------------------------------------------------------------------------


def test_clock_plan_for_arty() -> None:
    assert PLAN.sys_mhz == 100.0
    assert PLAN.pclk_mhz == pytest.approx(100.0 / 6)  # 2 * (settle + 1) system cycles
    assert PLAN.cross_requirement_ns == pytest.approx(20.0)


def test_arty_pins_match_litex_boards() -> None:
    assert {p.port: (p.site, p.iostandard) for p in arty.PINS} == {
        "clk100": ("E3", "LVCMOS33"),
        "serial_tx": ("D10", "LVCMOS33"),
        "user_led0": ("H5", "LVCMOS33"),
    }
    from litex_boards.platforms import digilent_arty

    assert digilent_arty.Platform(variant="a7-35").device == arty.PARTS["vivado"]
    assert arty.PARTS["openxc7"] == "xc7a35tcsg324-1"


# nextpnr-xilinx ------------------------------------------------------------------------------


def test_nextpnr_multi_clock_report() -> None:
    timing = build.parse_nextpnr(NEXTPNR_PASS, PLAN, LOG)
    assert set(timing) == {"sys", "pclk", "sys->pclk", "pclk->sys"}
    # The post-route figures, not the pre-route ones (149.21 and 90.69 MHz).
    assert timing["sys"].achieved_mhz == 210.88
    assert timing["pclk"].achieved_mhz == 92.76
    assert timing["sys"].constraint_mhz == 100.0
    assert timing["pclk"].constraint_mhz == pytest.approx(16.667, abs=1e-3)
    # Cross-domain: data-path delay against S - 1 = 1 system period (10 ns).
    assert timing["sys->pclk"].setup_slack_ns == pytest.approx(10.0 - 6.59)
    assert timing["pclk->sys"].setup_slack_ns == pytest.approx(10.0 - 0.99)
    assert all(d.met for d in timing.values())
    build.check(timing, PLAN, LOG)


def test_nextpnr_long_mangled_clock_name() -> None:
    # Yosys may leave a clock net with a long internal name; nextpnr pads the shorter name.
    mangled = "$auto$clkbufmap.cc:268:execute$59340"
    text = NEXTPNR_PASS.replace("'clk0'", f"'{mangled}'").replace(
        "posedge clk0       ", f"posedge {mangled}"
    )
    text = text.replace("posedge clk0        ->", f"posedge {mangled} ->")
    text = re.sub(r"clock\s+'project\.clk'", "clock  'project.clk'", text)
    assert mangled in text and "clk0" not in text
    timing = build.parse_nextpnr(text, PLAN, LOG)
    assert timing["sys"].achieved_mhz == 210.88
    assert timing["sys->pclk"].achieved_mhz == pytest.approx(1000 / 6.59)
    assert timing["pclk->sys"].achieved_mhz == pytest.approx(1000 / 0.99)


def test_nextpnr_failing_domain() -> None:
    plan = ClockPlan(sys_mhz=100.0, pclk_mhz=125.0, settle=2)
    timing = build.parse_nextpnr(NEXTPNR_FAIL, plan, LOG)
    assert timing["pclk"].achieved_mhz == 91.46
    assert not timing["pclk"].met
    assert timing["sys"].met
    with pytest.raises(TimingError, match="timing not met in pclk"):
        build.check(timing, plan, LOG)


def test_nextpnr_domain_absent_from_report() -> None:
    pre, post = _post_route(NEXTPNR_PASS)
    post = "\n".join(line for line in post.splitlines() if "'project.clk'" not in line)
    timing = build.parse_nextpnr(pre + post, PLAN, LOG)
    assert "pclk" not in timing
    with pytest.raises(TimingError, match="unconstrained.*pclk.*absent"):
        build.check(timing, PLAN, LOG)


def test_nextpnr_clock_without_constraint() -> None:
    # A clock net the pre-pack script did not constrain (one made inside a project, say) is
    # timed at nextpnr's default and must not pass.
    text = NEXTPNR_PASS.replace("tt-harness clock: domain pclk net 'project.clk'", "dropped")
    with pytest.raises(TimingError, match="unconstrained: clock net 'project.clk'"):
        build.parse_nextpnr(text, PLAN, LOG)


def test_nextpnr_extra_clock_in_report() -> None:
    pre, post = _post_route(NEXTPNR_PASS)
    post = post.replace(
        "Info: Max frequency for clock 'project.clk'",
        "Info: Max frequency for clock 'project.vsync': 300.00 MHz (PASS at 12.00 MHz)\n"
        "Info: Max frequency for clock 'project.clk'",
    )
    with pytest.raises(TimingError, match="unconstrained: clock net 'project.vsync'"):
        build.parse_nextpnr(pre + post, PLAN, LOG)


def test_nextpnr_wrong_target_is_unconstrained() -> None:
    plan = ClockPlan(sys_mhz=100.0, pclk_mhz=20.0, settle=2)
    with pytest.raises(TimingError, match="pclk.*timed at 16.67 MHz"):
        build.parse_nextpnr(NEXTPNR_PASS, plan, LOG)


def test_nextpnr_cross_domain_over_budget() -> None:
    pre, post = _post_route(NEXTPNR_PASS)
    post = post.replace("posedge project.clk: 6.59 ns", "posedge project.clk: 12.40 ns")
    timing = build.parse_nextpnr(pre + post, PLAN, LOG)
    assert not timing["sys->pclk"].met
    with pytest.raises(TimingError, match="sys->pclk"):
        build.check(timing, PLAN, LOG)


def test_nextpnr_without_post_route_report() -> None:
    pre, _ = _post_route(NEXTPNR_PASS)
    with pytest.raises(TimingError, match="no post-route"):
        build.parse_nextpnr(pre, PLAN, LOG)


# Vivado --------------------------------------------------------------------------------------


def test_vivado_report() -> None:
    timing = build.parse_vivado(VIVADO_PASS, PLAN, LOG)
    assert set(timing) == {"sys", "pclk", "sys->pclk", "pclk->sys"}
    assert timing["sys"].achieved_mhz == pytest.approx(1000 / (10 - 5.700))
    assert timing["pclk"].achieved_mhz == pytest.approx(1000 / (60 - 48.768))
    assert timing["pclk"].constraint_mhz == pytest.approx(16.667, abs=1e-3)
    assert timing["sys->pclk"].setup_slack_ns == 17.283
    assert timing["sys->pclk"].hold_slack_ns == 8.317
    assert timing["pclk->sys"].hold_slack_ns == 1.053
    assert timing["sys->pclk"].constraint_mhz == pytest.approx(50.0)
    assert all(d.met for d in timing.values())
    build.check(timing, PLAN, LOG)


def _replace_row(text: str, first: str, new: str) -> str:
    out = [new if line.split()[:1] == [first] else line for line in text.splitlines()]
    return "\n".join(out) + "\n"


def test_vivado_failing_domain() -> None:
    row = next(
        line
        for line in VIVADO_PASS.splitlines()
        if line.split()[:1] == ["pclk"] and len(line.split()) == 13
    )
    failing = row.replace("48.768", "-1.250")
    text = VIVADO_PASS.replace(row, failing)
    timing = build.parse_vivado(text, PLAN, LOG)
    assert not timing["pclk"].met
    with pytest.raises(TimingError, match="timing not met in pclk"):
        build.check(timing, PLAN, LOG)


def test_vivado_hold_violation_fails() -> None:
    text = VIVADO_PASS.replace("8.317", "-0.020")
    timing = build.parse_vivado(text, PLAN, LOG)
    assert not timing["sys->pclk"].met


def test_vivado_clock_absent_from_report() -> None:
    lines = [ln for ln in VIVADO_PASS.splitlines() if not ln.strip().startswith("pclk ")]
    lines = [ln for ln in lines if ln.split()[:2] not in (["pclk", "sys"], ["sys", "pclk"])]
    timing = build.parse_vivado("\n".join(lines) + "\n", PLAN, LOG)
    assert set(timing) == {"sys"}
    with pytest.raises(TimingError, match="unconstrained.*pclk.*absent"):
        build.check(timing, PLAN, LOG)


def test_vivado_unclocked_registers_are_unconstrained() -> None:
    text = VIVADO_PASS.replace("checking no_clock (0)", "checking no_clock (12)")
    with pytest.raises(TimingError, match=r"unconstrained.*no_clock \(12\)"):
        build.parse_vivado(text, PLAN, LOG)


def test_vivado_unexpected_clock() -> None:
    text = VIVADO_PASS.replace(
        "  pclk  {0.000 30.000}",
        "  vsync {0.000 30.000}     60.000          16.667\n  pclk  {0.000 30.000}",
    )
    with pytest.raises(TimingError, match="unexpected clock vsync"):
        build.parse_vivado(text, PLAN, LOG)


# build_compare's gate, with the tools replaced by saved reports -------------------------------


def _counter() -> Sources:
    root = HERE / "data" / "projects" / "counter"
    project = Project(
        shuttle="tt00",
        macro="tt_um_test_counter",
        repo="/nonexistent",
        commit="0" * 40,
        top_module="tt_um_test_counter",
        source_files=("tt_um_test_counter.v",),
        clock_hz=25_175_000,
        clock_declared=25_175_000,
        pmods=(),
        address=None,
        ui_in=0,
        uio_in=0,
        patch=None,
    )
    return Sources(project, root, (root / "src" / "tt_um_test_counter.v",), needs_sv=False)


def _fake_flow(monkeypatch: pytest.MonkeyPatch, toolchain: str, report: str) -> list[str]:
    calls: list[str] = []

    def implement(job) -> str:
        calls.append("implement")
        job.unchecked_bit.write_bytes(b"not really a bitstream")
        return report

    def bitstream(job) -> None:
        calls.append("bitstream")

    flow = dataclasses.replace(build._FLOWS[toolchain], implement=implement, bitstream=bitstream)
    monkeypatch.setitem(build._FLOWS, toolchain, flow)
    return calls


@pytest.mark.parametrize("toolchain, report", [("openxc7", NEXTPNR_PASS), ("vivado", VIVADO_PASS)])
def test_build_compare_passes_on_a_met_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toolchain: str, report: str
) -> None:
    calls = _fake_flow(monkeypatch, toolchain, report)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", toolchain, tmp_path, clocks=100)
    assert calls == ["implement", "bitstream"]
    assert result.bitstream == tmp_path / "arty_compare.bit"
    assert result.bitstream.is_file()
    assert result.part == arty.PARTS[toolchain]
    assert all(d.met for d in result.timing.values())
    assert "module arty_compare" in (tmp_path / "arty_compare.v").read_text()


def test_build_compare_raises_on_failing_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pre, post = _post_route(NEXTPNR_PASS)
    post = post.replace("92.76 MHz (PASS at 16.67 MHz)", "12.00 MHz (FAIL at 16.67 MHz)").replace(
        "Info: Max frequency for clock 'project.clk'",
        "ERROR: Max frequency for clock 'project.clk'",
    )
    calls = _fake_flow(monkeypatch, "openxc7", pre + post)
    s = _counter()
    with pytest.raises(TimingError, match="timing not met in pclk") as info:
        build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert info.value.log == tmp_path / "build.log"
    assert calls == ["implement"]  # no bitstream step after a failed gate
    assert not list(tmp_path.glob("*.bit"))


def test_build_compare_raises_on_missing_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines = [ln for ln in VIVADO_PASS.splitlines() if not ln.strip().startswith("pclk ")]
    lines = [ln for ln in lines if ln.split()[:2] not in (["pclk", "sys"], ["sys", "pclk"])]
    _fake_flow(monkeypatch, "vivado", "\n".join(lines) + "\n")
    s = _counter()
    with pytest.raises(TimingError, match="unconstrained"):
        build.build_compare(s.project, s, "arty", "vivado", tmp_path, clocks=100)
    assert not list(tmp_path.glob("*.bit"))


def test_build_compare_removes_a_stale_bitstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "arty_compare.bit").write_bytes(b"from an earlier build")
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_FAIL)  # pclk timed at 125, not 16.67 MHz
    s = _counter()
    with pytest.raises(TimingError):
        build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert not (tmp_path / "arty_compare.bit").exists()


def test_build_compare_rejects_sources_of_another_project(tmp_path: Path) -> None:
    s = _counter()
    other = dataclasses.replace(s.project, macro="tt_um_other")
    with pytest.raises(ValueError, match="sources are for"):
        build.build_compare(other, s, "arty", "openxc7", tmp_path, clocks=100)


@pytest.mark.parametrize(
    "kwargs",
    [{"board": "gamebub"}, {"toolchain": "ise"}, {"jobs": 17}, {"jobs": 0}],
)
def test_build_compare_rejects_bad_arguments(tmp_path: Path, kwargs: dict) -> None:
    s = _counter()
    args = {"board": "arty", "toolchain": "openxc7", "jobs": 16} | kwargs
    with pytest.raises(ValueError):
        build.build_compare(s.project, s, out_dir=tmp_path, clocks=100, **args)


def test_cli_build_compare_unknown_project_fails(tmp_path: Path, capsys) -> None:
    videos = tmp_path / "videos"
    (videos / "data").mkdir(parents=True)
    (videos / "data" / "targets.json").write_text('{"targets": []}')
    argv = ["build-compare", "tt99/nope", "--board", "arty", "--toolchain", "openxc7"]
    assert cli.main([*argv, "--videos-repo", str(videos), "--out-dir", str(tmp_path)]) == 1
    assert "tt99/nope" in capsys.readouterr().err


# Real builds (Step 7) -------------------------------------------------------------------------


def _real_sources(project_id: str = REAL_ID) -> Sources:
    videos = manifest.default_videos_repo()
    if not (videos / "data" / "targets.json").is_file():
        pytest.skip(f"no tinytapeout-vga-videos checkout at {videos} (set TT_VIDEOS_REPO)")
    project = manifest.get(manifest.load(videos), project_id)
    try:
        return fetch.fetch(project, tools.REPO / "work" / "sources")
    except fetch.FetchError as e:
        pytest.skip(f"cannot fetch {project_id} sources (empty cache and no network?): {e}")


def _openxc7_installed() -> bool:
    needed = [
        tools.find("yosys"),
        tools.find("nextpnr-xilinx"),
        tools.find("fasm2frames"),
        tools.find("xc7frames2bit"),
    ]
    chipdb = build.CHIPDB / f"{arty.PARTS['openxc7']}.bin"
    return all(needed) and chipdb.is_file() and build.PRJXRAY_DB.is_dir()


def _check_real(result: build.BuildResult, toolchain: str) -> None:
    assert result.toolchain == toolchain
    assert result.part == arty.PARTS[toolchain]
    bit = result.bitstream.read_bytes()
    assert bit.find(bytes.fromhex("aa995566")) > 0, "no sync word in the bitstream"
    assert {"sys", "pclk"} <= set(result.timing)
    assert all(d.met for d in result.timing.values())
    for name, d in sorted(result.timing.items()):
        print(f"{toolchain} {name}: {d.achieved_mhz:.2f} MHz against {d.constraint_mhz:.2f} MHz")


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7() -> None:
    sources = _real_sources()
    out = tools.REPO / "build" / "compare" / "arty-openxc7" / "tt08" / sources.project.macro
    result = build.build_compare(sources.project, sources, "arty", "openxc7", out, REAL_CLOCKS)
    _check_real(result, "openxc7")


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_rejects_a_clock_made_in_the_project(tmp_path: Path) -> None:
    # dottee clocks some of its logic from its own vsync register; that clock has no
    # constraint, so the build must fail as unconstrained and leave no bitstream.
    sources = _real_sources("ttsky26a/tt_um_algofoogle_dottee")
    with pytest.raises(TimingError, match="unconstrained clock: BUFG .* 'project.uo_out"):
        build.build_compare(sources.project, sources, "arty", "openxc7", tmp_path, 1000)
    assert not list(tmp_path.glob("*.bit"))


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado() -> None:
    sources = _real_sources()
    out = tools.REPO / "build" / "compare" / "arty-vivado" / "tt08" / sources.project.macro
    result = build.build_compare(sources.project, sources, "arty", "vivado", out, REAL_CLOCKS)
    _check_real(result, "vivado")


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_rejects_a_clock_made_in_the_project(tmp_path: Path) -> None:
    # Vivado itself says "All user specified timing constraints are met" for dottee; only
    # check_timing shows the registers clocked from vsync_reg/Q.
    sources = _real_sources("ttsky26a/tt_um_algofoogle_dottee")
    with pytest.raises(TimingError, match=r"unconstrained: .*no_clock \(\d+\)"):
        build.build_compare(sources.project, sources, "arty", "vivado", tmp_path, 1000)
    assert not list(tmp_path.glob("*.bit"))
