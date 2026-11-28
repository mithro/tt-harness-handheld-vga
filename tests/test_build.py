# SPDX-License-Identifier: Apache-2.0
"""Compare builds (`tt_harness.build`): timing-report parsers, the timing gate, real builds.

The fixtures under tests/data/timing/ are excerpts of real tool output from compare builds for
the Arty, of tt08/tt_um_tinytapeout_logo_screensaver and (the two dottee files, with the
lines the flows add about clocks the project makes) of ttsky26a/tt_um_algofoogle_dottee.
`scripts/timing_fixtures.py` regenerates them from real builds; the four regenerated ones
are of 2026-11-24 at the default settle of 3 (pclk 80 ns), and the numbers quoted from them
below change when they are regenerated:

- nextpnr-arty-logo.log: nextpnr-xilinx 0.8.2, two clocks ('clk0' is sys, 'project.clk' is
  pclk; the clock-name column is padded), the pre-route and the post-route report.
- nextpnr-arty-logo-pclk125-fail.log: the logo netlist of 2026-11-23 (settle 2) with pclk
  constrained at 125 MHz, which the project cannot meet (nextpnr's own ERROR line); not
  regenerated, and parsed with a plan of its own.
- vivado-arty-logo-timing-summary.rpt: Vivado 2025.2 report_timing_summary (check_timing
  and the summary tables).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tt_harness import (
    __version__,
    build,
    cli,
    fetch,
    input_gate,
    manifest,
    prescreen,
    tools,
    verilate,
)
from tt_harness import project_clocks as pc
from tt_harness.boards import arty
from tt_harness.build import ClockPlan, TimingError
from tt_harness.compare import recorder
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
# Everything below follows from the plan's settle S and system period T (10 ns):
P = PLAN.pclk_period_ns  # 2 * (S + 1) * T
XREQ = PLAN.cross_requirement_ns  # sys <-> pclk: S * T
ALLOW = build.CROSS_DOMAIN_ALLOWANCE_CYCLES * PLAN.sys_period_ns  # the open flow's
FULL = P - ALLOW  # open-flow budget between pclk and a rising-edge-only project clock
HALF = P / 2 - ALLOW  # ... when either may be on a falling edge
PCLK_TXT = f"{PLAN.pclk_mhz:.2f}"  # as nextpnr prints the target
PLAN2 = build.clock_plan(arty, settle=2)  # the minimum settle still has to work

REAL_ID = "tt08/tt_um_tinytapeout_logo_screensaver"
REAL_CLOCKS = 6_000_000


def _post_route(text: str) -> tuple[str, str]:
    i = text.rindex(build._NEXTPNR_ROUTED)
    return text[:i], text[i:]


# Plan ----------------------------------------------------------------------------------------


def test_clock_plan_for_arty() -> None:
    assert PLAN.sys_mhz == 100.0
    assert PLAN.settle == build.SETTLE == recorder.DEFAULT_SETTLE == 3
    # pclk's shortest period is 2 * (settle + 1) system cycles: 80 ns.
    assert PLAN.pclk_period_ns == pytest.approx(2 * (PLAN.settle + 1) * PLAN.sys_period_ns)
    assert PLAN.pclk_period_ns == pytest.approx(80.0)
    assert PLAN.pclk_mhz == pytest.approx(12.5)
    # sys <-> pclk: settle system cycles, 30 ns.
    assert PLAN.cross_requirement_ns == pytest.approx(PLAN.settle * PLAN.sys_period_ns)
    assert PLAN.cross_requirement_ns == pytest.approx(30.0)
    assert (FULL, HALF, XREQ - ALLOW) == pytest.approx((70.0, 30.0, 20.0))


def test_clock_plan_follows_the_settle() -> None:
    assert PLAN2.settle == 2
    assert PLAN2.pclk_period_ns == pytest.approx(60.0)
    assert PLAN2.pclk_mhz == pytest.approx(100.0 / 6)
    assert PLAN2.cross_requirement_ns == pytest.approx(20.0)
    for settle in (2, 3, 4, 7):
        plan = build.clock_plan(arty, settle=settle)
        assert plan.pclk_period_ns == pytest.approx(2 * (settle + 1) * 10.0)
        assert plan.cross_requirement_ns == pytest.approx(settle * 10.0)
        assert plan.requirement_ns("pclk", "pclk") == pytest.approx(plan.pclk_period_ns)
        assert plan.requirement_ns("pclk", "project", same_edge=False) == pytest.approx(
            plan.pclk_period_ns / 2
        )
        assert plan.requirement_ns("project", "sys") == pytest.approx(settle * 10.0)


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
    # The post-route figures, not the pre-route ones (142.82 and 92.46 MHz).
    assert timing["sys"].achieved_mhz == 183.89
    assert timing["pclk"].achieved_mhz == 97.56
    assert timing["sys"].constraint_mhz == 100.0
    assert timing["pclk"].constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    # Cross-domain: data-path delay against S system periods less the allowance (20 ns).
    assert timing["sys->pclk"].setup_slack_ns == pytest.approx(XREQ - ALLOW - 5.69)
    assert timing["pclk->sys"].setup_slack_ns == pytest.approx(XREQ - ALLOW - 0.99)
    assert timing["sys->pclk"].requirement_ns == pytest.approx(20.0)
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
    assert timing["sys"].achieved_mhz == 183.89
    assert timing["sys->pclk"].setup_slack_ns == pytest.approx(XREQ - ALLOW - 5.69)
    assert timing["pclk->sys"].setup_slack_ns == pytest.approx(XREQ - ALLOW - 0.99)
    # A data-path delay against a harness budget is not a frequency.
    assert timing["sys->pclk"].achieved_mhz is None


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
    assert timing[vsync].achieved_mhz == 304.14
    assert timing[vsync].root_kind == "register" and timing[vsync].signed_off
    assert timing[vsync].constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    assert timing["sys"].kind == "sys" and timing["pclk"].kind == "pclk"
    assert timing[vsync + "->pclk"].kind == "cross"
    # Between pclk and a register-made project clock, neither with falling-edge users: one
    # pclk period less the allowance (70 ns).
    assert timing[vsync + "->pclk"].setup_slack_ns == pytest.approx(FULL - 13.32)
    assert timing[vsync + "->pclk"].requirement_ns == pytest.approx(70.0)
    # Between sys and a project clock: S system periods less the allowance (20 ns).
    assert timing["sys->" + vsync].setup_slack_ns == pytest.approx(XREQ - ALLOW - 4.67)
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


@pytest.mark.parametrize("pair", ["sys->pclk", "pclk->sys"])
def test_gate_requires_both_sys_pclk_pairs(pair: str) -> None:
    timing = build.parse_nextpnr(NEXTPNR_PASS, PLAN, LOG)
    del timing[pair]
    with pytest.raises(TimingError, match=f"unconstrained.*{re.escape(pair)}.*absent"):
        build.check(timing, PLAN, LOG)


def test_gate_fails_on_a_failing_cross_entry() -> None:
    timing = build.parse_vivado(VIVADO_DOTTEE, PLAN, LOG)
    key = "project:project/hvsync_gen/vsync_reg/Q->pclk"
    timing[key] = dataclasses.replace(timing[key], met=False, hold_slack_ns=-0.1)
    with pytest.raises(TimingError, match="timing not met in project:"):
        build.check(timing, PLAN, LOG)


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
    plan = ClockPlan(sys_mhz=100.0, pclk_mhz=20.0, settle=PLAN.settle)
    with pytest.raises(TimingError, match=f"pclk.*timed at {float(PCLK_TXT)} MHz"):
        build.parse_nextpnr(NEXTPNR_PASS, plan, LOG)


def test_nextpnr_cross_domain_over_budget() -> None:
    pre, post = _post_route(NEXTPNR_PASS)
    assert "posedge project.clk: 5.69 ns" in post
    # Over the budget of S system periods less the allowance.
    post = post.replace(
        "posedge project.clk: 5.69 ns", f"posedge project.clk: {XREQ - ALLOW + 2.4:.2f} ns"
    )
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
    # Without the pair reports' requirements no frequency is claimed.
    assert all(d.achieved_mhz is None for d in timing.values())
    assert timing["pclk"].constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    assert timing["sys->pclk"].setup_slack_ns == 26.677
    assert timing["sys->pclk"].hold_slack_ns == 7.909
    assert timing["pclk->sys"].hold_slack_ns == 1.013
    assert timing["sys->pclk"].constraint_mhz == pytest.approx(1000 / XREQ)
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
    # A register-made clock: both edges on pclk rising edges, half pclk's frequency.
    assert timing[vsync].root_kind == "register" and timing[vsync].signed_off
    assert timing[vsync].constraint_mhz == pytest.approx(PLAN.pclk_mhz / 2, abs=1e-3)
    assert timing[vsync].setup_slack_ns == 157.764
    assert timing[vsync].requirement_ns == pytest.approx(2 * P)
    assert timing[vsync + "->pclk"].hold_slack_ns == 0.306
    assert timing["sys->" + vsync + " (async_default)"].setup_slack_ns == 27.892
    assert build.project_clocks(timing) == ("project/hvsync_gen/vsync_reg/Q",)
    assert all(d.met for d in timing.values())


def test_vivado_project_clock_needs_its_mapping() -> None:
    lines = VIVADO_DOTTEE.splitlines()
    assert lines[2].startswith("tt-harness project clock: project_clk0 on ")
    text = "\n".join(lines[:2] + lines[3:]) + "\n"  # without that line
    with pytest.raises(TimingError, match="unexpected clock project_clk0"):
        build.parse_vivado(text, PLAN, LOG)


def _pclk_summary_row(text: str) -> str:
    """pclk's line of the clock summary (name, waveform, period, frequency)."""
    return next(ln for ln in text.splitlines() if ln.startswith("  pclk ") and "{" in ln)


def _pclk_intra_row(text: str) -> str:
    """pclk's line of the intra-clock table (thirteen fields, WNS first)."""
    return next(
        ln for ln in text.splitlines() if ln.split()[:1] == ["pclk"] and len(ln.split()) == 13
    )


def _with_pclk_wns(text: str, wns: str) -> str:
    """`text` with pclk's worst setup slack replaced by `wns`."""
    row = _pclk_intra_row(text)
    return text.replace(row, row.replace(row.split()[1], wns, 1))


def _replace_row(text: str, first: str, new: str) -> str:
    out = [new if line.split()[:1] == [first] else line for line in text.splitlines()]
    return "\n".join(out) + "\n"


def test_vivado_failing_domain() -> None:
    assert _pclk_intra_row(VIVADO_PASS).split()[1] == "68.692"
    text = _with_pclk_wns(VIVADO_PASS, "-1.250")
    timing = build.parse_vivado(text, PLAN, LOG)
    assert not timing["pclk"].met
    with pytest.raises(TimingError, match="timing not met in pclk"):
        build.check(timing, PLAN, LOG)


def test_vivado_hold_violation_fails() -> None:
    assert VIVADO_PASS.count("7.909") == 1  # sys -> pclk worst hold slack
    text = VIVADO_PASS.replace("7.909", "-0.020")
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
    summary = _pclk_summary_row(VIVADO_PASS)
    extra = summary.replace("  pclk ", "  vsync", 1)
    text = VIVADO_PASS.replace(summary, f"{extra}\n{summary}")
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
    assert f"97.56 MHz (PASS at {PCLK_TXT} MHz)" in post
    post = post.replace(
        f"97.56 MHz (PASS at {PCLK_TXT} MHz)", f"10.00 MHz (FAIL at {PCLK_TXT} MHz)"
    ).replace(
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
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_FAIL)  # pclk timed at 125 MHz, not the plan's
    s = _counter()
    with pytest.raises(TimingError):
        build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert not (tmp_path / "arty_compare.bit").exists()


def test_build_compare_writes_its_record_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    s = dataclasses.replace(_counter(), warnings=("a fetch warning",))
    result = build.build_compare(
        s.project, s, "arty", "openxc7", tmp_path, clocks=100, baud=500_000
    )
    path = build.record_path(tmp_path, "arty")
    assert path == tmp_path / "arty_compare.build.json"
    record = json.loads(path.read_text())
    source = s.files[0]
    assert record == {
        "project": s.project.id,
        "repo": s.project.repo,
        "commit": s.project.commit,
        "patch_sha256": None,
        "board": "arty",
        "toolchain": "openxc7",
        "part": arty.PARTS["openxc7"],
        "clocks": 100,
        "baud": 500_000,
        "settle": build.SETTLE,
        "tt_harness_version": __version__,
        "inputs": {"src/tt_um_test_counter.v": hashlib.sha256(source.read_bytes()).hexdigest()},
        "source_rewrites": [],
        "warnings": ["a fetch warning"],
        "timing": {k: dataclasses.asdict(d) for k, d in result.timing.items()},
        "limitations": list(result.limitations),
        "input_drivers": [list(d) for d in result.input_drivers],
        "bitstream_sha256": hashlib.sha256(result.bitstream.read_bytes()).hexdigest(),
    }


def test_build_compare_leaves_no_record_after_a_timing_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build.record_path(tmp_path, "arty").write_text("{}")  # from an earlier build
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_FAIL)
    s = _counter()
    with pytest.raises(TimingError):
        build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert not build.record_path(tmp_path, "arty").exists()


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
        # The only project clock of the pool designs is dottee's vsync register: a flip-flop
        # on pclk's rising edge. Vivado gives it both edges on pclk rising edges (half
        # pclk's frequency); openXC7 the pclk frequency (twice it if half-cycle).
        assert sources.project.id == DOTTEE_ID
        assert domain.root_kind == "register"
        assert domain.root_gate_of == 1
        assert domain.signed_off
        expected = (
            PLAN.pclk_mhz / 2
            if toolchain == "vivado"
            else PLAN.pclk_mhz * (2 if domain.half_cycle else 1)
        )
        assert domain.constraint_mhz == pytest.approx(expected, abs=0.001)
    assert all(d.signed_off for d in result.timing.values())
    assert not any("not signed off" in item for item in result.limitations)
    # The gate on the implemented netlist: rst_n straight from a flip-flop, the rest
    # constants (both tools fold them into the project and drop their nets).
    drivers = dict(result.input_drivers)
    assert set(drivers) == set(input_gate.INPUT_PORTS)
    assert drivers["rst_n"] in input_gate.FLIP_FLOPS
    assert {drivers[p] for p in ("ena", "ui_in", "uio_in")} == {input_gate.FOLDED}
    assert "tt-harness project input: rst_n driven by FD" in result.log.read_text()
    # The pre-screen finds a clock made in logic exactly when the tools do.
    screen = prescreen.check(sources)
    assert screen.derived_clocks == result.prescreen_derived_clocks
    assert bool(screen.derived_clocks) == bool(result.project_clocks), (
        f"pre-screen derived_clocks {screen.derived_clocks}, tools {result.project_clocks}"
    )
    _checkout_clean(sources)
    for name, d in sorted(result.timing.items()):
        print(f"{toolchain} {sources.project.macro} {name}: {build.describe(d)}")


DOTTEE_ID = "ttsky26a/tt_um_algofoogle_dottee"


def _out(root: Path, toolchain: str, sources: Sources) -> Path:
    """The build directory under `root` (the `build_root` fixture: pytest's temporary
    directory, never the default `build/compare` tree of the CLI and the pool script)."""
    p = sources.project
    return root / f"arty-{toolchain}" / p.shuttle / p.macro


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
@pytest.mark.parametrize("project_id", [REAL_ID, DOTTEE_ID])
def test_real_build_openxc7(project_id: str, build_root: Path) -> None:
    sources = _real_sources(project_id)
    result = build.build_compare(
        sources.project,
        sources,
        "arty",
        "openxc7",
        _out(build_root, "openxc7", sources),
        REAL_CLOCKS,
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


def _source_hashes(sources: Sources) -> dict[str, str]:
    src = sources.root / "src"
    return {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(src.rglob("*"))
        if f.is_file()
    }


_DIRECTIVE_NONE = re.compile(rb"(`default_nettype[ \t]+)none\b")


def _assert_only_directives_differ(original: Path, copy: Path) -> int:
    """Every differing line is a `default_nettype none` -> `wire` directive; returns how many."""
    a = original.read_bytes().split(b"\n")
    b = copy.read_bytes().split(b"\n")
    assert len(a) == len(b)
    changed = 0
    for la, lb in zip(a, b, strict=True):
        if la == lb:
            continue
        assert _DIRECTIVE_NONE.search(la), f"{original}: changed line is no directive: {la!r}"
        assert _DIRECTIVE_NONE.sub(rb"\1wire", la) == lb, (la, lb)
        changed += _DIRECTIVE_NONE.subn(b"", la)[1]
    return changed


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_retry_can_be_forbidden(tmp_path: Path) -> None:
    sources = _real_sources()
    before = _source_hashes(sources)
    with pytest.raises(build.BuildError, match=r"Synth 8-6735.*--no-vivado-nettype-rewrite"):
        build.build_compare(
            sources.project,
            sources,
            "arty",
            "vivado",
            tmp_path,
            1000,
            vivado_nettype_rewrite=False,
        )
    assert not list(tmp_path.glob("*.bit"))
    assert not (tmp_path / "src-nettype-wire").exists()
    assert _source_hashes(sources) == before
    _checkout_clean(sources)


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
@pytest.mark.parametrize("project_id", [REAL_ID, DOTTEE_ID])
def test_real_build_vivado(project_id: str, build_root: Path) -> None:
    # The default: unmodified first, then the `default_nettype wire` retry on Synth 8-6735.
    sources = _real_sources(project_id)
    before = _source_hashes(sources)
    result = build.build_compare(
        sources.project, sources, "arty", "vivado", _out(build_root, "vivado", sources), REAL_CLOCKS
    )
    _check_real(result, sources, "vivado")
    log = result.log.read_text()
    assert "[Synth 8-6735]" in log  # from the unmodified attempt, first
    assert "retrying Vivado with `default_nettype wire` copies" in log
    # Only `default_nettype none` directives changed, in the copies; the originals are intact.
    assert _source_hashes(sources) == before
    unmodified = _unmodified_inputs(sources)
    src = sources.root / "src"
    copies = result.bitstream.parent / "src-nettype-wire"
    rewritten = dict(result.source_rewrites)
    assert rewritten, "Vivado needed the retry for this design"
    assert len(result.inputs) == len(unmodified)
    for copy in sorted(copies.rglob("*")):
        if not copy.is_file():
            continue
        original = src / copy.relative_to(copies)
        changed = _assert_only_directives_differ(original, copy)
        assert changed == rewritten.get(str(original), 0)
    for given, digest in result.inputs.items():
        original = src / Path(given).relative_to(copies)
        if str(original) not in rewritten:
            assert digest == unmodified[str(original)]
    if project_id == DOTTEE_ID:
        assert result.language == build.SYSTEMVERILOG
        assert result.project_clocks == ("project/hvsync_gen/vsync_reg/Q",)
    else:
        assert result.language == build.VERILOG
        assert result.project_clocks == ()


# The rewrite and its precondition ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected, count",
    [
        ("`default_nettype none\n", "`default_nettype wire\n", 1),
        ("`default_nettype \t none // x\n", "`default_nettype \t wire // x\n", 1),
        ("// `default_nettype none\n", "// `default_nettype none\n", 0),
        ("/* `default_nettype none\n */\n", "/* `default_nettype none\n */\n", 0),
        ('$display("`default_nettype none");\n', '$display("`default_nettype none");\n', 0),
        ("`default_nettype nonesuch\n", "`default_nettype nonesuch\n", 0),
        ("`default_nettype wire\n", "`default_nettype wire\n", 0),
        (
            "`default_nettype none\r\nmodule m; endmodule\r\n`default_nettype none\r\n",
            "`default_nettype wire\r\nmodule m; endmodule\r\n`default_nettype wire\r\n",
            2,
        ),
    ],
)
def test_rewrite_nettype_none(text: str, expected: str, count: int) -> None:
    assert build.rewrite_nettype_none(text) == (expected, count)


UNDECLARED = HERE / "data" / "projects" / "undeclared"


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("yosys") is None, reason="yosys not installed")
@pytest.mark.parametrize("toolchain", build.TOOLCHAINS)
def test_undeclared_net_fails_before_any_rewrite(tmp_path: Path, toolchain: str) -> None:
    # A real undeclared net under `default_nettype none`: Yosys rejects it, so neither flow
    # gets as far as its tools, and Vivado's rewrite is never reached.
    files = ("tt_um_test_undeclared.v",)
    project = dataclasses.replace(
        _counter().project,
        macro="tt_um_test_undeclared",
        top_module="tt_um_test_undeclared",
        source_files=files,
    )
    sources = Sources(project, UNDECLARED, (UNDECLARED / "src" / files[0],), needs_sv=False)
    before = _source_hashes(sources)
    with pytest.raises(build.BuildError, match="pre-screen.*implicitly declared"):
        build.build_compare(project, sources, "arty", toolchain, tmp_path, 100)
    assert not (tmp_path / "src-nettype-wire").exists()
    assert not list(tmp_path.glob("*.bit"))
    log = (tmp_path / "build.log").read_text()
    assert "pre-screen failed, nothing built" in log
    assert "vivado -mode" not in log and "nextpnr-xilinx" not in log
    assert _source_hashes(sources) == before


# Fix 1 ---------------------------------------------------------------------------------------

# Real lines of the logo's unmodified Vivado attempt, and one in the same format naming the
# generated top (what the reviewer saw for the edges design before the top was read first).
LOGO_SRC = "/w/work/sources/tt08/logo/src"
NETTYPE_PROJECT = (
    "ERROR: [Synth 8-6735] net type must be explicitly specified for 'clk' when "
    f"default_nettype is none [{LOGO_SRC}/vga_sync_generator.v:21]\n"
    "ERROR: [Synth 8-9844] non-net port 'clk' cannot be of mode input "
    f"[{LOGO_SRC}/vga_sync_generator.v:21]\n"
)
NETTYPE_HARNESS = (
    "ERROR: [Synth 8-6735] net type must be explicitly specified for 'clk100' when "
    "default_nettype is none [/w/build/compare/arty-vivado/tt00/edges/arty_compare.v:3]\n"
)
OTHER_ERROR = f"ERROR: [Synth 8-2716] syntax error near '>>' [{LOGO_SRC}/audio.v:194]\n"
LOGO_FILES = [Path(f"{LOGO_SRC}/vga_sync_generator.v"), Path(f"{LOGO_SRC}/palette.v")]


@pytest.mark.parametrize(
    "text, decision",
    [
        (NETTYPE_PROJECT, "retry"),
        (NETTYPE_PROJECT + NETTYPE_HARNESS, "retry"),
        (NETTYPE_HARNESS, "harness"),
        (OTHER_ERROR, "none"),
    ],
)
def test_nettype_retry_trigger(text: str, decision: str) -> None:
    got, named = build.nettype_retry(text, LOGO_FILES)
    assert got == decision
    if decision == "retry":
        assert named == [f"{LOGO_SRC}/vga_sync_generator.v"]


def test_generated_top_is_immune_to_project_directives(tmp_path: Path) -> None:
    s = _counter()
    top = arty.Top(s.project, 1_000_000, 100)
    text = arty.to_verilog(top, tmp_path / "t.v").read_text()
    assert text.startswith("`default_nettype wire\n")


def test_vivado_achieved_from_pair_requirements() -> None:
    lines = (
        "tt-harness requirement: sys -> sys setup 10.000 ns\n"
        f"tt-harness requirement: pclk -> pclk setup {P:.3f} ns\n"
        f"tt-harness requirement: sys -> pclk setup {XREQ:.3f} ns\n"
    )
    timing = build.parse_vivado(lines + VIVADO_PASS, PLAN, LOG)
    assert timing["sys"].achieved_mhz == pytest.approx(1000 / (10 - 5.503))
    assert timing["pclk"].achieved_mhz == pytest.approx(1000 / (P - 68.692))
    assert timing["sys->pclk"].achieved_mhz == pytest.approx(1000 / (XREQ - 26.677))
    assert timing["pclk->sys"].achieved_mhz is None  # no requirement line for it


def test_vivado_half_cycle_worst_path_uses_its_own_requirement() -> None:
    # If pclk's worst path were a rise-to-fall path (half a period), the frequency is
    # derived from that requirement and scaled to the clock's period.
    text = _with_pclk_wns(VIVADO_PASS, "18.768")
    lines = f"tt-harness requirement: pclk -> pclk setup {P / 2:.3f} ns\n"
    timing = build.parse_vivado(lines + text, PLAN, LOG)
    # The clock frequency at which this half-cycle path just closes (Minor 3; it was
    # 1000 / (half period - slack), twice that, not comparable with the constraint).
    assert timing["pclk"].achieved_mhz == pytest.approx(PLAN.pclk_mhz * (P / 2) / (P / 2 - 18.768))


def test_vivado_clock_with_setup_but_no_hold_figures_is_refused() -> None:
    row = _pclk_intra_row(VIVADO_PASS)
    assert row.split()[5] == "0.204"  # pclk's worst hold slack
    text = VIVADO_PASS.replace(row, row.replace("0.204", "   NA", 1))
    with pytest.raises(TimingError, match="only one of setup and hold"):
        build.parse_vivado(text, PLAN, LOG)


OPPOSITE = "Info: Max delay posedge project.clk -> negedge project.clk: 40.00 ns\n"


def test_nextpnr_opposite_edge_path_within_a_clock_is_gated() -> None:
    # Within one clock, rise to fall is half a period less the allowance (30 ns at S = 3).
    pre, post = _post_route(NEXTPNR_PASS)
    timing = build.parse_nextpnr(pre + post + OPPOSITE, PLAN, LOG)
    d = timing["pclk->pclk (opposite edges)"]
    assert d.kind == "cross" and not d.met
    assert d.setup_slack_ns == pytest.approx(HALF - 40)
    with pytest.raises(TimingError, match="timing not met in pclk->pclk"):
        build.check(timing, PLAN, LOG)


def _half_cycle(text: str) -> str:
    """The logo excerpt as if pclk had falling-edge users too (prepack line, target)."""
    line = next(
        ln for ln in text.splitlines() if ln.startswith("tt-harness clock: domain pclk net ")
    )
    text = text.replace(
        line,
        f"tt-harness clock: domain pclk net 'project.clk' {2 * PLAN.pclk_mhz!r} MHz "
        "(cells 60 rising 56 falling 4 latches 0 half-cycle; BUFG pclk_bufg)",
    )
    assert f"(PASS at {PCLK_TXT} MHz)" in text
    return text.replace(f"(PASS at {PCLK_TXT} MHz)", f"(PASS at {2 * PLAN.pclk_mhz:.2f} MHz)")


def test_nextpnr_half_cycle_clock() -> None:
    timing = build.parse_nextpnr(_half_cycle(NEXTPNR_PASS), PLAN, LOG)
    assert timing["pclk"].half_cycle
    assert timing["pclk"].constraint_mhz == pytest.approx(2 * PLAN.pclk_mhz)
    assert "constraint doubled" in build.describe(timing["pclk"])
    # The doubled target is required: timed at the plain frequency is a wrong constraint.
    wrong = _half_cycle(NEXTPNR_PASS).replace(
        f"(PASS at {2 * PLAN.pclk_mhz:.2f} MHz)", f"(PASS at {PCLK_TXT} MHz)"
    )
    with pytest.raises(
        TimingError, match=f"timed at {float(PCLK_TXT)} MHz, not {2 * PLAN.pclk_mhz:.3f}"
    ):
        build.parse_nextpnr(wrong, PLAN, LOG)


def test_undeclared_symbols(tmp_path: Path) -> None:
    src = tmp_path / "a.v"
    src.write_text("module a(clk, q);\n  input clk;\n  output reg [3:0] q;\nendmodule\n")
    log = (
        "WARNING: [Synth 8-11241] undeclared symbol 'q', assumed default net type 'wire' [a.v:1]\n"
        "WARNING: [Synth 8-11241] undeclared symbol 'ghost', assumed default net type 'wire'\n"
    )
    assert build.undeclared_symbols(log, [src]) == ["ghost"]
    assert build.undeclared_symbols("", [src]) == []


def test_rewritten_sources_must_be_under_src(tmp_path: Path) -> None:
    s = _counter()
    outside = tmp_path / "elsewhere.v"
    outside.write_text("module x; endmodule\n")
    s = dataclasses.replace(s, files=(*s.files, outside))
    job = build._Job(
        sources=s,
        board=arty,
        toolchain="vivado",
        part=arty.PARTS["vivado"],
        out_dir=tmp_path,
        top_v=tmp_path / "t.v",
        pins_xdc=tmp_path / "p.xdc",
        plan=PLAN,
        jobs=1,
        log=tmp_path / "build.log",
        language=build.VERILOG,
    )
    with pytest.raises(build.BuildError, match="not under"):
        build._vivado_rewritten_sources(job)


def test_build_compare_removes_stale_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("timing.rpt", "nextpnr.log", "sys-to-pclk-setup.rpt", "vivado.log"):
        (tmp_path / name).write_text("from an earlier build")
    (tmp_path / "src-nettype-wire").mkdir()
    seen = []

    def implement(job) -> str:
        seen.extend(p.name for p in tmp_path.iterdir())
        job.unchecked_bit.write_bytes(b"bit")
        return NEXTPNR_PASS

    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    flow = dataclasses.replace(build._FLOWS["openxc7"], implement=implement)
    monkeypatch.setitem(build._FLOWS, "openxc7", flow)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    for name in ("timing.rpt", "nextpnr.log", "sys-to-pclk-setup.rpt", "vivado.log"):
        assert name not in seen
    assert "src-nettype-wire" not in seen
    # The standing openXC7 facts, in fixed wording.
    assert build.LIMIT_NO_HOLD in result.limitations
    assert build.LIMIT_CROSS_BUDGET in result.limitations
    assert build.LIMIT_LOCAL_CLOCKS not in result.limitations  # no project clocks


def test_build_compare_reports_prescreen_disagreement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    screen = prescreen.Prescreen(0, 0, 1, True, Path("prescreen.log"))
    monkeypatch.setattr(build.prescreen, "check", lambda sources, out_dir=None: screen)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert any("disagree" in item for item in result.limitations)
    assert "disagree" in capsys.readouterr().err


# Fix 2 ---------------------------------------------------------------------------------------

# A small nextpnr log in the format of a compare build of tests/data/projects/clocks (the
# lines are from one, 2026-11-24, with the cell names shortened): pclk, sys and one
# register-made project clock, none with falling-edge users.
MINI_CLOCKS = (
    f"tt-harness clock: domain pclk net 'project_clk' {PLAN.pclk_mhz!r} MHz "
    "(cells 9 rising 9 falling 0 latches 0 intra yes; BUFG pclk_bufg)\n"
    "tt-harness clock: domain sys net 'clk0' 100.0 MHz "
    "(cells 113 rising 113 falling 0 latches 0 intra yes; BUFG sys_bufg from clk100)\n"
    f"tt-harness clock: domain project net 'project.div' {PLAN.pclk_mhz!r} MHz "
    "(cells 5 rising 5 falling 0 latches 0 intra yes; clocks ff.C)\n"
)
MINI_ROOTS = (
    "tt-harness clock root: project.div\n"
    "tt-harness clock contributor: project.div FDRE pclk rising direct no-async\n"
)
MINI_REPORT = (
    "Info: Routing complete.\n"
    f"Info: Max frequency for clock 'project_clk': 287.36 MHz (PASS at {PCLK_TXT} MHz)\n"
    "Info: Max frequency for clock        'clk0': 235.13 MHz (PASS at 100.00 MHz)\n"
    f"Info: Max frequency for clock 'project.div': 1218.03 MHz (PASS at {PCLK_TXT} MHz)\n"
    "Info: Max delay posedge clk0        -> posedge project_clk: 4.04 ns\n"
    "Info: Max delay posedge project_clk -> posedge clk0       : 0.92 ns\n"
    "Info: Max delay posedge project.div -> posedge project_clk: 45.00 ns\n"
    "Info: Max delay posedge project_clk -> posedge project.div: 1.01 ns\n"
)
MINI = MINI_CLOCKS + MINI_ROOTS + MINI_REPORT
DIV = "project:project.div"


def test_nextpnr_pair_without_falling_users_keeps_the_full_period() -> None:
    timing = build.parse_nextpnr(MINI, PLAN, LOG)
    # One pclk period less the allowance (70 ns at S = 3).
    assert timing[f"{DIV}->pclk"].setup_slack_ns == pytest.approx(FULL - 45.0)
    assert timing[f"{DIV}->pclk"].requirement_ns == pytest.approx(FULL)
    assert timing[f"pclk->{DIV}"].setup_slack_ns == pytest.approx(FULL - 1.01)
    assert all(d.met for d in timing.values())
    assert timing[DIV].root_kind == "register" and timing[DIV].signed_off
    assert timing["pclk"].root_kind is None and timing["pclk"].signed_off


@pytest.mark.parametrize(
    "old, new",
    [
        # nextpnr prints a falling-edge capture as `posedge`: any falling user of either
        # clock halves the budget of the pair.
        ("cells 9 rising 9 falling 0", "cells 9 rising 7 falling 2"),  # pclk has some
        ("cells 5 rising 5 falling 0", "cells 5 rising 0 falling 5"),  # the project clock: all
        ("cells 5 rising 5 falling 0", "cells 5 rising 4 falling 1"),
    ],
)
def test_nextpnr_pair_with_a_falling_user_gets_half_the_period(old: str, new: str) -> None:
    assert old in MINI
    timing = build.parse_nextpnr(MINI.replace(old, new), PLAN, LOG)
    d = timing[f"{DIV}->pclk"]
    # Half a pclk period less the allowance (30 ns at S = 3). The 45 ns path does not fit.
    assert d.requirement_ns == pytest.approx(HALF)
    assert d.setup_slack_ns == pytest.approx(HALF - 45.0)
    assert not d.met
    with pytest.raises(TimingError, match="timing not met in project:project.div->pclk"):
        build.check(timing, PLAN, LOG)
    assert timing[f"pclk->{DIV}"].requirement_ns == pytest.approx(HALF)
    # Pairs with sys are S system periods less the allowance whatever the edges.
    assert timing["sys->pclk"].requirement_ns == pytest.approx(XREQ - ALLOW)


def test_nextpnr_pair_with_unknown_edge_counts_gets_half_the_period() -> None:
    old = " (cells 9 rising 9 falling 0 latches 0 intra yes; BUFG pclk_bufg)"
    assert old in MINI
    timing = build.parse_nextpnr(MINI.replace(old, " (BUFG pclk_bufg)"), PLAN, LOG)
    assert timing[f"{DIV}->pclk"].requirement_ns == pytest.approx(HALF)


@pytest.mark.parametrize(
    "contributor, kind, signed_off",
    [
        ("FDRE pclk falling direct no-async", "derived", True),
        ("LDCE pclk rising direct no-async", "unclassified", False),
    ],
)
def test_nextpnr_project_clock_with_edges_off_the_pclk_rising_edge(
    contributor: str, kind: str, signed_off: bool
) -> None:
    text = MINI.replace("FDRE pclk rising direct no-async", contributor)
    timing = build.parse_nextpnr(text, PLAN, LOG)
    assert timing[DIV].root_kind == kind
    assert timing[DIV].signed_off is signed_off
    # Its edges may be on pclk falling edges: half a period either way.
    assert timing[f"{DIV}->pclk"].requirement_ns == pytest.approx(HALF)
    assert timing[f"pclk->{DIV}"].requirement_ns == pytest.approx(HALF)
    assert not timing[f"{DIV}->pclk"].met


def test_nextpnr_project_clock_without_root_facts_is_unclassified() -> None:
    timing = build.parse_nextpnr(MINI_CLOCKS + MINI_REPORT, PLAN, LOG)
    assert timing[DIV].root_kind == "unclassified"
    assert not timing[DIV].signed_off
    assert timing[f"{DIV}->pclk"].requirement_ns == pytest.approx(HALF)


def _mini_without_fmax(cells: int, intra: str) -> str:
    """MINI with project.div clocking `cells` cells, the given intra note, and no Fmax line."""
    old = "(cells 5 rising 5 falling 0 latches 0 intra yes; clocks ff.C)"
    new = f"(cells {cells} rising {cells} falling 0 latches 0{intra}; clocks ff.C)"
    assert old in MINI
    text = MINI.replace(old, new)
    return "\n".join(ln for ln in text.splitlines() if "clock 'project.div':" not in ln) + "\n"


@pytest.mark.parametrize(
    "cells, intra, passes",
    [
        (1, " intra yes", False),  # one cell feeding itself: there are paths, none reported
        (5, " intra yes", False),
        (1, " intra no", True),
        (5, " intra no", True),
        (1, "", True),  # unknown: a single cell is taken to have none
        (5, "", False),
    ],
)
def test_nextpnr_clock_without_fmax_line(cells: int, intra: str, passes: bool) -> None:
    text = _mini_without_fmax(cells, intra)
    if not passes:
        with pytest.raises(
            TimingError,
            match=rf"project.div \({cells} clocked cells.*absent from the timing report",
        ):
            build.parse_nextpnr(text, PLAN, LOG)
        return
    d = build.parse_nextpnr(text, PLAN, LOG)[DIV]
    assert d.intra_paths is False and d.achieved_mhz is None and d.met
    assert d.root_kind == "register"


def test_prepack_counts_a_self_connection_as_an_intra_clock_path() -> None:
    # The pre-pack script is run by nextpnr; here its netlist walk runs on stand-in objects.
    class Net:
        def __init__(self, name: str) -> None:
            self.name, self.driver = name, None

    class Ref:
        def __init__(self, cell, port: str) -> None:
            self.cell, self.port = cell, port

    class Port:
        def __init__(self, net) -> None:
            self.net = net

    class Cell:
        def __init__(self, name: str, kind: str, **ports) -> None:
            self.name, self.type, self.params = name, kind, []
            self.ports = Cells({p: Port(n) for p, n in ports.items()})

    class Cells(dict):
        def __iter__(self):
            return iter(self.items())

    class Ctx:
        def __init__(self, cells) -> None:
            self.cells = Cells({c.name: c for c in cells})

        def addClock(self, net: str, mhz: float) -> None:
            pass

    def run(loop: bool) -> str:
        clk100, clk_i, sys, pclk_d, pclk = (
            Net(n) for n in ("clk100", "i", "clk0", "d", "project_clk")
        )
        slow, q, other = Net("slow"), Net("q"), Net("other")
        ibuf = Cell("ibuf", "IBUF", I=clk100, O=clk_i)
        sys_bufg = Cell("sys_bufg", "BUFG", I=clk_i, O=sys)
        pclk_reg = Cell("pclk_reg", "FDRE", C=sys, D=pclk_d, Q=pclk_d)
        pclk_bufg = Cell("pclk_bufg", "BUFG", I=pclk_d, O=pclk)
        slow_reg = Cell("slow_reg", "FDRE", C=pclk, D=slow, Q=slow)
        # One cell on `slow`: Q back into its own CE (loop) or CE from a pclk cell (no loop).
        user = Cell("user", "FDRE", C=slow, D=other, CE=q if loop else slow, Q=q)
        feeder = Cell("feeder", "FDRE", C=pclk, D=other, Q=other)
        for net, cell, port in [
            (clk_i, ibuf, "O"),
            (sys, sys_bufg, "O"),
            (pclk_d, pclk_reg, "Q"),
            (pclk, pclk_bufg, "O"),
            (slow, slow_reg, "Q"),
            (q, user, "Q"),
            (other, feeder, "Q"),
        ]:
            net.driver = Ref(cell, port)
        ctx = Ctx([ibuf, sys_bufg, pclk_reg, pclk_bufg, slow_reg, user, feeder])
        out: list[str] = []
        script = build._PREPACK.format(
            input_port="clk100", sys_mhz=100.0, pclk_bufg="pclk_bufg", pclk_mhz=PLAN.pclk_mhz
        )
        scope = {"ctx": ctx, "print": lambda *a: out.append(" ".join(map(str, a)))}
        exec(script, scope)  # noqa: S102 (the script nextpnr runs, on stand-in objects)
        return "\n".join(out) + "\n"

    looped, plain = run(True), run(False)
    assert "net 'slow'" in looped and "cells 1 rising 1 falling 0 latches 0 intra yes" in looped
    assert "cells 1 rising 1 falling 0 latches 0 intra no" in plain
    # The same walk reports what drives each project clock.
    assert "tt-harness clock root: slow\n" in plain
    assert "tt-harness clock contributor: slow FDRE pclk rising direct no-async\n" in plain
    assert pc.classify(pc.parse_roots(plain))["slow"].kind == "register"


def test_requirement_ns() -> None:
    assert PLAN.requirement_ns("sys", "sys") == pytest.approx(10.0)
    assert PLAN.requirement_ns("sys", "sys", same_edge=False) == pytest.approx(5.0)
    assert PLAN.requirement_ns("sys", "pclk") == pytest.approx(30.0)
    assert PLAN.requirement_ns("project", "sys", same_edge=False) == pytest.approx(30.0)
    assert PLAN.requirement_ns("pclk", "pclk") == pytest.approx(80.0)
    assert PLAN.requirement_ns("pclk", "project", same_edge=False) == pytest.approx(40.0)
    # At the minimum settle:
    assert PLAN2.requirement_ns("sys", "pclk") == pytest.approx(20.0)
    assert PLAN2.requirement_ns("pclk", "pclk") == pytest.approx(60.0)
    assert PLAN2.requirement_ns("pclk", "project", same_edge=False) == pytest.approx(30.0)


def test_nextpnr_sys_opposite_edge_path_is_gated_at_half_a_system_period() -> None:
    line = "Info: Max delay posedge clk0 -> negedge clk0: 0.50 ns\n"
    timing = build.parse_nextpnr(MINI + line, PLAN, LOG)
    d = timing["sys->sys (opposite edges)"]
    # 5 ns less the 10 ns allowance: no budget at all, so it cannot pass.
    assert d.requirement_ns == pytest.approx(-5.0)
    assert not d.met


# Vivado: the logo report with one project clock added, as the flow would report it.


def _vivado_with_project_clock(kind: str, row: str, extra: str = "") -> str:
    """VIVADO_PASS plus `project_clk0` of root class `kind` with intra-clock row `row`."""
    period, wave = (
        (f"{2 * P:.3f}", f"{{0.000 {P:.3f}}}")
        if kind == "register"
        else (f"{P:.3f}", f"{{0.000 {P / 2:.3f}}}")
    )
    contributor = {
        "register": "FDRE pclk rising direct no-async",
        "derived": "FDRE pclk falling direct no-async",
        "unclassified": "LDCE pclk falling direct no-async",
    }[kind]
    head = (
        "tt-harness clock root: project/x_reg/Q\n"
        f"tt-harness clock contributor: project/x_reg/Q {contributor}\n"
        f"tt-harness project clock: project_clk0 on project/x_reg/Q kind {kind}\n" + extra
    )
    summary = _pclk_summary_row(VIVADO_PASS)
    intra = _pclk_intra_row(VIVADO_PASS)
    text = VIVADO_PASS.replace(
        summary,
        f"{summary}\n    project_clk0  {wave}     {period}          {1000 / float(period):.3f}",
    )
    return head + text.replace(intra, f"{intra}\n    project_clk0  {row}")


NO_PATHS_ROW = "                                        29.500        0.000        0        12"
PATHS_ROW = "57.741  0.000  0  12  0.292  0.000  0  12  29.500  0.000  0  12"
X = "project:project/x_reg/Q"


@pytest.mark.parametrize(
    "kind, mhz, signed_off",
    [
        ("register", 1000 / (2 * P), True),
        ("derived", 1000 / P, True),
        ("unclassified", 1000 / P, False),
    ],
)
def test_vivado_project_clock_records_its_root(kind: str, mhz: float, signed_off: bool) -> None:
    timing = build.parse_vivado(_vivado_with_project_clock(kind, PATHS_ROW), PLAN, LOG)
    d = timing[X]
    assert d.root_kind == kind
    assert d.signed_off is signed_off
    assert d.constraint_mhz == pytest.approx(mhz)
    assert timing["pclk"].root_kind is None and timing["pclk"].signed_off
    line = build.describe(d)
    assert f"{kind} clock" in line
    assert ("NOT SIGNED OFF" in line) == (not signed_off)


def test_vivado_project_clock_period_must_match_its_kind() -> None:
    doubled, single = f"{2 * P:.3f}", f"{P:.3f}"
    text = _vivado_with_project_clock("register", PATHS_ROW).replace(
        f"{doubled}   ", single.ljust(len(doubled) + 3)
    )
    with pytest.raises(TimingError, match=f"period {P:.1f} ns, not {doubled}"):
        build.parse_vivado(text, PLAN, LOG)


def test_vivado_project_clock_kind_must_match_its_root_facts() -> None:
    # Constrained as a register clock, but the root is made on pclk's falling edge.
    text = _vivado_with_project_clock("register", PATHS_ROW).replace(
        "FDRE pclk rising direct", "FDRE pclk falling direct"
    )
    with pytest.raises(TimingError, match="constrained as register.*derived"):
        build.parse_vivado(text, PLAN, LOG)
    text = "\n".join(_vivado_with_project_clock("register", PATHS_ROW).splitlines()[2:]) + "\n"
    with pytest.raises(TimingError, match="no clock root facts"):
        build.parse_vivado(text, PLAN, LOG)


def test_vivado_project_clock_line_in_another_format_is_refused() -> None:
    text = _vivado_with_project_clock("register", PATHS_ROW).replace(
        "on project/x_reg/Q kind register",
        "on project/x_reg/Q from project/x_reg/Q (master pin project/x_reg/C) kind gate",
    )
    with pytest.raises(TimingError, match="cannot read"):
        build.parse_vivado(text, PLAN, LOG)


NO_PATHS = "tt-harness no paths: project_clk0 -> project_clk0\n"


def test_vivado_project_clock_without_paths_needs_the_pair_report_to_agree() -> None:
    text = _vivado_with_project_clock("register", NO_PATHS_ROW, NO_PATHS)
    d = build.parse_vivado(text, PLAN, LOG)[X]
    assert d.intra_paths is False and d.achieved_mhz is None and d.met
    # Without the pair report saying so, an empty row is not "no paths".
    text = _vivado_with_project_clock("register", NO_PATHS_ROW)
    with pytest.raises(TimingError, match="project_clk0 has no setup or hold figures"):
        build.parse_vivado(text, PLAN, LOG)
    # NA in every column is the same.
    na = "NA  NA  NA  NA  NA  NA  NA  NA  29.500  0.000  0  12"
    with pytest.raises(TimingError, match="project_clk0 has no setup or hold figures"):
        build.parse_vivado(_vivado_with_project_clock("register", na), PLAN, LOG)
    assert not build.parse_vivado(_vivado_with_project_clock("register", na, NO_PATHS), PLAN, LOG)[
        X
    ].intra_paths


@pytest.mark.parametrize("clock", ["sys", "pclk"])
def test_vivado_sys_and_pclk_are_never_accepted_without_paths(clock: str) -> None:
    # Doctored copies of the real report: the clock's own row emptied, as Vivado prints a
    # clock without paths, with and without a pair-report line claiming there are none.
    row = next(
        ln for ln in VIVADO_PASS.splitlines() if ln.split()[:1] == [clock] and len(ln.split()) == 13
    )
    fields = row.split()
    empty = f"{fields[0]}  " + NO_PATHS_ROW
    na = "  ".join([fields[0], *["NA"] * 8, *fields[9:]])
    for doctored in (empty, na):
        for head in ("", f"tt-harness no paths: {clock} -> {clock}\n"):
            with pytest.raises(TimingError, match=f"{clock} has no setup or hold figures"):
                build.parse_vivado(head + VIVADO_PASS.replace(row, doctored), PLAN, LOG)


def test_no_path_lines_come_from_the_pair_reports(tmp_path: Path) -> None:
    (tmp_path / "project_clk0-to-project_clk0-setup.rpt").write_text(
        "Timing Report\n\nNo timing paths found.\n"
    )
    # Arbitrary parser input: the requirement is whatever the report says (60 ns here,
    # not a value of the current plan).
    (tmp_path / "pclk-to-pclk-setup.rpt").write_text(
        "Slack (MET) :  1.0ns\n  Requirement:            60.000ns  (pclk rise@60.000ns - ...)\n"
    )
    (tmp_path / "sys-to-pclk-hold.rpt").write_text("No timing paths found.\n")
    assert build._pair_report_lines(tmp_path) == [
        "tt-harness requirement: pclk -> pclk setup 60.000 ns",
        "tt-harness no paths: project_clk0 -> project_clk0",
    ]


def test_describe_never_prints_an_incomparable_frequency() -> None:
    # A clock whose worst path is a half-cycle one: the frequency is the one the clock could
    # run at (twice the path's delay as a period), and the line says so.
    text = _with_pclk_wns(VIVADO_PASS, "18.768")
    lines = f"tt-harness requirement: pclk -> pclk setup {P / 2:.3f} ns\n"
    d = build.parse_vivado(lines + text, PLAN, LOG)["pclk"]
    assert d.requirement_ns == P / 2
    achieved = PLAN.pclk_mhz * (P / 2) / (P / 2 - 18.768)  # not 1000 / (P / 2 - 18.768)
    assert d.achieved_mhz == pytest.approx(achieved)
    line = build.describe(d)
    assert f"{achieved:.2f} MHz achieved against {PCLK_TXT} MHz" in line
    assert f"worst setup path has a {P / 2:.3f} ns requirement" in line
    # A clock pair is a requirement in ns, never a frequency.
    lines = (
        f"tt-harness requirement: sys -> pclk setup {XREQ:.3f} ns\n"
        f"tt-harness requirement: pclk -> sys setup {XREQ:.3f} ns\n"
    )
    timing = build.parse_vivado(lines + VIVADO_PASS, PLAN, LOG)
    for key in ("sys->pclk", "pclk->sys"):
        line = build.describe(timing[key])
        assert "MHz" not in line
        assert line.startswith(f"requirement {XREQ:.3f} ns, setup slack ")
    # nextpnr pairs too: the budget in ns.
    line = build.describe(build.parse_nextpnr(MINI, PLAN, LOG)[f"{DIV}->pclk"])
    assert "MHz" not in line
    assert line.startswith(f"requirement {FULL:.3f} ns, setup slack {FULL - 45:.3f} ns")


def test_vivado_pair_constraint_is_its_worst_paths_requirement() -> None:
    # A pair whose worst path has another requirement than the nominal S system periods
    # is printed and scaled against its own.
    own = XREQ + 10.0
    lines = f"tt-harness requirement: sys -> pclk setup {own:.3f} ns\n"
    d = build.parse_vivado(lines + VIVADO_PASS, PLAN, LOG)["sys->pclk"]
    assert d.requirement_ns == own
    assert d.constraint_mhz == pytest.approx(1000 / own)
    assert d.achieved_mhz == pytest.approx(1000 / (own - 26.677))


def test_undeclared_symbols_ignores_names_in_initialisers_and_comments(tmp_path: Path) -> None:
    src = tmp_path / "a.v"
    src.write_text(
        "module a(input wire clk, input wire [1:0] sel,\n"
        "         output reg [3:0] q);\n"
        "  wire y = ghost & sel[0];\n"
        "  wire [3:0] z = {phantom, 3'b0}, w;\n"
        "  // wire commented;\n"
        "  /* reg blocked; */\n"
        "  localparam [3:0] K = spectre, L = 4'd2;\n"
        "  reg [7:0] mem [0:3];\n"
        "  integer i;\n"
        "endmodule\n"
    )
    names = ["clk", "sel", "q", "y", "z", "w", "K", "L", "mem", "i"]
    ghosts = ["ghost", "phantom", "commented", "blocked", "spectre"]
    log = "".join(
        f"WARNING: [Synth 8-11241] undeclared symbol '{n}', assumed default net type 'wire'\n"
        for n in names + ghosts
    )
    assert build.undeclared_symbols(log, [src]) == sorted(ghosts)


def _clock_report(kind: str) -> str:
    contributor = (
        "LDCE pclk rising direct no-async"
        if kind == "unclassified"
        else (
            "FDRE pclk rising direct no-async\n"
            "tt-harness clock contributor: project.div FDRE pclk rising direct no-async"
        )
    )
    return MINI.replace("FDRE pclk rising direct no-async", contributor)


def test_build_compare_flags_a_clock_that_is_not_signed_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _clock_report("unclassified").replace("45.00 ns", "4.50 ns")
    _fake_flow(monkeypatch, "openxc7", report)
    screen = prescreen.Prescreen(0, 0, 1, True, Path("prescreen.log"))
    monkeypatch.setattr(build.prescreen, "check", lambda sources, out_dir=None: screen)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert result.bitstream.is_file()  # built and flagged, not refused
    d = result.timing[DIV]
    assert d.root_kind == "unclassified" and not d.signed_off
    assert (
        "openXC7: project clock project.div is not signed off: a LDCE, not a flip-flop, drives "
        "it; it was timed as a clock with edges on either pclk edge, which is an assumption"
    ) in result.limitations
    assert "tt-harness limitation: openXC7: project clock project.div is not signed off" in (
        result.log.read_text()
    )
    assert "NOT SIGNED OFF" in result.log.read_text()


@pytest.mark.parametrize(
    "contributor, limitation",
    [
        (
            "FDCE pclk rising direct async",
            (
                "openXC7: project clock project.div is not signed off: a FDCE with an asynchronous "
                "set or clear drives it; its edges are modelled on pclk edges, but an asynchronous "
                "set or clear can move one"
            ),
        ),
        (
            "FDRE sys rising direct no-async",
            (
                "openXC7: project clock project.div is not signed off: a flip-flop clocked by sys "
                "drives it; it was timed as a primary clock with pclk's waveform and no modelled "
                "insertion delay relative to pclk and sys, so its cross-domain setup and hold "
                "figures are indicative only"
            ),
        ),
        (
            "FDRE pclk falling through-logic no-async",
            (
                "openXC7: project clock project.div is not signed off: a flip-flop clocked by pclk "
                "through logic, whose other inputs are not examined, drives it; it was timed as a "
                "clock with edges on either pclk edge, which is an assumption"
            ),
        ),
    ],
)
def test_build_compare_says_what_is_assumed_for_each_unsigned_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contributor: str, limitation: str
) -> None:
    report = MINI.replace("FDRE pclk rising direct no-async", contributor)
    report = report.replace("45.00 ns", "4.50 ns")
    _fake_flow(monkeypatch, "openxc7", report)
    screen = prescreen.Prescreen(0, 0, 1, True, Path("prescreen.log"))
    monkeypatch.setattr(build.prescreen, "check", lambda sources, out_dir=None: screen)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    d = result.timing[DIV]
    assert d.root_kind == "unclassified" and not d.signed_off
    assert [item for item in result.limitations if "not signed off" in item] == [limitation]


def test_build_compare_limitations_for_a_signed_off_gate_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _clock_report("register")
    lines = [ln for ln in report.splitlines() if "clock 'project.div':" not in ln]
    report = "\n".join(lines).replace("intra yes; clocks ff.C", "intra no; clocks ff.C") + "\n"
    _fake_flow(monkeypatch, "openxc7", report)
    screen = prescreen.Prescreen(0, 0, 1, True, Path("prescreen.log"))
    monkeypatch.setattr(build.prescreen, "check", lambda sources, out_dir=None: screen)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert result.timing[DIV].signed_off
    assert not any("not signed off" in item for item in result.limitations)
    assert (
        "openXC7: project clock project.div is a gate of 2 flip-flops; a glitch at the gate is "
        "not timed"
    ) in result.limitations
    # The toolchain is named the same way in every entry.
    assert "openXC7: project:project.div has no paths within its own clock" in result.limitations
    assert not any(item.startswith("openxc7") for item in result.limitations)


def test_build_compare_removes_stale_vivado_backup_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = ["vivado_12345.backup.log", "clock-roots.txt", "project-clocks.tcl"]
    for name in stale:
        (tmp_path / name).write_text("from an earlier build")
    seen: list[str] = []

    def implement(job) -> str:
        seen.extend(p.name for p in tmp_path.iterdir())
        job.unchecked_bit.write_bytes(b"bit")
        return NEXTPNR_PASS

    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    flow = dataclasses.replace(build._FLOWS["openxc7"], implement=implement)
    monkeypatch.setitem(build._FLOWS, "openxc7", flow)
    s = _counter()
    build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert not set(stale) & set(seen)


def test_vivado_clock_script_collects_roots_and_runs_the_classifier(tmp_path: Path) -> None:
    tcl = build.vivado_clocks_tcl(PLAN, arty, tmp_path)
    assert "create_clock -name sys -period 10.000 [get_ports clk100]" in tcl
    assert f"create_generated_clock -name pclk -divide_by {2 * (PLAN.settle + 1)}" in tcl
    assert "create_generated_clock -name pclk -divide_by 8" in tcl  # settle 3
    # Facts for every root, then the constraints written from them by project_clocks.py.
    assert 'tt_fact "tt-harness clock root: $root"' in tcl
    assert "all_fanin -quiet -flat -only_cells $root" in tcl
    roots, out = tmp_path / "clock-roots.txt", tmp_path / "project-clocks.tcl"
    script = Path(pc.__file__)
    assert (
        f"exec /usr/bin/env -i {{{sys.executable}}} -I {{{script}}} {{{roots}}} {{{out}}} "
        f"pclk_bufg/O {P:.3f}"
    ) in tcl
    assert f"source {{{out}}}" in tcl
    assert tcl.index("close $tt_facts") < tcl.index("exec ") < tcl.index(f"source {{{out}}}")
    # No project-clock waveform is chosen in this script any more.
    assert "-edges" not in tcl and "kind gate" not in tcl
    # The approved sys <-> pclk relationship, applied to pclk and every project clock.
    for line in (
        "set_multicycle_path -setup -start 3 -from [get_clocks sys] -to $c",
        "set_multicycle_path -hold -start 3 -from [get_clocks sys] -to $c",
        "set_multicycle_path -setup -end 3 -from $c -to [get_clocks sys]",
        "set_multicycle_path -hold -end 2 -from $c -to [get_clocks sys]",
    ):
        assert line in tcl


@pytest.mark.parametrize("settle", [2, 3, 5])
def test_vivado_clock_script_follows_the_settle(tmp_path: Path, settle: int) -> None:
    # The approved scheme keeps its form; only its numbers change with the settle S:
    # pclk is the system clock divided by 2 (S + 1); sys -> pclk has S system cycles for
    # setup with the hold check moved by the same S; pclk -> sys has S cycles, hold S - 1.
    plan = build.clock_plan(arty, settle=settle)
    tcl = build.vivado_clocks_tcl(plan, arty, tmp_path)
    assert f"create_generated_clock -name pclk -divide_by {2 * (settle + 1)} " in tcl
    assert f"pclk_bufg/O {20.0 * (settle + 1):.3f}\n" in tcl
    wanted = [
        f"set_multicycle_path -setup -start {settle} -from [get_clocks sys] -to $c",
        f"set_multicycle_path -hold -start {settle} -from [get_clocks sys] -to $c",
        f"set_multicycle_path -setup -end {settle} -from $c -to [get_clocks sys]",
        f"set_multicycle_path -hold -end {settle - 1} -from $c -to [get_clocks sys]",
    ]
    got = [ln.strip() for ln in tcl.splitlines() if "set_multicycle_path" in ln]
    assert got == wanted
    assert "set_false_path" not in tcl and "set_clock_groups" not in tcl
    if settle == 2:
        assert "-divide_by 6 " in tcl and "pclk_bufg/O 60.000\n" in tcl


# Real builds of the project-clock designs ------------------------------------------------------

CLOCKS = ("clocks", "tt_um_test_clocks")  # register-class clocks: gate, ripple, two registers
FALLCLK = ("fallclk", "tt_um_test_fallclk")  # a falling-edge register clock and a latch clock
FALLREG = ("fallreg", "tt_um_test_fallreg")  # the falling-edge register clock alone


def _pair_requirements(out: Path, a: str, b: str, tag: str) -> list[tuple[float, str]]:
    """(requirement ns, the rest of the line) of every path in a pair report."""
    report = (out / f"{a}-to-{b}-{tag}.rpt").read_text()
    return [
        (float(m.group(1)), m.group(2))
        for m in re.finditer(r"Requirement:\s+(-?[\d.]+)ns\s+\((.*)\)", report)
    ]


def _vivado_names(result: build.BuildResult) -> dict[str, str]:
    """Signal name -> Vivado clock name, from the build log's mapping lines."""
    names = {}
    for m in build._VIVADO_PROJECT_CLOCK.finditer(result.log.read_text()):
        names[m.group("root")] = m.group("name")
    return names


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_register_class_clocks(build_root: Path) -> None:
    _, result = _local_build(CLOCKS, "vivado", build_root)
    out = result.bitstream.parent
    names = _vivado_names(result)
    by_signal = {}
    for root, name in names.items():
        signal = next(s for s in ("capg", "div2", "div", "r2") if f"/{s}" in root)
        by_signal[signal] = (root, name)
    assert set(by_signal) == {"capg", "div2", "div", "r2"}, names
    # pclk -> the falling edge of the gate-made and of the ripple-stage clock: those edges
    # are on pclk rising edges, so the hold requirement is 0 ns (pclk's own waveform gave
    # minus half a period) and setup is a whole pclk period.
    for signal in ("capg", "div2"):
        _, name = by_signal[signal]
        hold = [r for r in _pair_requirements(out, "pclk", name, "hold") if "fall@" in r[1]]
        assert hold, f"no hold path to the falling edge of {name}"
        assert all(abs(req) < 0.001 for req, _ in hold), hold
        setup = [r for r in _pair_requirements(out, "pclk", name, "setup") if "fall@" in r[1]]
        assert setup and all(req == P for req, _ in setup), setup
    # Two different register-made clocks: `capx` on r2 takes `a` from the div domain. r2 can
    # rise one pclk period after div, so setup is one pclk period (not two), and hold stays
    # analysed
    # at 0 ns.
    div, r2 = by_signal["div"][1], by_signal["r2"][1]
    setup = _pair_requirements(out, div, r2, "setup")
    assert setup and all(req == P for req, _ in setup), setup
    hold = _pair_requirements(out, div, r2, "hold")
    assert hold and all(req == 0.0 for req, _ in hold), hold
    pair = result.timing[
        f"{build.PROJECT}{by_signal['div'][0]}->{build.PROJECT}{by_signal['r2'][0]}"
    ]
    assert pair.requirement_ns == P
    assert pair.hold_slack_ns is not None and pair.met
    # Every project clock is register class, at half pclk's frequency, and signed off.
    assert len(result.project_clocks) == 4
    for root in result.project_clocks:
        d = result.timing[f"{build.PROJECT}{root}"]
        assert d.root_kind == "register" and d.signed_off
        assert d.constraint_mhz == pytest.approx(PLAN.pclk_mhz / 2)
    assert not any("not signed off" in item for item in result.limitations)
    gate_root = by_signal["capg"][0]
    assert (
        f"Vivado: project clock {gate_root} is a gate of 2 flip-flops; a glitch at the gate is "
        "not timed"
    ) in result.limitations


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_clocks_off_the_pclk_rising_edge(build_root: Path) -> None:
    _, result = _local_build(FALLCLK, "vivado", build_root)
    out = result.bitstream.parent
    names = _vivado_names(result)
    (nq_root,) = [r for r in names if "/nq" in r]
    (lq_root,) = [r for r in names if "/lq" in r]
    nq, lq = result.timing[f"{build.PROJECT}{nq_root}"], result.timing[f"{build.PROJECT}{lq_root}"]
    assert nq.root_kind == "derived" and nq.signed_off
    assert lq.root_kind == "unclassified" and not lq.signed_off
    assert nq.constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    assert lq.constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    # Their edges may be on either pclk edge: half a pclk period for setup and the same edge
    # for hold, on every path between pclk and them.
    for root in (nq_root, lq_root):
        setup = _pair_requirements(out, "pclk", names[root], "setup")
        assert setup and all(req == P / 2 for req, _ in setup), setup
        hold = _pair_requirements(out, "pclk", names[root], "hold")
        assert hold and all(req == 0.0 for req, _ in hold), hold
    assert [item for item in result.limitations if "not signed off" in item] == [
        (
            f"Vivado: project clock {lq_root} is not signed off: a LDCE, not a flip-flop, "
            "drives it; it was timed as a clock with edges on either pclk edge, which is an "
            "assumption"
        )
    ]


ASYNCCLK = ("asyncclk", "tt_um_test_asyncclk")  # a clock made by an async-reset register


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_async_reset_register_clock_is_generated_from_pclk(
    build_root: Path,
) -> None:
    _, result = _local_build(ASYNCCLK, "vivado", build_root)
    out = result.bitstream.parent
    (root,) = result.project_clocks
    (name,) = _vivado_names(result).values()
    d = result.timing[f"{build.PROJECT}{root}"]
    assert d.root_kind == "unclassified" and not d.signed_off
    assert d.constraint_mhz == pytest.approx(PLAN.pclk_mhz)
    # A generated clock of pclk, not a primary clock on the register's output.
    tcl = (out / "project-clocks.tcl").read_text()
    assert f"create_generated_clock -name {name} -divide_by 1" in tcl
    assert "create_clock" not in tcl
    # pclk -> the clock: hold is checked on the same pclk edge, with both clocks' insertion
    # delays in the check. The clock's delay includes pclk's own (a primary clock on the
    # register output had 0.3 to 0.6 ns), so it is longer than the source's.
    hold = _pair_requirements(out, "pclk", name, "hold")
    assert hold and all(req == 0.0 for req, _ in hold), hold
    setup = _pair_requirements(out, "pclk", name, "setup")
    assert setup and all(req == P / 2 for req, _ in setup), setup
    report = (out / f"pclk-to-{name}-hold.rpt").read_text()
    dcd = [float(x) for x in re.findall(r"Destination Clock Delay \(DCD\):\s+([\d.]+)ns", report)]
    scd = [float(x) for x in re.findall(r"Source Clock Delay\s+\(SCD\):\s+([\d.]+)ns", report)]
    assert dcd and scd and len(dcd) == len(scd)
    assert all(s > 2.0 for s in scd), scd
    assert all(d > s for d, s in zip(dcd, scd, strict=True)), (dcd, scd)
    assert [item for item in result.limitations if "not signed off" in item] == [
        (
            f"Vivado: project clock {root} is not signed off: a FDCE with an asynchronous set "
            "or clear drives it; its edges are modelled on pclk edges, but an asynchronous set "
            "or clear can move one"
        )
    ]


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_register_class_clocks(build_root: Path) -> None:
    _, result = _local_build(CLOCKS, "openxc7", build_root)
    assert len(result.project_clocks) == 4
    for root in result.project_clocks:
        d = result.timing[f"{build.PROJECT}{root}"]
        assert d.root_kind == "register" and d.signed_off, (root, d)
    # pclk has a falling-edge user (`nf`), and two project clocks have only falling-edge
    # users: every pair among pclk and the project clocks is budgeted at half a period.
    pairs = {
        k: d for k, d in result.timing.items() if d.kind == "cross" and "sys" not in k.split("->")
    }
    with_pclk = {k: d for k, d in pairs.items() if "pclk" in k.split("->")}
    assert with_pclk
    assert all(d.requirement_ns == pytest.approx(HALF) for d in with_pclk.values()), with_pclk
    # `div` and `r2` have rising-edge users only: a whole period between them.
    (div_r2,) = [d for k, d in pairs.items() if "div->" in k and k.endswith("r2")]
    assert div_r2.requirement_ns == pytest.approx(FULL)
    assert not any("not signed off" in item for item in result.limitations)


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_clock_made_on_the_pclk_falling_edge(build_root: Path) -> None:
    # (The latch of the fallclk design cannot be placed by nextpnr-xilinx: "no Bels
    # remaining of type 'LDCE'".)
    _, result = _local_build(FALLREG, "openxc7", build_root)
    (root,) = result.project_clocks
    d = result.timing[f"{build.PROJECT}{root}"]
    assert d.root_kind == "derived" and d.signed_off
    assert "falling edge of pclk" in d.root_reason
    # Its edges are on pclk falling edges: half a period, less the allowance, both ways.
    pairs = {
        k: v for k, v in result.timing.items() if v.kind == "cross" and "sys" not in k.split("->")
    }
    assert pairs
    assert all(v.requirement_ns == pytest.approx(HALF) for v in pairs.values()), pairs
    assert not any("not signed off" in item for item in result.limitations)


# Real builds of the local test designs ---------------------------------------------------------


def _local(name: str, top_module: str) -> Sources:
    root = HERE / "data" / "projects" / name
    files = (f"{top_module}.v",)
    project = dataclasses.replace(
        _counter().project, macro=top_module, top_module=top_module, source_files=files
    )
    return Sources(project, root, tuple(root / "src" / f for f in files), needs_sv=False)


EDGES = ("edges", "tt_um_test_edges")  # both edges of clk
NEGCLK = ("negclk", "tt_um_test_negclk")  # falling edge of a flip-flop-made clock


def _local_build(
    design: tuple[str, str], toolchain: str, root: Path
) -> tuple[Sources, build.BuildResult]:
    sources = _local(*design)
    before = _source_hashes(sources)
    out = _out(root, toolchain, sources)
    result = build.build_compare(sources.project, sources, "arty", toolchain, out, 100_000)
    assert _source_hashes(sources) == before
    assert result.bitstream.is_file()
    assert all(d.met for d in result.timing.values())
    assert bool(result.project_clocks) == bool(prescreen.check(sources).derived_clocks)
    assert dict(result.input_drivers)["rst_n"] in input_gate.FLIP_FLOPS
    for name, d in sorted(result.timing.items()):
        print(f"{toolchain} {sources.project.macro} {name}: {build.describe(d)}")
    return sources, result


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_both_edge_design_checks_half_cycles(build_root: Path) -> None:
    _, result = _local_build(EDGES, "openxc7", build_root)
    pclk = result.timing["pclk"]
    assert pclk.half_cycle
    assert pclk.constraint_mhz == pytest.approx(2 * PLAN.pclk_mhz)
    assert any("twice its frequency" in item for item in result.limitations)
    assert result.project_clocks == ()


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_negedge_of_a_register_clock(build_root: Path) -> None:
    _, result = _local_build(NEGCLK, "openxc7", build_root)
    assert len(result.project_clocks) == 1
    assert not result.timing["pclk"].half_cycle  # pclk itself is used on its rising edge
    assert build.LIMIT_LOCAL_CLOCKS in result.limitations
    (root,) = result.project_clocks
    assert result.timing[f"{build.PROJECT}{root}"].root_kind == "register"
    # The project clock has falling-edge users only (`rising 0 falling 4`): pclk -> it is
    # budgeted at half a pclk period less the allowance, not at a whole one.
    assert result.timing[f"pclk->{build.PROJECT}{root}"].requirement_ns == pytest.approx(HALF)


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_clean_project_is_not_rewritten(build_root: Path) -> None:
    # The edges design has `default_nettype none` and ANSI ports: Vivado accepts it as it
    # is, and the generated top (read first, `default_nettype wire`) is never the trigger.
    _, result = _local_build(EDGES, "vivado", build_root)
    assert result.source_rewrites == ()
    assert "[Synth 8-6735]" not in result.log.read_text()


@pytest.mark.vivado
@pytest.mark.skipif(tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_negedge_of_a_register_clock_is_timed_tightly(build_root: Path) -> None:
    _, result = _local_build(NEGCLK, "vivado", build_root)
    assert result.source_rewrites == ()
    (root,) = result.project_clocks
    clock = result.timing[f"{build.PROJECT}{root}"]
    assert clock.constraint_mhz == pytest.approx(PLAN.pclk_mhz / 2)  # -edges {1 3 5}
    assert clock.root_kind == "register" and clock.signed_off
    # pclk -> the project clock's falling edge: that edge lies on a pclk rising edge, so the
    # hold requirement is 0 (pclk's own falling edge would have given minus half a period).
    report = (result.bitstream.parent / "pclk-to-project_clk0-hold.rpt").read_text()
    reqs = re.findall(r"Requirement:\s+(-?[\d.]+)ns\s+\(project_clk0 fall@", report)
    assert reqs, "no hold path to the project clock's falling edge in the report"
    assert all(abs(float(r)) < 0.001 for r in reqs), reqs
