# SPDX-License-Identifier: Apache-2.0
"""The Arty clock probe (`tt_harness.boards.arty_clock_probe`), plan 1 Task 7c.

Without tools: the settings and their exact ideal counts, the UART line (format, checksum,
what the gateware really sends), the lock-loss counter, the whole core in the simulator with
two measured clocks, and the generated Verilog's MMCM parameters. With tools (marked
`toolchain` / `vivado`): one real build per toolchain of setting B, the one whose DIVCLK is
not 1, through the timing gate, and what its bitstream programs. The eight builds of the
measurement itself are made by `scripts/clock_probe_run.py`.
"""

from __future__ import annotations

import importlib.util
import re
from fractions import Fraction
from pathlib import Path

import board_fakes
import pytest
from litex_boards.platforms import digilent_arty
from migen import Module, run_simulation

from tt_harness import clock_build as cb
from tt_harness import tools
from tt_harness.boards import arty_clock_probe as probe
from tt_harness.boards import arty_clock_probe_build as probe_build
from tt_harness.boards.arty_clock_probe import Reading
from tt_harness.host import run

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build" / "clock-probe"
SYS = 20  # the system clock's period in simulator units
DIVIDER = 2  # system clocks per UART bit in the simulations


# Settings --------------------------------------------------------------------------------------


def test_settings_and_their_exact_ideal_counts() -> None:
    want = {
        "A": (25_174_825, Fraction(3_600_000_000, 143)),
        "B": (25_174_825, Fraction(3_600_000_000, 143)),
        "C": (24_489_796, Fraction(1_200_000_000, 49)),
        "D": (25_000_000, Fraction(25_000_000)),
    }
    for name, (hz, ideal) in want.items():
        s = probe.setting(name)
        assert s.clock_hz == hz
        assert s.vco_hz == 900e6
        assert probe.ideal_count(s) == ideal
        assert round(ideal) == hz
    # A is the solver's own choice for the Arty; B has the Game Bub's MULT and DIVIDE.
    a, solved = probe.setting("A"), cb.generate_setting(cb.ARTY)
    assert (a.divclk, a.mult, a.div) == (solved.divclk, solved.mult, solved.div)
    b, gamebub = probe.setting("B"), cb.generate_setting(cb.GAMEBUB)
    assert (b.mult, b.div, b.divclk) == (gamebub.mult, gamebub.div, 2)
    assert [probe.duty_phases(probe.setting(n)) for n in "ABCD"] == [143, 143, 49, 4]


# The line --------------------------------------------------------------------------------------

EXAMPLE = Reading(free=25_174_826, gated=25_174_826, high=50_000_000, locked=1, losses=0, gate=3)


def test_line_format_and_checksum() -> None:
    line = probe.format_line(EXAMPLE)
    assert line == b"F 0180232A G 0180232A H 02FAF080 L 1 U 00 N 03 *57\n"
    assert len(line) == probe.LINE_BYTES
    assert probe.parse_line(line) == EXAMPLE
    assert probe.parse_line(line.rstrip(b"\n") + b"\r\n") == EXAMPLE


def test_a_damaged_line_is_rejected_not_misread() -> None:
    line = probe.format_line(EXAMPLE)
    for i in range(len(line) - 1):
        for flip in (0x01, 0x04, 0x10):
            damaged = line[:i] + bytes([line[i] ^ flip]) + line[i + 1 :]
            assert probe.parse_line(damaged) is None, damaged
    assert probe.parse_line(line[:-10]) is None
    assert probe.parse_line(b"") is None
    good, bad = probe.parse_capture(b"\x00\xff" + line[5:] + line + b"garbage\n" + line[:20])
    assert good == [EXAMPLE]
    assert len(bad) == 3


# Gateware in the simulator ---------------------------------------------------------------------


def uart_bytes(dut, tx, clocks: int, generators=(), sim_clocks=None) -> bytes:
    """Run `dut` for `clocks` system clocks and decode what `tx` sent (8N1, `DIVIDER`)."""
    levels: list[int] = []

    def watch():
        for _ in range(clocks):
            levels.append((yield tx))
            yield

    run_simulation(dut, [watch(), *generators], clocks=sim_clocks or {"sys": SYS})
    out = bytearray()
    i = 1
    while i < len(levels) - 10 * DIVIDER:
        if levels[i - 1] == 1 and levels[i] == 0:  # a start bit
            bits = [levels[i + DIVIDER * (1 + b) + DIVIDER // 2] for b in range(9)]
            assert bits[8] == 1, "no stop bit"
            out.append(sum(bit << n for n, bit in enumerate(bits[:8])))
            i += 10 * DIVIDER
        else:
            i += 1
    return bytes(out)


def test_line_sender_sends_the_line_the_parser_reads() -> None:
    dut = probe.LineSender(DIVIDER)
    readings = [
        Reading(0x0180232A, 0x0180232B, 0x02FAF080, 1, 0, 0),
        Reading(0xFFFFFFFF, 0, 0x89ABCDEF, 0, 0xFF, 1),
        Reading(0x01234567, 0x76543210, 0, 1, 0x1E, 2),
    ]

    def drive():
        for r in readings:
            yield dut.free.eq(r.free)
            yield dut.gated.eq(r.gated)
            yield dut.high.eq(r.high)
            yield dut.locked.eq(r.locked)
            yield dut.losses.eq(r.losses)
            yield dut.strobe.eq(1)
            yield
            yield dut.strobe.eq(0)
            # The inputs may change at once: the line is of the values at the strobe.
            yield dut.free.eq(0x55555555)
            for _ in range(probe.LINE_BYTES * 10 * DIVIDER + 40):
                yield

    sent = uart_bytes(dut, dut.tx, 3 * (probe.LINE_BYTES * 10 * DIVIDER + 60), [drive()])
    assert sent == b"".join(probe.format_line(r) for r in readings)


def test_lock_monitor_counts_falling_edges_and_saturates() -> None:
    dut = probe.LockMonitor()
    seen = {}

    def drive():
        # Low at configuration, then lock: the first rise is not a loss.
        for _ in range(5):
            yield
        yield dut.locked.eq(1)
        for _ in range(6):
            yield
        seen["after lock"] = ((yield dut.locked_sys), (yield dut.losses))
        for _ in range(3):
            yield dut.locked.eq(0)
            for _ in range(5):
                yield
            yield dut.locked.eq(1)
            for _ in range(5):
                yield
        seen["after three"] = ((yield dut.locked_sys), (yield dut.losses))
        for _ in range(260):
            yield dut.locked.eq(0)
            for _ in range(4):
                yield
            yield dut.locked.eq(1)
            for _ in range(4):
                yield
        yield dut.locked.eq(0)
        for _ in range(6):
            yield
        seen["saturated"] = ((yield dut.locked_sys), (yield dut.losses))

    run_simulation(dut, drive(), clocks={"sys": SYS})
    assert seen == {"after lock": (1, 0), "after three": (1, 3), "saturated": (0, 255)}


def test_core_reports_both_clocks_the_level_and_the_lock() -> None:
    gate = 1100  # longer than a line: 51 bytes x 10 bits x DIVIDER = 1020 system clocks
    dut = probe.ProbeCore(gate, DIVIDER, "free", "gated")

    def drive():
        yield dut.locked.eq(1)
        yield dut.level.eq(1)
        for _ in range(2 * gate + 10):
            yield
        yield dut.locked.eq(0)  # one loss, during the third gate
        for _ in range(10):
            yield
        yield dut.locked.eq(1)
        yield dut.level.eq(0)

    # The free-running clock at 10/7 of the system clock, the gated one stopped.
    clocks = {"sys": SYS, "free": 14, "gated": 10**9}
    sent = uart_bytes(dut, dut.tx, 5 * gate, [drive()], clocks)
    good, bad = probe.parse_capture(sent)
    assert bad == []
    assert [r.gate for r in good] == [0, 1, 2, 3]
    ideal = Fraction(gate * SYS, 14)
    assert all(abs(r.free - ideal) <= 1 for r in good[1:]), good
    assert all(r.gated == 0 for r in good)
    assert [r.locked for r in good] == [1, 1, 1, 1]
    assert [r.losses for r in good] == [0, 0, 1, 1]
    assert good[1].high == gate and good[3].high == 0


# Generated Verilog -----------------------------------------------------------------------------


def _verilog(name: str) -> str:
    platform = digilent_arty.Platform(variant="a7-35", toolchain="vivado")
    top = probe.ClockProbe(platform, probe.setting(name))
    return str(platform.get_verilog(top, name="top"))


@pytest.mark.parametrize("name", sorted(probe.SETTINGS))
def test_generated_verilog_has_the_mmcm_parameters_asked_for(name: str) -> None:
    divclk, mult, div = probe.SETTINGS[name]
    v = _verilog(name)
    block = re.search(r"^MMCME2_ADV\s*#\s*\((.*?)\)\s*\w+\s*\(", v, re.MULTILINE | re.DOTALL)
    assert block, "no MMCME2_ADV instance"
    params = dict(re.findall(r"\.(\w+)\s*\(\s*([^()]*?)\s*\)", block.group(1)))
    assert int(re.sub(r"^\d+'d", "", params["DIVCLK_DIVIDE"])) == divclk
    assert float(params["CLKFBOUT_MULT_F"]) == mult
    assert float(params["CLKOUT0_DIVIDE_F"]) == div
    assert float(params["CLKIN1_PERIOD"]) == 10.0
    assert params["COMPENSATION"] == '"INTERNAL"'
    # One MMCM, the three clock buffers, and the falling-edge flop of the waveform replica.
    assert len(re.findall(r"^MMCME2_ADV\b", v, re.MULTILINE)) == 1
    assert len(re.findall(r"^BUFG\b", v, re.MULTILINE)) == 2
    assert len(re.findall(r"^BUFGCE\b", v, re.MULTILINE)) == 1
    fdre = re.search(r"^FDRE\s*#\s*\((.*?)\)\s*replica_fall", v, re.MULTILINE | re.DOTALL)
    assert fdre and re.search(r"\.IS_C_INVERTED\s*\(\s*(1'd)?1\s*\)", fdre.group(1))


def test_probe_is_a_module_the_clock_build_can_drive() -> None:
    platform = digilent_arty.Platform(variant="a7-35", toolchain="openxc7")
    top = probe.ClockProbe(platform, probe.setting("B"))
    assert isinstance(top, Module)
    assert top.pc.setting == probe.setting("B")


# Real builds -----------------------------------------------------------------------------------


def _programmed(job: cb.Build) -> dict[str, int]:
    assert job.bitstream is not None and job.bitstream.is_file()
    return probe_build.programmed_mmcm(job.bitstream)


@pytest.mark.toolchain
def test_clock_probe_open_flow_build() -> None:
    if not cb.CHIPDB.is_dir() or tools.find("nextpnr-xilinx") is None:
        pytest.skip(".toolchains/ is absent; run: uv run scripts/setup_toolchains.py")
    job = probe_build.build("B", "openxc7", BUILD / "test-B-openxc7")
    domains = job.facts["domains"]
    assert sorted(domains) == ["input", "project:BUFG", "project:BUFGCE"]
    assert all(d.met for d in domains.values()), domains
    assert domains["input"].constraint_mhz == pytest.approx(100.0)
    assert domains["project:BUFG"].constraint_mhz == pytest.approx(25.17, abs=0.005)
    mmcm = _programmed(job)
    # DIVCLK 2 is one high and one low; MULT 18 is nine and nine; the right filter for 18.
    assert (mmcm["DIVCLK_DIVCLK_HIGH_TIME"], mmcm["DIVCLK_DIVCLK_LOW_TIME"]) == (1, 1)
    assert (mmcm["CLKFBOUT_CLKOUT1_HIGH_TIME"], mmcm["CLKFBOUT_CLKOUT1_LOW_TIME"]) == (9, 9)
    assert mmcm["TABLE"] == 0x3D4
    assert (mmcm["CLKOUT0_CLKOUT2_FRAC_EN"], mmcm["CLKOUT0_CLKOUT2_FRAC"]) == (1, 6)
    # The falling-edge flop of the replica has its clock inverted in the bitstream.
    fasm = (job.out_dir / "top.fasm").read_text()
    assert re.search(r"\.CLKINV$", fasm, re.MULTILINE), "no slice with an inverted clock"


@pytest.mark.vivado
def test_clock_probe_vivado_build() -> None:
    if tools.vivado_settings() is None:
        pytest.skip("Vivado 2025.2 is not installed")
    job = probe_build.build("B", "vivado", BUILD / "test-B-vivado")
    domains = job.facts["domains"]
    assert sorted(domains) == ["input", "project"]
    assert all(d.met for d in domains.values()), domains
    assert domains["project"].constraint_mhz == pytest.approx(25.174825, rel=1e-4)
    text = job.facts["vivado"]
    assert "tt-harness mmcm: DIVCLK_DIVIDE 2\n" in text
    assert "tt-harness mmcm: CLKFBOUT_MULT_F 18.000\n" in text
    assert "tt-harness mmcm: CLKOUT0_DIVIDE_F 35.750\n" in text
    mmcm = _programmed(job)
    assert (mmcm["DIVCLK_DIVCLK_HIGH_TIME"], mmcm["DIVCLK_DIVCLK_LOW_TIME"]) == (1, 1)
    assert mmcm["TABLE"] == 0x3D4
    assert mmcm["CLKOUT0_CLKOUT1_HIGH_TIME"] == 17


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
    assert (summary["free_min"], summary["gated_min"]) == (5, 0)
    assert script.summarise(b"", 10)["complete"] is False
