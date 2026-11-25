# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: B023  (generators defined in loops run within the iteration that made them)
"""`AtSpeedCheck` tests.

Part 1 (Migen simulator, two unrelated clocks, a toy design whose output is a counter that
a low `rst_n` clears): the pipeline alignment at the reset boundary, the result for small
N, restart, `start` at many phases of the project clock, and a stopped project clock.
Part 2 (marked `toolchain`, Verilator, two REAL pool designs, `sys` 50 MHz and the project
clock at the design's real frequency, both integer-picosecond periods not related to each
other): the CRC against the CRC of the real reference dump. See `test_slot.py` for what a
simulation can and cannot show about clock-domain crossings.
"""

from __future__ import annotations

import zlib

import pytest
from migen import If, Module, Signal
from slot_sim import gate, run

from tt_harness.core.atspeed import AtSpeedCheck
from tt_harness.core.slot import ProjectSlot
from tt_harness.core.stimulus import RESET_CLOCKS


class _Toy(Module):
    """`uo_out` = 0 while `rst_n` is low at a clock edge, else it counts up from there."""

    def __init__(self, slot: ProjectSlot) -> None:
        self.sync.project += If(~slot.pin_rst_n, slot.uo_out.eq(0)).Else(
            slot.uo_out.eq(slot.uo_out + 1)
        )


def toy_bytes(n: int) -> bytes:
    """Byte k = uo_out after clock k, clock 0 the first with rst_n low (10 clocks of reset)."""
    return bytes(0 if k < RESET_CLOCKS else (k - RESET_CLOCKS + 1) & 0xFF for k in range(n))


class _Bench(Module):
    def __init__(self, clock_hz: int = 25_175_000, sim_clock: bool = True) -> None:
        self.submodules.slot = slot = ProjectSlot(
            None, Signal(), 100_000_000, clock_hz, sim_clock=sim_clock
        )
        self.submodules.check = check = AtSpeedCheck(min_project_hz=1_000_000)
        slot.attach(check)
        if sim_clock:
            self.submodules.toy = _Toy(slot)


def bench(running: bool = True, **kw) -> _Sim:
    return gate(_Bench(**kw), running)


def waits(n: int):
    for _ in range(n):
        yield


def do_check(b: _Bench, n: int, out: dict, key: str, timeout: int = 6000):
    """Generator fragment: write N, pulse start, wait for done, record the crc."""
    yield b.check.clocks.eq(n)
    yield
    yield b.check.start.eq(1)
    yield
    yield b.check.start.eq(0)
    for _ in range(timeout):
        yield
        if (yield b.check.done):
            out[key] = yield b.check.crc
            return
    out[key] = None


@pytest.mark.parametrize("n", [0, 1, 2, 9, 10, 11, 12, 13, 14, 100, 300])
def test_crc_of_a_toy_design_matches_the_definition(n: int) -> None:
    b = bench()
    out: dict = {}

    def sysgen():
        yield b.slot.rst_n.eq(1)
        yield from waits(30)  # the design is running, counting
        yield from do_check(b, n, out, "crc")

    run(b, [sysgen()])
    assert out["crc"] == zlib.crc32(toy_bytes(n))


def test_start_at_a_few_phases_gives_the_same_crc() -> None:
    """Fast subset of `test_restart_start_phases_and_busy_flags`: three project clock phases
    against the system clock, one start delay each, one check each."""
    expected = zlib.crc32(toy_bytes(40))
    for offset, delay in ((50, 0), (14_050, 1), (29_050, 3)):
        b = bench()
        out: dict = {}

        def sysgen():
            yield b.slot.rst_n.eq(1)
            yield from waits(30 + delay)
            yield from do_check(b, 40, out, "crc")

        run(b, [sysgen()], project=(39_700, offset))
        assert out["crc"] == expected, (offset, delay, out)


@pytest.mark.slow
def test_restart_start_phases_and_busy_flags() -> None:
    expected = zlib.crc32(toy_bytes(64))
    seen = set()
    for offset in (50, 4_050, 9_050, 14_050, 19_050, 24_050, 29_050, 34_050):
        for delay in (0, 1, 2, 3):
            b = bench()
            out: dict = {}
            flags: list[tuple[int, int]] = []

            def sysgen():
                yield b.slot.rst_n.eq(1)
                yield from waits(30 + delay)
                yield from do_check(b, 64, out, "first")
                flags.append(((yield b.check.busy), (yield b.check.done)))
                yield from do_check(b, 64, out, "again")  # restart, nothing reconfigured
                yield b.check.clocks.eq(10)
                yield from do_check(b, 10, out, "other")

            def tickgen():
                while True:
                    seen.add(((yield b.check.busy), (yield b.check.done)))
                    yield

            run(b, [sysgen()], tick_gens=[_passive(tickgen())], project=(39_700, offset))
            assert out["first"] == out["again"] == expected, (offset, delay, out)
            assert out["other"] == zlib.crc32(toy_bytes(10)) != expected
            assert flags == [(0, 1)]
    # busy and done are never both set, and a run goes busy -> done.
    assert (1, 1) not in seen and (1, 0) in seen and (0, 1) in seen


def _passive(gen):
    yield "passive"
    yield from gen


@pytest.mark.parametrize("how", ["clock_hz_zero", "clock_stopped_then_started"])
def test_stopped_clock_never_blocks_the_system_side_at_speed(how: str) -> None:
    """A started check stays busy with no project clock, the system side keeps going, a
    second `start` is ignored, and once the clock runs it completes and can be run again."""
    if how == "clock_hz_zero":
        b = gate(_Bench(clock_hz=0, sim_clock=False))
    else:
        b = bench(running=False)
    out: dict = {}
    status: dict = {}

    def sysgen():
        yield b.slot.rst_n.eq(1)
        yield b.check.clocks.eq(40)
        yield
        yield b.check.start.eq(1)
        yield
        yield b.check.start.eq(0)
        yield from waits(500)  # the writer never waited for anything
        status["busy"] = yield b.check.busy
        status["done"] = yield b.check.done
        yield b.check.start.eq(1)  # ignored while busy
        yield
        yield b.check.start.eq(0)
        yield from waits(100)
        status["busy2"] = yield b.check.busy
        if how == "clock_stopped_then_started":
            yield b.ce_project.eq(1)
            for _ in range(3000):
                yield
                if (yield b.check.done):
                    break
            out["crc"] = yield b.check.crc
            out["done"] = yield b.check.done
            yield from do_check(b, 40, out, "again")

    run(b, [sysgen()], project=None if how == "clock_hz_zero" else (39_700, 5_050))
    assert status == {"busy": 1, "done": 0, "busy2": 1}
    if how == "clock_stopped_then_started":
        assert out["done"] == 1
        assert out["crc"] == out["again"] == zlib.crc32(toy_bytes(40))


# Verilator, with real designs ----------------------------------------------------------------

import subprocess
from pathlib import Path

from migen import ClockDomain
from migen.fhdl import verilog

from tt_harness import pool, tools, verdict, verilate

HARNESS = Path(__file__).resolve().parent.parent / "tt_harness" / "sim" / "slot_tb.cpp"
TOP_NAME = "slot_tb_top"
SYS_PS = 20_000  # 50 MHz
DESIGNS = ["tt07/tt_um_tinytapeout_dvd_screensaver", "tt08/tt_um_tinytapeout_logo_screensaver"]


class _VerilatorTop(Module):
    def __init__(self, top_module: str, clock_hz: int) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.submodules.slot = slot = ProjectSlot(
            top_module, Signal(), 100_000_000, clock_hz, sim_clock=True
        )
        self.submodules.check = check = AtSpeedCheck()
        slot.attach(check)
        p = {n: Signal(name=n) for n in ("rst_n", "ena", "gamepad_enable", "clock_enable", "start")}
        p["ui_in"] = Signal(8, name="ui_in")
        p["clocks"] = Signal(32, name="clocks")
        p["done"] = Signal(name="done")
        p["busy"] = Signal(name="busy")
        p["crc"] = Signal(32, name="crc")
        p["pin_rst_n"] = Signal(name="pin_rst_n")
        p["locked"] = Signal(name="locked")
        p["clock_running"] = Signal(name="clock_running")
        self.comb += [
            slot.rst_n.eq(p["rst_n"]),
            slot.ena.eq(p["ena"]),
            slot.gamepad_enable.eq(p["gamepad_enable"]),
            slot.clock_enable.eq(p["clock_enable"]),
            slot.ui_in.eq(p["ui_in"]),
            check.start.eq(p["start"]),
            check.clocks.eq(p["clocks"]),
            p["done"].eq(check.done),
            p["busy"].eq(check.busy),
            p["crc"].eq(check.crc),
            p["pin_rst_n"].eq(slot.pin_rst_n),
            p["locked"].eq(slot.locked),
            p["clock_running"].eq(slot.clock_running),
        ]
        self.ios = {self.cd_sys.clk, self.cd_sys.rst, slot.cd_project.clk, *p.values()}


class _Sim:
    def __init__(self, binary: Path, ref: verdict.Reference) -> None:
        self.binary = binary
        self.ref = ref
        self.proj_ps = round(1e12 / ref.clock_hz) // 2 * 2

    def run(self, script: list[str], proj_off: int = 3_333, timeout: int = 900) -> list[str]:
        cmd = [
            str(self.binary),
            "--sys-ps",
            str(SYS_PS),
            "--proj-ps",
            str(self.proj_ps),
            "--proj-off",
            str(proj_off),
        ]
        r = subprocess.run(
            cmd,
            input="\n".join(script) + "\n",
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.splitlines()

    def limit(self, n: int) -> int:
        return (n + 1000) * (self.proj_ps + 100)  # the launch delay is ~400 sys clocks

    def check_script(self, n: int, settle_ps: int = 2_000_000) -> list[str]:
        return [
            "set rst_n 1",
            "set ena 1",
            f"run {settle_ps}",
            f"set clocks {n:x}",
            "pulse_start",
            f"wait_done {self.limit(n)}",
        ]

    def crc(self, n: int) -> tuple[int, int]:
        """(done, crc) of one check of N clocks straight after configuration."""
        done, _, crc, _ = self.run(self.check_script(n))[-1].split()
        return int(done), int(crc, 16)

    def expected(self, n: int) -> int:
        return zlib.crc32(self.ref.samples[:n])


@pytest.fixture(scope="module", params=DESIGNS, ids=lambda d: d.split("/")[1])
def design(request, tmp_path_factory) -> _Sim:
    if tools.find("verilator") is None:
        pytest.skip("verilator not installed")
    project_id = request.param
    ref_dir = verdict.default_reference_dir()
    if not (ref_dir / project_id / "reference.json").is_file():
        pytest.skip(f"no shared reference for {project_id} under {ref_dir}")
    ref = verdict.load_reference(ref_dir, project_id)
    try:
        sources = pool.load_sources(project_id)
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
    ) as e:  # no checkout, or a cold cache and no network
        pytest.skip(f"cannot load sources of {project_id}: {e}")
    assert (ref.reset_clocks, ref.ui_in, ref.uio_in) == (RESET_CLOCKS, 0, 0)
    assert sources.project.ui_in == 0 and sources.project.uio_in == 0
    work = tmp_path_factory.mktemp(f"slot-{sources.project.top_module}")
    top = _VerilatorTop(sources.project.top_module, ref.clock_hz)
    top_v = work / "top.v"
    verilog.convert(top, ios=top.ios, name=TOP_NAME).write(str(top_v))
    binary = verilate.build(
        sources, TOP_NAME, HARNESS, [top_v], work / "obj", work / "verilator.log"
    )
    return _Sim(binary, ref)


@pytest.mark.toolchain
@pytest.mark.parametrize("n", [1, 9, 10, 11, 100, 1_000_000, 6_000_000])
def test_crc_equals_crc_of_reference_dump(design: _Sim, n: int) -> None:
    done, crc = design.crc(n)
    assert done == 1, "the check did not finish"
    assert crc == design.expected(n)


@pytest.mark.toolchain
def test_crc_differs_when_one_byte_differs(design: _Sim) -> None:
    n = 5_000
    done, crc = design.crc(n)
    assert done == 1 and crc == design.expected(n)
    for k in (0, 9, 10, 11, n // 2, n - 1):  # around the reset boundary and at the ends
        flipped = bytearray(design.ref.samples[:n])
        flipped[k] ^= 0x01
        assert zlib.crc32(bytes(flipped)) != crc, k
    # A one-clock misalignment of the pipeline would also show: the dump shifted by one.
    assert zlib.crc32(design.ref.samples[1 : n + 1]) != crc
    assert zlib.crc32(design.ref.samples[: n - 1]) != crc
    # And the check itself is sensitive to N: one clock more or less is a different CRC.
    assert design.crc(n + 1) == (1, design.expected(n + 1)) != (1, crc)
    assert design.crc(n - 1) == (1, design.expected(n - 1)) != (1, crc)


@pytest.mark.toolchain
def test_restart_gives_the_same_crc(design: _Sim) -> None:
    n = 20_000
    script = ["set rst_n 1", "set ena 1", "run 2000000", f"set clocks {n:x}"]
    for _ in range(3):  # no reconfiguration, nothing touched in between
        script += ["pulse_start", f"wait_done {design.limit(n)}"]
    out = [ln.split() for ln in design.run(script)]
    assert [o[0] for o in out] == ["1", "1", "1"]
    assert {int(o[2], 16) for o in out} == {design.expected(n)}


@pytest.mark.toolchain
def test_start_at_every_phase_of_the_project_clock_gives_the_same_crc(design: _Sim) -> None:
    n = 3_000
    # `run` before each start moves it against the project clock; the clocks are unrelated.
    waits_ps = [0, 977, 2_001, 4_999, 9_973, 12_345, 17_011, 19_999, 23_457, 31_111, 38_501, 39_721]
    script = ["set rst_n 1", "set ena 1", "run 2000000", f"set clocks {n:x}"]
    for w in waits_ps:
        script += [f"run {w}", "pulse_start", f"wait_done {design.limit(n)}"]
    out = [ln.split() for ln in design.run(script)]
    assert [o[0] for o in out] == ["1"] * len(waits_ps)
    assert {int(o[2], 16) for o in out} == {design.expected(n)}
    # And with the project clock starting at other phases against the system clock.
    for off in (1_111, 9_999, 19_000, 27_777, 35_555):
        done, _, crc, _ = design.run(design.check_script(n), proj_off=off)[-1].split()
        assert (int(done), int(crc, 16)) == (1, design.expected(n)), off


@pytest.mark.toolchain
def test_reset_asserts_with_the_project_clock_stopped_in_verilator(design: _Sim) -> None:
    """The reset cell as Migen emits it: the pin falls at once with no clock, and rises only
    after the clock restarts (second project edge after it)."""
    script = [
        "set rst_n 1",
        "run 2000000",
        "show",  # pin high
        "proj 0",
        "set rst_n 0",
        "run 60000",  # three sys clocks (the request is a sys flop); the project clock is stopped
        "show",
        "set rst_n 1",
        "run 2000000",
        "show",  # released, but no clock: still low
        "proj 1",
        "run 30000",  # one edge so far (the first is half a period after the restart)
        "show",
        "run 40000",
        "show",
    ]
    out = [ln.split() for ln in design.run(script)]
    pins = [int(o[3]) for o in out]
    assert pins == [1, 0, 0, 0, 1], pins
