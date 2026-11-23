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
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from tt_harness import build, cli, fetch, manifest, prescreen, tools, verilate
from tt_harness.boards import arty
from tt_harness.build import ClockPlan, TimingError
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

HERE = Path(__file__).resolve().parent
TIMING = HERE / "data" / "timing"
NEXTPNR_PASS = (TIMING / "nextpnr-arty-logo.log").read_text()
NEXTPNR_FAIL = (TIMING / "nextpnr-arty-logo-pclk125-fail.log").read_text()
NEXTPNR_DOTTEE = (TIMING / "nextpnr-arty-dottee.log").read_text()
VIVADO_PASS = (TIMING / "vivado-arty-logo-timing-summary.rpt").read_text()
VIVADO_DOTTEE = (TIMING / "vivado-arty-dottee-timing-summary.rpt").read_text()
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


def test_nextpnr_three_clocks_with_a_project_clock() -> None:
    # dottee: its vsync register (also uo_out[3]) clocks a frame counter. The clock-name
    # column is padded to 'project.uo_out[3]'.
    timing = build.parse_nextpnr(NEXTPNR_DOTTEE, PLAN, LOG)
    vsync = "project:project.uo_out[3]"
    assert set(timing) == {
        "sys",
        "pclk",
        vsync,
        "sys->pclk",
        "sys->" + vsync,
        "pclk->sys",
        vsync + "->pclk",
    }
    assert timing[vsync].kind == "project"
    assert timing[vsync].achieved_mhz == 280.66
    assert timing[vsync].constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    assert timing["sys"].kind == "sys" and timing["pclk"].kind == "pclk"
    assert timing[vsync + "->pclk"].kind == "cross"
    # Between pclk and a project clock: one pclk period less the allowance (50 ns).
    assert timing[vsync + "->pclk"].setup_slack_ns == pytest.approx(50.0 - 14.56)
    # Between sys and a project clock: S system periods less the allowance (10 ns).
    assert timing["sys->" + vsync].setup_slack_ns == pytest.approx(10.0 - 4.95)
    assert build.project_clocks(timing) == ("project.uo_out[3]",)
    assert all(d.met for d in timing.values())


def test_nextpnr_project_clock_without_constraint() -> None:
    text = "\n".join(ln for ln in NEXTPNR_DOTTEE.splitlines() if "domain project net" not in ln)
    with pytest.raises(TimingError, match=r"unconstrained: clock net 'project.uo_out\[3\]'"):
        build.parse_nextpnr(text, PLAN, LOG)


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
    with pytest.raises(TimingError, match="unconstrained.*pclk.*absent"):
        build.parse_nextpnr(pre + post, PLAN, LOG)


def test_gate_requires_sys_and_pclk() -> None:
    timing = build.parse_nextpnr(NEXTPNR_PASS, PLAN, LOG)
    del timing["pclk"]
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


def test_vivado_report_with_a_project_clock() -> None:
    timing = build.parse_vivado(VIVADO_DOTTEE, PLAN, LOG)
    vsync = "project:project/hvsync_gen/vsync_reg/Q"
    assert set(timing) == {
        "sys",
        "pclk",
        vsync,
        "pclk->sys",
        vsync + "->sys",
        "sys->pclk",
        vsync + "->pclk",
        "sys->" + vsync + " (async_default)",  # recovery/removal of its `negedge rst_n`
    }
    assert timing[vsync].kind == "project"
    assert timing[vsync].constraint_mhz == pytest.approx(PLAN.pclk_mhz, abs=1e-3)
    assert timing[vsync].setup_slack_ns == 57.741
    assert timing[vsync + "->pclk"].hold_slack_ns == 0.523
    assert timing["sys->" + vsync + " (async_default)"].setup_slack_ns == 18.795
    assert build.project_clocks(timing) == ("project/hvsync_gen/vsync_reg/Q",)
    assert all(d.met for d in timing.values())


def test_vivado_project_clock_needs_its_mapping() -> None:
    text = VIVADO_DOTTEE.split("\n", 1)[1]  # without the `tt-harness project clock:` line
    with pytest.raises(TimingError, match="unexpected clock project_clk0"):
        build.parse_vivado(text, PLAN, LOG)


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
    with pytest.raises(TimingError, match="unconstrained.*pclk.*absent"):
        build.parse_vivado("\n".join(lines) + "\n", PLAN, LOG)


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
    # The pre-screen needs Yosys; these tests are about the gate only.
    screen = prescreen.Prescreen(0, 0, 0, False, Path("prescreen.log"))
    monkeypatch.setattr(build.prescreen, "check", lambda sources, out_dir=None: screen)
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


def _unmodified_inputs(sources: Sources) -> dict[str, str]:
    files, _ = verilate.project_files(sources)
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def _checkout_clean(sources: Sources) -> None:
    """The fetched checkout still matches its commit: the build modified nothing in it."""
    r = subprocess.run(
        ["git", "-C", str(sources.root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert r.stdout == "", f"fetched sources modified:\n{r.stdout}"


def _check_real(result: build.BuildResult, sources: Sources, toolchain: str) -> None:
    assert result.toolchain == toolchain
    assert result.part == arty.PARTS[toolchain]
    bit = result.bitstream.read_bytes()
    assert bit.find(bytes.fromhex("aa995566")) > 0, "no sync word in the bitstream"
    assert {"sys", "pclk"} <= set(result.timing)
    assert all(d.met for d in result.timing.values())
    for name in result.project_clocks:
        domain = result.timing[f"{build.PROJECT}{name}"]
        assert domain.kind == "project"
        assert domain.constraint_mhz == pytest.approx(PLAN.pclk_mhz, abs=0.01)
    # The pre-screen finds a clock made in logic exactly when the tools do.
    screen = prescreen.check(sources)
    assert screen.derived_clocks == result.prescreen_derived_clocks
    assert bool(screen.derived_clocks) == bool(result.project_clocks), (
        f"pre-screen derived_clocks {screen.derived_clocks}, tools {result.project_clocks}"
    )
    _checkout_clean(sources)
    for name, d in sorted(result.timing.items()):
        print(f"{toolchain} {name}: {d.achieved_mhz:.2f} MHz against {d.constraint_mhz:.2f} MHz")


DOTTEE_ID = "ttsky26a/tt_um_algofoogle_dottee"


def _out(toolchain: str, sources: Sources) -> Path:
    p = sources.project
    return tools.REPO / "build" / "compare" / f"arty-{toolchain}" / p.shuttle / p.macro


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
@pytest.mark.parametrize("project_id", [REAL_ID, DOTTEE_ID])
def test_real_build_openxc7(project_id: str) -> None:
    sources = _real_sources(project_id)
    result = build.build_compare(
        sources.project, sources, "arty", "openxc7", _out("openxc7", sources), REAL_CLOCKS
    )
    _check_real(result, sources, "openxc7")
    # Yosys read the fetched files themselves, byte for byte.
    assert result.inputs == _unmodified_inputs(sources)
    assert result.source_rewrites == ()
    if project_id == DOTTEE_ID:
        # SystemVerilog only because the plain parse fails (`>>=` in audio.v).
        assert result.language == build.SYSTEMVERILOG
        assert result.project_clocks == ("project.uo_out[3]",)  # its vsync register
    else:
        assert result.language == build.VERILOG
        assert result.project_clocks == ()


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_refuses_to_modify_sources(tmp_path: Path) -> None:
    sources = _real_sources()
    with pytest.raises(build.BuildError, match=r"Synth 8-6735.*--vivado-nettype-rewrite"):
        build.build_compare(sources.project, sources, "arty", "vivado", tmp_path, 1000)
    assert not list(tmp_path.glob("*.bit"))
    _checkout_clean(sources)


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
@pytest.mark.parametrize("project_id", [REAL_ID, DOTTEE_ID])
def test_real_build_vivado(project_id: str) -> None:
    sources = _real_sources(project_id)
    result = build.build_compare(
        sources.project,
        sources,
        "arty",
        "vivado",
        _out("vivado", sources),
        REAL_CLOCKS,
        vivado_nettype_rewrite=True,
    )
    _check_real(result, sources, "vivado")
    assert "[Synth 8-6735]" in result.log.read_text()  # from the unmodified attempt, first
    # Only files with `default_nettype none` were given modified, and only in that line.
    unmodified = _unmodified_inputs(sources)
    src = sources.root / "src"
    copies = result.bitstream.parent / "src-nettype-wire"
    assert result.source_rewrites
    assert len(result.inputs) == len(unmodified)
    for given, digest in result.inputs.items():
        original = src / Path(given).relative_to(copies)
        if str(original) in result.source_rewrites:
            assert Path(given).read_text() == original.read_text().replace(
                "`default_nettype none", "`default_nettype wire"
            )
        else:
            assert digest == unmodified[str(original)]
    if project_id == DOTTEE_ID:
        assert result.project_clocks == ("project/hvsync_gen/vsync_reg/Q",)
    else:
        assert result.project_clocks == ()
