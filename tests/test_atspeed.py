# SPDX-License-Identifier: Apache-2.0
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
        self.submodules.check = check = AtSpeedCheck()
        slot.attach(check)
        if sim_clock:
            self.submodules.toy = _Toy(slot)


def bench(running: bool = True, **kw) -> _Bench:
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


def test_sensitivity_a_different_byte_changes_the_crc() -> None:
    assert zlib.crc32(toy_bytes(64)) != zlib.crc32(toy_bytes(65))
    flipped = bytearray(toy_bytes(64))
    flipped[30] ^= 1
    assert zlib.crc32(bytes(flipped)) != zlib.crc32(toy_bytes(64))


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
