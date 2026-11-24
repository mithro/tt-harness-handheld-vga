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

import re
from pathlib import Path

import pytest

from tt_harness import clock_build as cb
from tt_harness import tools

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


@pytest.fixture(scope="module", params=BOARDS)
def open_job(request) -> cb.Build:
    _need_open()
    return cb.openxc7_build(request.param, BUILD / f"{request.param.name}-openxc7")


@pytest.fixture(scope="module", params=BOARDS)
def vivado_job(request) -> cb.Build:
    _need_vivado()
    return cb.vivado_build(request.param, BUILD / f"{request.param.name}-vivado")


# Settings ----------------------------------------------------------------------------------


def test_settings_are_the_fractional_solution() -> None:
    for board, mult in ((cb.ARTY, 9.0), (cb.GAMEBUB, 18.0)):
        s = cb.generate_setting(board)
        assert (s.divclk, s.mult, s.div) == (1, mult, 35.75)
        assert s.fractional
        assert s.achieved_hz == pytest.approx(25_174_825.17, rel=1e-8)


# Open flow ---------------------------------------------------------------------------------


@pytest.mark.toolchain
def test_open_flow_build(open_job: cb.Build) -> None:
    job = open_job
    _bitstream_ok(job)
    log = job.facts["nextpnr"]
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
    feats = cb.mmcm_features(fasm)
    assert "CLKOUT0_CLKOUT2_FRAC_EN" in feats, "CLKOUT0 fractional enable is not set"
    frac = _frac_eighths(job.setting.div)
    assert feats["CLKOUT0_CLKOUT2_FRAC[2:0]"] == format(frac, "03b")
    # CLKFBOUT_MULT_F is whole in this setting: the feedback divider has no fraction.
    assert _frac_eighths(job.setting.mult) == 0
    assert not any(k.startswith("CLKFBOUT") and "FRAC" in k for k in feats)


@pytest.mark.toolchain
def test_open_flow_bufgce_enable_is_routed(open_job: cb.Build) -> None:
    job = open_job
    log = job.facts["nextpnr"]
    m = re.search(r"Constrained BUFGCTRL 'BUFGCE' to bel 'BUFGCTRL_X0Y(\d+)/BUFGCTRL'", log)
    assert m, "the BUFGCE was not placed on a BUFGCTRL"
    n = m.group(1)
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
    domains = cb.parse_vivado(job.facts["vivado"], job.board, job.setting, job.log)
    assert sorted(domains) == ["input", "project"]
    assert all(d.met for d in domains.values()), domains
    # The MMCM output clock is derived from the input clock constraint, at the achieved period.
    assert domains["project"].constraint_mhz == pytest.approx(
        job.setting.achieved_hz / 1e6, rel=1e-4
    )
    text = job.facts["vivado"]
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
