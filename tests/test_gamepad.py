# SPDX-License-Identifier: Apache-2.0
"""GamepadEmulator tests.

Two kinds. The Migen-simulator tests need no toolchain: the exact level sequence of a frame
(against the Task 1 trace of the demo runner), its timing in clock cycles, when `buttons` is
sampled, and that the three outputs are register outputs. The Verilator tests (marked
`toolchain`) wire the emulator to the REAL receiver, `tests/data/gamepad/gamepad_pmod.v`
(`gamepad_pmod_dual`), both on one clock, at two project clock frequencies, and check what
the receiver decodes.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from migen import ClockDomain, Instance, Module, Signal, ResetSignal
from migen.fhdl import verilog
from migen.sim import run_simulation

from tt_harness import tools, verilate
from tt_harness.compare import inputs
from tt_harness.core.gamepad import (
    CLOCK_HALF_PERIOD_US,
    FRAME_RATE_HZ,
    FRAME_SLOTS,
    GamepadEmulator,
)
from tt_harness.demo.validate import GAMEPAD_BUTTONS, gamepad_bits

DATA = Path(__file__).parent / "data"
TRACE = DATA / "demo" / "traces" / "gamepad-two-controllers.json"
RECEIVER = DATA / "gamepad" / "gamepad_pmod.v"
HARNESS = Path(__file__).resolve().parent.parent / "tt_harness" / "sim" / "gamepad_tb.cpp"

LATCH, CLOCK, DATA_BIT = 0x10, 0x20, 0x40  # ui_in bits 4..6, as in docs/demo-runner.md
FREQS = [25_175_000, 50_000_000]


def _level(latch: int, clock: int, data: int) -> int:
    return (LATCH if latch else 0) | (CLOCK if clock else 0) | (DATA_BIT if data else 0)


def _expected_levels(word: int) -> list[int]:
    """The levels the runner writes for `word` (docs/demo-runner.md), repeats left out."""
    seq = []
    data = 0
    for i in range(24):
        data = DATA_BIT if (word >> i) & 1 else 0
        seq += [data, data | CLOCK, data]
    seq += [data | LATCH, data, 0]
    out, last = [], 0
    for level in seq:
        if level != last:
            out.append(level)
        last = level
    return out


# Migen simulator ---------------------------------------------------------------------------

SIM_FREQ = 1_000_000  # the lowest the block supports: one slot is 4 clocks


@dataclass
class Frame:
    """One run of the emulator in the Migen simulator."""

    levels: list[int]  # distinct levels in order, from idle, ending at idle
    raw: list[int]  # the level in every cycle
    frame_cycles: list[int]  # cycles in which `frame` was high


def _run(
    emu: GamepadEmulator,
    cycles: int,
    word: int,
    change: dict[int, int] | None = None,
) -> Frame:
    """Run `cycles` clocks with `buttons` = `word`, set to `change[cycle]` at those cycles."""
    raw: list[int] = []
    frame_cycles: list[int] = []

    def gen():
        yield emu.buttons.eq(word)
        for cycle in range(cycles):
            if change and cycle in change:
                yield emu.buttons.eq(change[cycle])
            yield
            latch = yield emu.latch
            clock = yield emu.clock
            data = yield emu.data
            raw.append(_level(latch, clock, data))
            if (yield emu.frame):
                frame_cycles.append(cycle)

    run_simulation(emu, gen())
    levels, last = [], 0
    for level in raw:
        if level != last:
            levels.append(level)
        last = level
    return Frame(levels, raw, frame_cycles)


def test_levels_of_one_frame_are_the_runners_levels() -> None:
    trace = json.loads(TRACE.read_text())
    runner_levels = trace["reports"]["start_and_a"]
    word = gamepad_bits((("start",), ("a",)))
    assert word == 0x008100
    emu = GamepadEmulator(SIM_FREQ)
    frame = _run(emu, emu.frame_clocks - 1, word)  # one frame, ends before the next starts
    assert len(frame.frame_cycles) == 1
    assert frame.levels == runner_levels
    assert frame.levels == _expected_levels(word)


@pytest.mark.parametrize(
    "controllers",
    [(), (("up", "a"),), ((), ()), (("b", "r"), ("l",)), (("select",),)],
)
def test_levels_follow_the_word_for_other_reports(controllers) -> None:
    word = gamepad_bits(controllers)
    emu = GamepadEmulator(SIM_FREQ)
    frame = _run(emu, emu.frame_clocks - 1, word)
    assert frame.levels == _expected_levels(word)


def test_timing_is_exact_in_clock_cycles() -> None:
    emu = GamepadEmulator(SIM_FREQ)
    hold = emu.slot_clocks
    assert hold == 4  # CLOCK_HALF_PERIOD_US * 1 MHz
    assert emu.frame_clocks == SIM_FREQ // FRAME_RATE_HZ
    word = gamepad_bits((("start",), ("a",)))
    frame = _run(emu, 2 * emu.frame_clocks, word)
    # `_run` records cycle n after the clock edge n+1 of the simulator, so output index i is
    # the level in the cycle after the i-th edge. The first frame starts at the first edge:
    # slot s occupies indices [s*hold, (s+1)*hold).
    for slot in range(FRAME_SLOTS):
        chunk = frame.raw[slot * hold : (slot + 1) * hold]
        assert len(set(chunk)) == 1, f"slot {slot} is not steady: {chunk}"
    # The pulse is in the first cycle of the idle slot after the latch, once per frame.
    first = (FRAME_SLOTS - 1) * hold
    assert frame.frame_cycles == [first, first + emu.frame_clocks]
    # The next frame starts exactly one frame period after the first.
    assert frame.raw[emu.frame_clocks : emu.frame_clocks + FRAME_SLOTS * hold] == frame.raw[
        : FRAME_SLOTS * hold
    ]
    # Idle between frames: all three low.
    assert set(frame.raw[FRAME_SLOTS * hold - hold : emu.frame_clocks]) == {0}


def test_every_level_is_held_for_whole_slots() -> None:
    emu = GamepadEmulator(SIM_FREQ)
    frame = _run(emu, emu.frame_clocks, gamepad_bits((("start",), ("a",))))
    runs, length = [], 1
    for prev, cur in zip(frame.raw, frame.raw[1:], strict=False):
        if cur == prev:
            length += 1
        else:
            runs.append(length)
            length = 1
    # Every run before the idle tail is a whole number of slots, at least one slot.
    assert runs
    assert all(r % emu.slot_clocks == 0 and r >= emu.slot_clocks for r in runs)


@pytest.mark.parametrize("freq", [1_000_000, 25_175_000, 50_000_000])
def test_constants_derive_from_the_clock_frequency(freq: int) -> None:
    emu = GamepadEmulator(freq)
    assert emu.slot_clocks == max(4, round(freq * CLOCK_HALF_PERIOD_US / 1_000_000))
    assert emu.frame_clocks == round(freq / FRAME_RATE_HZ)
    assert emu.frame_clocks > FRAME_SLOTS * emu.slot_clocks


def test_a_frame_period_shorter_than_a_frame_is_refused() -> None:
    with pytest.raises(ValueError, match="frame"):
        GamepadEmulator(SIM_FREQ, frame_rate_hz=20_000)
    with pytest.raises(ValueError, match="clk_freq_hz"):
        GamepadEmulator(0)


def test_buttons_change_only_between_frames() -> None:
    emu = GamepadEmulator(SIM_FREQ)
    old = gamepad_bits((("start",), ("a",)))
    new = gamepad_bits((("x",), ("up", "down")))
    # `new` is written 50 cycles into the first frame (one frame is 300 cycles).
    frame = _run(emu, 2 * emu.frame_clocks, old, change={50: new})
    first = frame.raw[: emu.frame_clocks]
    second = frame.raw[emu.frame_clocks :]

    def distinct(raw):
        out, last = [], 0
        for level in raw:
            if level != last:
                out.append(level)
            last = level
        return out

    assert distinct(first) == _expected_levels(old)
    assert distinct(second) == _expected_levels(new)


def test_buttons_are_sampled_in_the_cycle_the_frame_starts() -> None:
    emu = GamepadEmulator(SIM_FREQ)
    old, new = 0x000001, 0x000000
    # Changed in the cycle after the first edge: too late for frame 1, in time for frame 2.
    frame = _run(emu, 2 * emu.frame_clocks, old, change={1: new})
    # bit 0 is sent first: data is high in slot 0 of the first frame only.
    assert frame.raw[0] & DATA_BIT
    assert not frame.raw[emu.frame_clocks] & DATA_BIT


class _Walk(Module):
    """The emulator with its three outputs on the inputs of one instance, as a project's."""

    def __init__(self) -> None:
        self.emu = GamepadEmulator(25_175_000)
        self.submodules += self.emu
        self.specials += Instance(
            "thing",
            name="project",
            i_latch=self.emu.latch,
            i_clock=self.emu.clock,
            i_data=self.emu.data,
        )


def test_outputs_are_register_outputs() -> None:
    found = inputs.project_inputs(_Walk(), "project")
    assert set(found) == {"latch", "clock", "data"}
    for port, source in found.items():
        assert source.kind == "register", f"{port}: {source.kind}: {source.detail}"


# Verilator, against the real receiver -------------------------------------------------------

needs_verilator = [
    pytest.mark.toolchain,
    pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed"),
]

RECEIVER_OUTPUTS = ("b", "y", "select", "start", "up", "down", "left", "right", "a", "x", "l", "r")
assert RECEIVER_OUTPUTS == GAMEPAD_BUTTONS


class _Bench(Module):
    """Emulator -> gamepad_pmod_dual, one clock. `rst` resets both."""

    def __init__(self, clk_freq_hz: int) -> None:
        self.clock_domains.cd_sys = ClockDomain()
        self.rst = Signal()
        self.buttons = Signal(24)
        self.frame = Signal()
        self.latch = Signal()
        self.clock = Signal()
        self.data = Signal()
        self.pressed0 = Signal(12)
        self.pressed1 = Signal(12)
        self.present = Signal(2)

        emu = GamepadEmulator(clk_freq_hz)
        self.submodules.emu = emu
        self.comb += [
            ResetSignal("sys").eq(self.rst),
            emu.buttons.eq(self.buttons),
            self.frame.eq(emu.frame),
            self.latch.eq(emu.latch),
            self.clock.eq(emu.clock),
            self.data.eq(emu.data),
        ]
        wires = {name: Signal(2, name=f"w_{name}") for name in RECEIVER_OUTPUTS}
        is_present = Signal(2)
        self.specials += Instance(
            "gamepad_pmod_dual",
            i_rst_n=~self.rst,
            i_clk=self.cd_sys.clk,
            i_pmod_data=emu.data,
            i_pmod_clk=emu.clock,
            i_pmod_latch=emu.latch,
            o_is_present=is_present,
            **{f"o_{name}": wire for name, wire in wires.items()},
        )
        self.comb += [
            self.present.eq(is_present),
            self.pressed0.eq(_cat([w[0] for w in wires.values()])),
            self.pressed1.eq(_cat([w[1] for w in wires.values()])),
        ]


def _cat(bits):
    from migen import Cat

    return Cat(*bits)


@pytest.fixture(scope="module", params=FREQS, ids=[f"{f}Hz" for f in FREQS])
def bench(request, tmp_path_factory) -> Path:
    """The Verilated bench for one project clock frequency; returns its executable."""
    if tools.find("verilator") is None:
        pytest.skip("verilator not installed")
    work = tmp_path_factory.mktemp(f"gamepad-bench-{request.param}")
    top = _Bench(request.param)
    ios = {
        top.cd_sys.clk,
        top.rst,
        top.buttons,
        top.frame,
        top.latch,
        top.clock,
        top.data,
        top.pressed0,
        top.pressed1,
        top.present,
    }
    top_v = work / "top.v"
    verilog.convert(top, ios=ios, name="top").write(str(top_v))
    verilator = tools.find("verilator")
    log = work / "verilator.log"
    obj = work / "obj"
    cmd = [
        str(verilator),
        *verilate.BASE_FLAGS,
        "-j",
        "8",
        "--top-module",
        "top",
        "-Mdir",
        str(obj),
        str(HARNESS),
        str(top_v),
        str(RECEIVER),
    ]
    rc = verilate.run(cmd, log, work, verilate.BUILD_TIMEOUT_S)
    assert rc == 0, f"verilator failed, see {log}"
    return obj / "Vtop"


@dataclass(frozen=True)
class Seen:
    pressed0: int
    pressed1: int
    present: int
    latch: int
    clock: int
    data: int


def _script(bench: Path, lines: list[str]) -> list[Seen]:
    done = subprocess.run(
        [str(bench)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = []
    for row in done.stdout.splitlines():
        p0, p1, present, latch, clock, data = row.split()
        out.append(Seen(int(p0, 16), int(p1, 16), int(present, 16), int(latch), int(clock), int(data)))
    return out


def _receive(bench: Path, words: list[int]) -> list[Seen]:
    """For each word: reset, set `buttons`, wait for one frame, and read the receiver."""
    lines = []
    for word in words:
        lines += ["reset 4", f"buttons {word:x}", "frames 1", "show"]
    return _script(bench, lines)


def _mask(*names: str) -> int:
    return sum(1 << GAMEPAD_BUTTONS.index(name) for name in names)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_each_single_button_is_received_on_controller_1(bench) -> None:
    words = [gamepad_bits(((name,),)) for name in GAMEPAD_BUTTONS]
    for name, seen in zip(GAMEPAD_BUTTONS, _receive(bench, words), strict=True):
        assert (seen.pressed0, seen.present & 1) == (_mask(name), 1), name
        # The second controller is not listed: absent, so no button reads as held.
        assert (seen.pressed1, seen.present >> 1) == (0, 0), name


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_each_single_button_on_controller_2(bench) -> None:
    words = [gamepad_bits(((), (name,))) for name in GAMEPAD_BUTTONS]
    for name, seen in zip(GAMEPAD_BUTTONS, _receive(bench, words), strict=True):
        assert (seen.pressed1, seen.present >> 1) == (_mask(name), 1), name
        assert (seen.pressed0, seen.present & 1) == (0, 1), name


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_two_controllers_together(bench) -> None:
    cases = [
        (("start",), ("a",)),
        (("up", "left", "b"), ("down", "right", "l", "r")),
        (("b", "y", "select", "start", "up", "down", "left", "right", "a", "x", "l"), ("r",)),
        ((), ()),
    ]
    seen = _receive(bench, [gamepad_bits(c) for c in cases])
    for (p1, p2), got in zip(cases, seen, strict=True):
        assert (got.pressed0, got.pressed1, got.present) == (_mask(*p1), _mask(*p2), 3), (p1, p2)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_no_controller_reads_as_absent(bench) -> None:
    seen = _receive(bench, [gamepad_bits(()), gamepad_bits(((), ())), gamepad_bits(((),))])
    assert (seen[0].pressed0, seen[0].pressed1, seen[0].present) == (0, 0, 0)
    assert (seen[1].pressed0, seen[1].pressed1, seen[1].present) == (0, 0, 3)
    assert (seen[2].pressed0, seen[2].pressed1, seen[2].present) == (0, 0, 1)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_buttons_change_only_between_frames_as_the_receiver_sees(bench) -> None:
    old = gamepad_bits((("start",), ("a",)))
    new = gamepad_bits((("x",), ("up",)))
    lines = [
        "reset 4",
        f"buttons {old:x}",
        "run 100",  # inside the first frame
        f"buttons {new:x}",
        "frames 1",
        "show",
        "frames 1",
        "show",
    ]
    first, second = _script(bench, lines)
    assert (first.pressed0, first.pressed1) == (_mask("start"), _mask("a"))
    assert (second.pressed0, second.pressed1) == (_mask("x"), _mask("up"))


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_receiver_restarts_after_a_reset_of_the_design(bench) -> None:
    # The receiver alone is reset (as the demo runner's reset pulse does); the emulator keeps
    # sending, so the next frame restores the buttons. Here both are reset: the first frame
    # after the reset is complete and correct.
    word = gamepad_bits((("l",), ("r",)))
    first, second = _script(
        bench,
        [f"buttons {word:x}", "reset 4", "frames 1", "show", "reset 4", "frames 1", "show"],
    )
    assert first == second
    assert (first.pressed0, first.pressed1, first.present) == (_mask("l"), _mask("r"), 3)
