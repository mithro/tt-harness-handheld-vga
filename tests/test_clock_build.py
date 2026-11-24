# SPDX-License-Identifier: Apache-2.0
"""Real builds of the project-clock design (`tt_harness.clock_build`), spike B.

Four builds: Arty A7-35T (100 MHz in) and Game Bub (50 MHz in), each with the open flow
(marked `toolchain`) and Vivado (marked `vivado`). Each asserts that a bitstream exists, that
both clock domains (the input clock and the MMCM output) are constrained at the right
frequency and meet timing, and what the bitstream says about the fractional divider, the
BUFGCE enable and the button pull-ups. Logs and reports are left in `build/clock/<board>-<tool>/`.
The findings are written up in docs/results/2026-11-spike-b-project-clock.md.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest

from tt_harness import clock_build as cb
from tt_harness import mmcm_fasm, tools
from tt_harness.core.clocking import explicit_setting

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build" / "clock"
SYNC_WORD = bytes.fromhex("aa995566")
BOARDS = [pytest.param(cb.ARTY, id="arty"), pytest.param(cb.GAMEBUB, id="gamebub")]


def _need_open() -> None:
    if not cb.CHIPDB.is_dir() or tools.find("nextpnr-xilinx") is None:
        pytest.skip(".toolchains/ is absent; run: uv run scripts/setup_toolchains.py")


def _need_vivado() -> None:
    if tools.vivado_settings() is None:
        pytest.skip("Vivado 2025.2 is not installed")


def _bitstream_ok(job: cb.Build) -> None:
    assert job.bitstream is not None and job.bitstream.is_file()
    data = job.bitstream.read_bytes()
    assert len(data) > 1_000_000, f"bitstream is only {len(data)} bytes"
    assert 0 < data.find(SYNC_WORD) < 512, "no sync word near the start of the bitstream"


def _frac_eighths(value: float) -> int:
    return round((value - int(value)) * 8)


@functools.cache
def _open(name: str) -> cb.Build:
    board = cb.BOARDS[name]
    _need_open()
    return cb.openxc7_build(board, BUILD / f"{board.name}-openxc7")


@functools.cache
def _vivado(name: str) -> cb.Build:
    board = cb.BOARDS[name]
    _need_vivado()
    return cb.vivado_build(board, BUILD / f"{board.name}-vivado")


@pytest.fixture(params=BOARDS)
def open_job(request) -> cb.Build:
    return _open(request.param.name)


@pytest.fixture(params=BOARDS)
def vivado_job(request) -> cb.Build:
    return _vivado(request.param.name)


# Settings ----------------------------------------------------------------------------------


def test_settings_are_the_fractional_solution() -> None:
    for board, mult in ((cb.ARTY, 9.0), (cb.GAMEBUB, 18.0)):
        s = cb.generate_setting(board)
        assert (s.divclk, s.mult, s.div) == (1, mult, 35.75)
        assert s.fractional
        assert s.achieved_hz == pytest.approx(25_174_825.17, rel=1e-8)


def _vivado_facts(feedback_ns: float, project_ns: float = 39.722) -> str:
    """The lines `parse_vivado` reads, as a Vivado run of the Arty design would print them."""
    checks = "".join(f"{n}. checking {c} (0)\n" for n, c in enumerate(cb.VIVADO_CHECKS, 1))
    return checks + (
        "tt-harness clock: clk100 period 10.000 generated 0\n"
        f"tt-harness clock: pc_clkfb period {feedback_ns:.3f} generated 1\n"
        f"tt-harness clock: pc_clkout_raw period {project_ns:.3f} generated 1\n"
        "tt-harness slack: clk100 setup 4.747\n"
        "tt-harness slack: clk100 hold 0.074\n"
        "tt-harness slack: pc_clkfb setup none\n"
        "tt-harness slack: pc_clkfb hold none\n"
        "tt-harness slack: pc_clkout_raw setup 18.562\n"
        "tt-harness slack: pc_clkout_raw hold 0.106\n"
    )


def test_vivado_gate_expects_the_feedback_clock_at_the_phase_detector_period(tmp_path) -> None:
    """CLKFBOUT's clock has the input period times DIVCLK_DIVIDE: 20 ns for DIVCLK 2."""
    log = tmp_path / "build.log"
    divclk1 = explicit_setting(100_000_000, 1, 9.0, 35.75)
    divclk2 = explicit_setting(100_000_000, 2, 18.0, 35.75)
    for setting, feedback_ns in ((divclk1, 10.0), (divclk2, 20.0)):
        domains = cb.parse_vivado(_vivado_facts(feedback_ns), cb.ARTY, setting, log)
        assert sorted(domains) == ["input", "project"]
        assert all(d.met for d in domains.values())
    # The other period is a generated clock the design should not have.
    for setting, feedback_ns in ((divclk1, 20.0), (divclk2, 10.0)):
        with pytest.raises(cb.TimingError, match="unexpected generated clock pc_clkfb"):
            cb.parse_vivado(_vivado_facts(feedback_ns), cb.ARTY, setting, log)
    # A feedback clock that has timing paths is not the feedback pin alone.
    with_paths = _vivado_facts(20.0).replace("pc_clkfb setup none", "pc_clkfb setup 1.000")
    with pytest.raises(cb.TimingError, match="unexpected generated clock"):
        cb.parse_vivado(with_paths, cb.ARTY, divclk2, log)


# Open flow ---------------------------------------------------------------------------------


@pytest.mark.toolchain
def test_open_flow_build(open_job: cb.Build) -> None:
    job = open_job
    _bitstream_ok(job)
    log = job.facts["nextpnr"]
    assert job.facts["feedback"].endswith("CLKFBOUT2IN")
    domains = cb.parse_nextpnr(log, job.board, job.setting, job.log)
    assert domains["input"].constraint_mhz == pytest.approx(job.board.clk_hz / 1e6)
    projects = {k: d for k, d in domains.items() if k.startswith("project:")}
    # The free-running BUFG and the gated BUFGCE both carry the MMCM output.
    assert sorted(projects) == ["project:BUFG", "project:BUFGCE"]
    for key, d in domains.items():
        # nextpnr prints its constraint to two decimals.
        want = job.board.clk_hz / 1e6 if key == "input" else job.setting.achieved_hz / 1e6
        assert d.constraint_mhz == pytest.approx(want, abs=0.005), key
        assert d.met, f"{key} misses timing: {d}"


@pytest.mark.toolchain
def test_open_flow_fractional_divider_is_in_the_fasm(open_job: cb.Build) -> None:
    job = open_job
    fasm = (job.out_dir / "top.fasm").read_text()
    feats = cb.mmcm_values(fasm)
    assert "CLKOUT0_CLKOUT2_FRAC_EN" in feats, "CLKOUT0 fractional enable is not set"
    frac = _frac_eighths(job.setting.div)
    assert feats["CLKOUT0_CLKOUT2_FRAC"] == frac
    # CLKFBOUT_MULT_F is whole in this setting: the feedback divider has no fraction.
    assert _frac_eighths(job.setting.mult) == 0
    assert not any(k.startswith("CLKFBOUT") and "FRAC" in k for k in feats)


@pytest.mark.toolchain
def test_open_flow_bufgce_enable_is_routed(open_job: cb.Build) -> None:
    job = open_job
    log = job.facts["nextpnr"]
    m = re.search(r"Constrained BUFGCTRL 'BUFGCE' to bel 'BUFGCTRL_X0Y(\d+)/BUFGCTRL'", log)
    assert m, "the BUFGCE was not placed on a BUFGCTRL"
    n = int(m.group(1)) % 16  # the index within its clock-buffer tile (top half: 16 to 31)
    fasm = (job.out_dir / "top.fasm").read_text()
    assert re.search(rf"BUFGCTRL{n}_CE0\.CLK_BUFG_IMUX\d+_\d\n", fasm), "no fabric route to CE0"
    assert f"BUFGCTRL.BUFGCTRL_X0Y{n}.ZINV_CE0\n" in fasm, "CE0 is inverted or the bit is absent"


@pytest.mark.toolchain
def test_open_flow_gamebub_button_pullups(open_job: cb.Build) -> None:
    if open_job.board is not cb.GAMEBUB:
        pytest.skip("the Arty buttons have external pull-downs; no pull-up property is set")
    fasm = (open_job.out_dir / "top.fasm").read_text()
    pulled = re.findall(r"^(\S+)\.PULLTYPE\.PULLUP$", fasm, re.MULTILINE)
    assert len(pulled) == 12, f"{len(pulled)} pull-ups for 12 buttons"


# Vivado ------------------------------------------------------------------------------------


@pytest.mark.vivado
def test_vivado_build(vivado_job: cb.Build) -> None:
    job = vivado_job
    _bitstream_ok(job)
    assert job.facts["feedback"].endswith("CLKFBOUT2IN")
    domains = cb.parse_vivado(job.facts["vivado"], job.board, job.setting, job.log)
    assert sorted(domains) == ["input", "project"]
    assert all(d.met for d in domains.values()), domains
    # The MMCM output clock is derived from the input clock constraint, at the achieved period.
    assert domains["project"].constraint_mhz == pytest.approx(
        job.setting.achieved_hz / 1e6, rel=1e-4
    )
    text = job.facts["vivado"]
    # The source says ZHOLD (for nextpnr-xilinx). Vivado 2025.2 sees CLKFBOUT wired straight
    # to CLKFBIN and changes the implemented cell's property to INTERNAL, without a message.
    assert 'COMPENSATION       ("ZHOLD")' in (job.out_dir / "top.v").read_text()
    for prop, want in (
        ("COMPENSATION", "INTERNAL"),
        ("DIVCLK_DIVIDE", str(job.setting.divclk)),
        ("CLKFBOUT_MULT_F", f"{job.setting.mult:.3f}"),
        ("CLKOUT0_DIVIDE_F", f"{job.setting.div:.3f}"),
    ):
        assert f"tt-harness mmcm: {prop} {want}\n" in text, prop


@pytest.mark.vivado
def test_vivado_times_the_bufgce_enable_path(vivado_job: cb.Build) -> None:
    text = vivado_job.facts["vivado"]
    m = re.search(
        r"^tt-harness ce path: slack (-?[\d.]+) from (\S+) to (\S+) clock (\S+)", text, re.MULTILINE
    )
    assert m, "Vivado found no timing path to the BUFGCE enable pin"
    assert float(m[1]) >= 0
    assert m[3].endswith("/CE0")
    assert "xilinxmultiregimpl" in m[2], "the enable path does not start at the synchroniser"


@pytest.mark.vivado
def test_vivado_gamebub_button_pullups(vivado_job: cb.Build) -> None:
    ports = re.findall(
        r"^tt-harness port: (\S+) .* PULLTYPE (\S*)$", vivado_job.facts["vivado"], re.MULTILINE
    )
    pulled = [p for p, t in ports if t == "PULLUP"]
    if vivado_job.board is cb.GAMEBUB:
        assert sorted(pulled) == sorted(p for p, _ in ports if p.startswith("buttons_"))
        assert len(pulled) == 12
    else:
        assert pulled == []


# Open flow against Vivado -------------------------------------------------------------------

# Every MMCM feature that differs between the two flows' bitstreams for the same design,
# decoded back with bit2fasm and read as integers: field -> (openXC7, Vivado). The comparison
# covers the whole MMCM site, not only the dividers, and (since Task 7d) the routing features
# of the MMCM's tile, which must be the same in both flows. The causes (nextpnr-xilinx 0.8.2
# `xilinx/fasm.cc`, `write_mmcm_clkout` and `write_mmcm`):
#
# - The fractional CLKOUT0_DIVIDE_F = 35.75 has an odd integer part. Vivado follows XAPP888's
#   `mmcm_frac_count_calc` (HIGH 17, WF_R 0, WF_F 0, PHASE_MUX_F 7); nextpnr-xilinx has no odd
#   branch and writes HIGH 16, WF_R 1, WF_F 1, PHASE_MUX_F 3. The fraction itself (FRAC,
#   FRAC_EN) agrees.
# - The unused outputs CLKOUT1 to CLKOUT6: Vivado sets NO_COUNT and HIGH = LOW = 1.
# - The loop filter: 0.8.2 writes 0x3d4 to TABLE and the per-multiplier filter value to
#   FILTREG1_RESERVED (swapped; fixed upstream, where TABLE gets the value and FILTREG1 is 8),
#   so TABLE is right only for MULT 10, 17 and 18.
# - POWER_REG: 0xffff against Vivado's 0x9900.
#
# Which divider encoding is right on silicon needs a hardware measurement; see
# docs/results/2026-11-spike-b-project-clock.md. The test asserts the documented set exactly,
# so it fails if either toolchain changes.
FILTER = {"arty": (0x394, 0x8, 0x3D4, 0x394), "gamebub": (0x3D4, 0x8, None, None)}


def known_differences(board: cb.Board) -> dict[str, tuple[int, int]]:
    diff = {
        "CLKOUT0_CLKOUT1_HIGH_TIME": (16, 17),
        "CLKOUT0_CLKOUT2_FRAC_WF_R": (1, 0),
        "CLKOUT5_CLKOUT2_FRACTIONAL_FRAC_WF_F": (1, 0),
        "CLKOUT5_CLKOUT2_FRACTIONAL_PHASE_MUX_F": (3, 7),
        "POWER_REG_POWER_REG_POWER_REG": (0xFFFF, 0x9900),
        "CLKOUT5_CLKOUT2_FRACTIONAL_NO_COUNT": (0, 1),
        "CLKOUT6_CLKOUT2_FRACTIONAL_NO_COUNT": (0, 1),
    }
    for n in (1, 2, 3, 4):
        diff[f"CLKOUT{n}_CLKOUT2_NO_COUNT"] = (0, 1)
    for n in (1, 2, 3, 4, 5, 6):
        diff[f"CLKOUT{n}_CLKOUT1_HIGH_TIME"] = (0, 1)
        diff[f"CLKOUT{n}_CLKOUT1_LOW_TIME"] = (0, 1)
    filt_open, filt_viv, table_open, table_viv = FILTER[board.name]
    diff["FILTREG1_RESERVED"] = (filt_open, filt_viv)
    if table_open is not None:
        diff["TABLE"] = (table_open, table_viv)
    return diff


@pytest.mark.toolchain
@pytest.mark.vivado
@pytest.mark.parametrize("board", BOARDS)
def test_open_and_vivado_mmcm_encodings_differ_as_documented(board: cb.Board) -> None:
    jobs = {"open": _open(board.name), "vivado": _vivado(board.name)}
    decoded, routing = {}, {}
    for tag, job in jobs.items():
        # Each build has decoded its own bitstream for the feedback gate.
        text = cb.decoded_path(job).read_text()
        decoded[tag] = cb.mmcm_values(text)
        (tile,) = mmcm_fasm.mmcm_tiles(text)
        routing[tag] = mmcm_fasm.tile_routing(text, tile)
    # The routing features of the MMCM's tile, which `mmcm_values` does not see (it keeps
    # the site's `.MMCME2_ADV.` features only): the pips into CLKIN1, CLKIN2 and CLKFBIN.
    # Comparing only the site features is how a tied-off feedback went unnoticed in Task 7b.
    assert routing["open"] == routing["vivado"], routing
    feedback = {f for f in routing["open"] if f.split(".")[0].endswith("MMCM_CLKFBIN")}
    assert len(feedback) == 1 and feedback.pop().endswith("CLKFBOUT2IN")
    keys = sorted(set(decoded["open"]) | set(decoded["vivado"]))
    diff = {
        k: (decoded["open"].get(k, 0), decoded["vivado"].get(k, 0))
        for k in keys
        if decoded["open"].get(k, 0) != decoded["vivado"].get(k, 0)
    }
    assert diff == known_differences(board)
    # The fraction and its enable agree.
    assert decoded["open"]["CLKOUT0_CLKOUT2_FRAC"] == decoded["vivado"]["CLKOUT0_CLKOUT2_FRAC"] == 6
    assert (
        decoded["open"]["CLKOUT0_CLKOUT2_FRAC_EN"] == decoded["vivado"]["CLKOUT0_CLKOUT2_FRAC_EN"]
    )
