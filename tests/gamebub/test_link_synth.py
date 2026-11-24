# SPDX-License-Identifier: Apache-2.0
"""Synthesis check of the MCU link (``link_top``) on the Game Bub pins, with both toolchains.

The ``spi`` clock is constrained at 40 MHz (the firmware's write clock) and ``sys`` at 50 MHz;
a flow passes only when it writes a bitstream, both clocks are in its timing report, and both
are met. What each flow cannot check is asserted too, so a change in the tools' behaviour
shows up here: see docs/results/2026-11-spike-c-gamebub-link-synthesis.md.

    uv run pytest tests/gamebub/test_link_synth.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tt_harness import tools
from tt_harness.boards.gamebub import link_build, link_top
from tt_harness.boards.gamebub.platform import Platform

REPO = Path(__file__).resolve().parent.parent.parent
OUT = REPO / "build" / "gamebub-link"
SYNC_WORD = bytes.fromhex("aa995566")


def _assert_bitstream(path: Path) -> None:
    bit = path.read_bytes()
    sync = bit.find(SYNC_WORD)
    assert 0 < sync < 512, "no configuration sync word near the start of the .bit file"
    assert b"7a100t" in bit[:sync], "part name missing from the .bit header"
    assert abs(len(bit) - 30_606_304 // 8) < 1024


def test_top_has_the_board_ports() -> None:
    platform = Platform("vivado")
    top_v, _ = link_top.top_verilog(platform, OUT / "ports" / "link_top.v")
    text = top_v.read_text()
    for port in ("clk50", "mcu_qspi_clk", "mcu_qspi_cs_n", "mcu_irq", "pmod", "tp"):
        assert re.search(rf"\b{port}\b", text), port
    assert re.search(r"inout\s+wire\s+\[3:0\]\s+mcu_qspi_dq", text)
    assert len(re.findall(r"^\s*input\s+wire\s+buttons_", text, re.MULTILINE)) == 12
    xdc = link_top.pin_xdc(platform)
    assert "set_property LOC T14 [get_ports {mcu_qspi_clk}]" in xdc
    assert "set_property LOC N15 [get_ports {mcu_irq}]" in xdc
    assert xdc.count("PULLTYPE PULLUP") == 12


@pytest.mark.toolchain
def test_openxc7_builds_with_spi_constrained_at_40mhz() -> None:
    if not (tools.TOOLCHAINS / "openxc7").is_dir():
        pytest.skip(".toolchains/ is absent; run: uv run scripts/setup_toolchains.py")
    required = [
        tools.find("yosys"),
        tools.find("nextpnr-xilinx"),
        link_build.CHIPDB / "xc7a100tcsg324-1.bin",
    ]
    if any(p is None or not Path(p).exists() for p in required):
        pytest.skip("openXC7 toolchain or the xc7a100tcsg324-1 chip database is missing")
    result = link_build.build_openxc7(OUT / "openxc7")
    _assert_bitstream(result.bitstream)
    assert set(result.clocks) == {"sys", "spi"}
    spi_mhz, spi_slack, spi_hold = result.clocks["spi"]
    sys_mhz, sys_slack, _ = result.clocks["sys"]
    assert (spi_mhz, sys_mhz) == (40.0, 50.0)
    assert spi_slack >= 0 and sys_slack >= 0
    # Documented limitation: nextpnr-xilinx 0.8.2 does no hold analysis.
    assert spi_hold is None


@pytest.mark.vivado
def test_vivado_builds_with_spi_constrained_at_40mhz() -> None:
    if tools.vivado_settings() is None:
        pytest.skip(f"Vivado is not installed at {tools.VIVADO_SETTINGS}")
    result = link_build.build_vivado(OUT / "vivado")
    _assert_bitstream(result.bitstream)
    assert set(result.clocks) == {"sys", "spi", "spi_rd"}
    for name, mhz in (("sys", 50.0), ("spi", 40.0), ("spi_rd", 16.0)):
        constraint, setup, hold = result.clocks[name]
        assert constraint == pytest.approx(mhz)
        assert setup >= 0 and hold >= 0, f"{name}: setup {setup}, hold {hold}"
