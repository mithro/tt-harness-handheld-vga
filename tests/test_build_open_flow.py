# SPDX-License-Identifier: Apache-2.0
"""The open flow without DSP inference, a tied-off clock pin, and where tests build."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import build_tree
import pytest
from test_build import (
    NEXTPNR_PASS,
    PLAN,
    VIVADO_PASS,
    _counter,
    _fake_flow,
    _local,
    _openxc7_installed,
)

from tt_harness import build, prescreen
from tt_harness import project_clocks as pc
from tt_harness.boards import arty

MUL = ("mul", "tt_um_test_mul")  # two small multiplications: DSP48E1 if inference is on


# No DSP inference in the open flow -----------------------------------------------------------


def _job(tmp_path: Path, toolchain: str) -> build._Job:
    s = _counter()
    return build._Job(
        sources=s,
        board=arty,
        toolchain=toolchain,
        part=arty.PARTS[toolchain],
        out_dir=tmp_path,
        top_v=arty.to_verilog(arty.Top(s.project, 1_000_000, 100), tmp_path / "arty_compare.v"),
        pins_xdc=tmp_path / "pins.xdc",
        plan=PLAN,
        jobs=1,
        log=tmp_path / "build.log",
        language=build.VERILOG,
    )


def test_open_flow_synthesis_script_turns_dsp_inference_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stop(Exception):
        pass

    def step(job, what, cmd, cwd, **kwargs):
        raise Stop(what)

    monkeypatch.setattr(build, "_step", step)
    with pytest.raises(Stop, match="synthesis"):
        build._openxc7_implement(_job(tmp_path, "openxc7"))
    script = (tmp_path / "synth.ys").read_text()
    assert "synth_xilinx -flatten -abc9 -noclkbuf -nodsp -arch xc7 -top arty_compare\n" in script


def test_vivado_script_is_not_told_about_dsp(tmp_path: Path) -> None:
    job = _job(tmp_path, "vivado")
    text = build._vivado_script(job, list(job.sources.files), []).read_text()
    assert "dsp" not in text.lower()


@pytest.mark.parametrize(
    "toolchain, report, expected", [("openxc7", NEXTPNR_PASS, True), ("vivado", VIVADO_PASS, False)]
)
def test_no_dsp_limitation_is_on_every_open_flow_build_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toolchain: str, report: str, expected: bool
) -> None:
    _fake_flow(monkeypatch, toolchain, report)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", toolchain, tmp_path, clocks=100)
    assert (build.LIMIT_NO_DSP in result.limitations) is expected
    assert build.LIMIT_NO_DSP == (
        "openXC7: synthesised without DSP inference (synth_xilinx -nodsp), so multipliers are "
        "built from LUTs and carry chains: nextpnr-xilinx 0.8.2 cannot route the constant "
        "cascade inputs of inferred DSP48E1 cells"
    )
    if expected:
        assert f"tt-harness limitation: {build.LIMIT_NO_DSP}" in result.log.read_text()


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_build_openxc7_multiplier_design_builds_without_dsp(build_root: Path) -> None:
    sources = _local(*MUL)
    out = build_root / "arty-openxc7" / "mul"
    result = build.build_compare(sources.project, sources, "arty", "openxc7", out, 100_000)
    assert result.bitstream.is_file()
    assert all(d.met for d in result.timing.values())
    assert "DSP48E1" not in (out / "utilization-yosys.txt").read_text()
    assert result.project_clocks == ()
    assert build.LIMIT_NO_DSP in result.limitations
    log = result.log.read_text()
    assert "tt-harness tied-off clock pin" not in log
    assert "domain project" not in log


@pytest.mark.toolchain
@pytest.mark.skipif(not _openxc7_installed(), reason="openXC7 toolchain not installed")
def test_real_netlist_with_inferred_dsp_has_no_spurious_project_clock(
    monkeypatch: pytest.MonkeyPatch, build_root: Path
) -> None:
    # With DSP inference left on (as before Task 9b) Yosys makes a DSP48E1 with no register
    # packed into it, whose CLK pin is tied off. nextpnr-xilinx 0.8.2 then fails in one way
    # or another (for this design "Port BCOUT15 has no connections", for the pool designs
    # an unrouteable constant on CARRYCASCIN), which is why inference is off by default; the
    # clock scan runs before that and is what this test is about.
    options = tuple(o for o in build.SYNTH_XILINX_OPTIONS if o != "-nodsp")
    monkeypatch.setattr(build, "SYNTH_XILINX_OPTIONS", options)
    sources = _local(*MUL)
    out = build_root / "arty-openxc7" / "mul-with-dsp"
    try:
        result = build.build_compare(sources.project, sources, "arty", "openxc7", out, 100_000)
    except build.BuildError as e:
        assert "place and route (nextpnr-xilinx) failed" in str(e), e
    else:
        assert result.project_clocks == ()
    assert "DSP48E1" in (out / "utilization-yosys.txt").read_text()
    log = (out / "build.log").read_text()
    assert "tt-harness tied-off clock pin: " in log
    assert "CLK$const" in log
    assert "tt-harness clock: domain project" not in log
    assert "tt-harness clock root:" not in log


# The pre-pack scan on stand-in objects ---------------------------------------------------------


class _Net:
    def __init__(self, name: str) -> None:
        self.name, self.driver = name, None


class _Ref:
    def __init__(self, cell, port: str) -> None:
        self.cell, self.port = cell, port


class _Port:
    def __init__(self, net) -> None:
        self.net = net


class _Cells(dict):
    def __iter__(self):
        return iter(self.items())


class _Cell:
    def __init__(self, name: str, kind: str, outputs: tuple[str, ...] = (), **ports) -> None:
        self.name, self.type, self.params = name, kind, []
        self.ports = _Cells({p: _Port(n) for p, n in ports.items()})
        for port in outputs:
            ports[port].driver = _Ref(self, port)


class _Ctx:
    def __init__(self, cells) -> None:
        self.cells = _Cells({c.name: c for c in cells})
        self.clocks: dict[str, float] = {}

    def addClock(self, net: str, mhz: float) -> None:
        self.clocks[net] = mhz


def _scan(extra_cells) -> tuple[str, _Ctx, dict[str, _Net]]:
    """Run the pre-pack scan on the harness's two clocks plus `extra_cells(nets)`."""
    names = ("clk100", "i", "clk0", "d", "project_clk", "data", "prod", "const", "slow", "q")
    n = {name: _Net(name) for name in names}
    n["const"].name = "dsp.CLK$const"
    cells = [
        _Cell("ibuf", "IBUF", ("O",), I=n["clk100"], O=n["i"]),
        _Cell("sys_bufg", "BUFG", ("O",), I=n["i"], O=n["clk0"]),
        _Cell("pclk_reg", "FDRE", ("Q",), C=n["clk0"], D=n["d"], Q=n["d"]),
        _Cell("pclk_bufg", "BUFG", ("O",), I=n["d"], O=n["project_clk"]),
        _Cell("feeder", "FDRE", ("Q",), C=n["project_clk"], D=n["data"], Q=n["data"]),
        *extra_cells(n),
    ]
    ctx = _Ctx(cells)
    out: list[str] = []
    script = build._PREPACK.format(
        input_port="clk100", sys_mhz=100.0, pclk_bufg="pclk_bufg", pclk_mhz=PLAN.pclk_mhz
    )
    exec(script, {"ctx": ctx, "print": lambda *a: out.append(" ".join(map(str, a)))})  # noqa: S102
    return "\n".join(out) + "\n", ctx, n


def _dsp(n) -> list[_Cell]:
    """An unregistered DSP48E1 (CLK tied to ground) between two pclk flip-flops."""
    return [
        _Cell("gnd", "GND", ("G",), G=n["const"]),
        _Cell("dsp", "DSP48E1", ("P",), CLK=n["const"], A=n["data"], P=n["prod"]),
        _Cell("user", "FDRE", ("Q",), C=n["project_clk"], D=n["prod"], Q=n["q"]),
    ]


def test_prepack_ignores_a_clock_pin_tied_to_a_constant() -> None:
    text, ctx, _ = _scan(_dsp)
    assert "tt-harness tied-off clock pin: dsp.CLK (net 'dsp.CLK$const')\n" in text
    assert "domain project" not in text and "tt-harness clock root" not in text
    assert set(ctx.clocks) == {"clk0", "project_clk"}  # no constraint on the constant
    # The cell is combinational for the walks: pclk's two flip-flops, with a path between
    # them through it.
    assert "domain pclk net 'project_clk'" in text
    assert "(cells 2 rising 2 falling 0 latches 0 intra yes; BUFG pclk_bufg)" in text


def test_prepack_treats_a_supply_tie_the_same_way() -> None:
    def cells(n):
        tied = _dsp(n)
        tied[0] = _Cell("vcc", "VCC", ("P",), P=n["const"])
        return tied

    text, ctx, _ = _scan(cells)
    assert "tt-harness tied-off clock pin: dsp.CLK" in text
    assert set(ctx.clocks) == {"clk0", "project_clk"}


def test_prepack_still_finds_a_clock_made_by_logic_beside_a_tied_off_pin() -> None:
    def cells(n):
        return [
            *_dsp(n),
            _Cell("slow_reg", "FDRE", ("Q",), C=n["project_clk"], D=n["slow"], Q=n["slow"]),
            _Cell("late", "FDRE", C=n["slow"], D=n["prod"], Q=_Net("unused")),
        ]

    text, ctx, _ = _scan(cells)
    assert "tt-harness tied-off clock pin: dsp.CLK" in text
    assert f"tt-harness clock: domain project net 'slow' {PLAN.pclk_mhz!r} MHz" in text
    assert set(ctx.clocks) == {"clk0", "project_clk", "slow"}
    assert "tt-harness clock root: slow\n" in text
    assert "tt-harness clock contributor: slow FDRE pclk rising direct no-async\n" in text
    assert pc.classify(pc.parse_roots(text))["slow"].kind == "register"
    assert "dsp.CLK$const" not in text.replace(
        "tied-off clock pin: dsp.CLK (net 'dsp.CLK$const')", ""
    )


def test_tied_off_line_is_not_read_as_a_clock_by_the_gate() -> None:
    # The parser sees the new line in every log of a design with such a pin.
    line = "tt-harness tied-off clock pin: dsp.CLK (net 'dsp.CLK$const')\n"
    timing = build.parse_nextpnr(line + NEXTPNR_PASS, PLAN, Path("build.log"))
    assert set(timing) == {"sys", "pclk", "sys->pclk", "pclk->sys"}
    assert build.project_clocks(timing) == ()


# Vivado's root discovery has the same rule ---------------------------------------------------

MUL16 = ("mul16", "tt_um_test_mul16")  # one unregistered DSP48E1 in Vivado


def test_vivado_root_discovery_skips_a_clock_pin_on_a_constant_net(tmp_path: Path) -> None:
    tcl = build.vivado_clocks_tcl(PLAN, arty, tmp_path)
    skip = tcl.index('if {$type eq "GROUND" || $type eq "POWER"} {')
    assert "set type [get_property -quiet TYPE [get_nets -quiet -of_objects $pin]]" in tcl
    assert 'puts "tt-harness tied-off clock pin: $pin"' in tcl
    # The pin is passed over before its driver could become a root.
    assert tcl.index("foreach pin [get_pins") < skip < tcl.index("lappend tt_roots $drv")
    assert tcl.index("continue", skip) < tcl.index("set drv [get_pins", skip)


def test_vivado_tied_off_line_is_not_read_as_a_project_clock() -> None:
    line = "tt-harness tied-off clock pin: project/p/CLK\n"
    timing = build.parse_vivado(line + VIVADO_PASS, PLAN, Path("build.log"))
    assert build.project_clocks(timing) == ()


@pytest.mark.vivado
@pytest.mark.skipif(build.tools.vivado_settings() is None, reason="Vivado not installed")
def test_real_build_vivado_unregistered_dsp_is_not_a_project_clock(build_root: Path) -> None:
    # Before Task 9b the GND cell driving the DSP48E1's CLK pin was taken as a clock root
    # (`project/GND/G`), constrained as a primary clock, and the gate then refused the build:
    # "unconstrained: clock domain(s) project:project/GND/G absent from the timing report".
    sources = _local(*MUL16)
    out = build_root / "arty-vivado" / "mul16"
    result = build.build_compare(sources.project, sources, "arty", "vivado", out, 100_000)
    assert result.project_clocks == ()
    assert set(result.timing) == {"sys", "pclk", "sys->pclk", "pclk->sys"}
    assert all(d.met and d.signed_off for d in result.timing.values())
    # Lines the run printed (the log also echoes the script's own text, behind "## ").
    printed = [ln for ln in result.log.read_text().splitlines() if ln.startswith("tt-harness ")]
    assert any(ln.startswith("tt-harness tied-off clock pin: ") for ln in printed)
    assert not any(ln.startswith("tt-harness clock root:") for ln in printed)
    assert "DSP48E1" in (out / "utilization.rpt").read_text()
    assert (out / "clock-roots.txt").read_text() == ""


# Where tests build -----------------------------------------------------------------------------


def test_build_tree_changes_are_seen(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "kept.bit").write_bytes(b"1")
    (tmp_path / "a" / "gone.bit").write_bytes(b"2")
    (tmp_path / "a" / "edited.log").write_text("x")
    before = build_tree.state(tmp_path)
    assert build_tree.changes(before, build_tree.state(tmp_path)) == []
    (tmp_path / "a" / "gone.bit").unlink()
    (tmp_path / "a" / "edited.log").write_text("longer")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "new.bit").write_bytes(b"3")
    assert build_tree.changes(before, build_tree.state(tmp_path)) == [
        "modified a/edited.log",
        "removed a/gone.bit",
        "created b/new.bit",
    ]
    assert build_tree.state(tmp_path / "absent") == {}


def test_real_build_tests_build_under_the_pytest_directory(build_root: Path) -> None:
    assert build_tree.DEFAULT_COMPARE_TREE == build.tools.REPO / "build" / "compare"
    assert build_tree.DEFAULT_COMPARE_TREE not in build_root.parents
    assert build_root != build_tree.DEFAULT_COMPARE_TREE


def test_prescreen_of_a_build_stays_in_its_build_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = []
    screen = prescreen.Prescreen(0, 0, 0, False, Path("prescreen.log"))

    def check(sources, out_dir=None):
        seen.append(out_dir)
        return screen

    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    monkeypatch.setattr(build.prescreen, "check", check)
    s = dataclasses.replace(_counter())
    build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert seen == [tmp_path / "prescreen"]
