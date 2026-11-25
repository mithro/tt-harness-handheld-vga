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
from migen import Cat, ClockDomain, Instance, Module, ResetSignal, Signal
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
TRACES = DATA / "demo" / "traces"
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

    emu.buttons.reset = word  # in place before the first edge

    def gen():
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


@pytest.mark.parametrize(
    ("trace_name", "report", "controllers", "word"),
    [
        ("gamepad-two-controllers", "start_and_a", (("start",), ("a",)), 0x008100),
        ("gamepad-resent-after-reset", "none", (), 0xFFFFFF),
    ],
)
def test_levels_of_one_frame_are_the_runners_levels(trace_name, report, controllers, word) -> None:
    trace = json.loads((TRACES / f"{trace_name}.json").read_text())
    runner_levels = trace["reports"][report]
    assert gamepad_bits(controllers) == word
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
    assert emu.frame_clocks == round(SIM_FREQ / FRAME_RATE_HZ) == 1667
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
    assert (
        frame.raw[emu.frame_clocks : emu.frame_clocks + FRAME_SLOTS * hold]
        == frame.raw[: FRAME_SLOTS * hold]
    )
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


def test_frames_are_one_default_period_apart() -> None:
    emu = GamepadEmulator(SIM_FREQ)
    frame = _run(emu, 3 * emu.frame_clocks, 0x008100)
    assert len(frame.frame_cycles) == 3
    gaps = {b - a for a, b in zip(frame.frame_cycles, frame.frame_cycles[1:], strict=False)}
    assert gaps == {round(SIM_FREQ / FRAME_RATE_HZ)} == {1667}


def test_frames_are_one_given_period_apart() -> None:
    emu = GamepadEmulator(SIM_FREQ, frame_rate_hz=2000)
    frame = _run(emu, 4 * emu.frame_clocks, 0x008100)
    assert len(frame.frame_cycles) == 4
    gaps = {b - a for a, b in zip(frame.frame_cycles, frame.frame_cycles[1:], strict=False)}
    assert gaps == {round(SIM_FREQ / 2000)} == {500}


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
    """Emulator -> the real `gamepad_pmod_dual` or `gamepad_pmod_single`, one clock.

    `rst` resets both; `rx_rst` resets the receiver alone. With the single receiver `pressed1`
    is 0 and bit 1 of `present` is 0.
    """

    def __init__(self, clk_freq_hz: int, receiver: str = "dual") -> None:
        self.clock_domains.cd_sys = ClockDomain()
        self.rst = Signal()
        self.rx_rst = Signal()
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
        width = 2 if receiver == "dual" else 1
        wires = {name: Signal(width, name=f"w_{name}") for name in RECEIVER_OUTPUTS}
        is_present = Signal(width)
        self.specials += Instance(
            f"gamepad_pmod_{receiver}",
            i_rst_n=~(self.rst | self.rx_rst),
            i_clk=self.cd_sys.clk,
            i_pmod_data=emu.data,
            i_pmod_clk=emu.clock,
            i_pmod_latch=emu.latch,
            o_is_present=is_present,
            **{f"o_{name}": wire for name, wire in wires.items()},
        )
        self.comb += [
            self.present.eq(is_present),
            self.pressed0.eq(Cat(*[w[0] for w in wires.values()])),
        ]
        if receiver == "dual":
            self.comb += self.pressed1.eq(Cat(*[w[1] for w in wires.values()]))


def _build_bench(work: Path, clk_freq_hz: int, receiver: str) -> Path:
    top = _Bench(clk_freq_hz, receiver)
    ios = {
        top.cd_sys.clk,
        top.rst,
        top.rx_rst,
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


@pytest.fixture(scope="module", params=FREQS, ids=[f"{f}Hz" for f in FREQS])
def bench(request, tmp_path_factory) -> Path:
    """The Verilated bench (dual receiver) for one project clock frequency: the executable."""
    if tools.find("verilator") is None:
        pytest.skip("verilator not installed")
    work = tmp_path_factory.mktemp(f"gamepad-bench-{request.param}")
    return _build_bench(work, request.param, "dual")


@pytest.fixture(scope="module", params=FREQS, ids=[f"{f}Hz" for f in FREQS])
def single_bench(request, tmp_path_factory) -> Path:
    """The same with `gamepad_pmod_single`."""
    if tools.find("verilator") is None:
        pytest.skip("verilator not installed")
    work = tmp_path_factory.mktemp(f"gamepad-single-bench-{request.param}")
    return _build_bench(work, request.param, "single")


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
        out.append(
            Seen(int(p0, 16), int(p1, 16), int(present, 16), int(latch), int(clock), int(data))
        )
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
def test_every_reset_gives_a_complete_first_frame(bench) -> None:
    # Both are reset, as at power-up: the first frame after the reset is complete and the
    # receiver decodes it, however often that is repeated.
    word = gamepad_bits((("l",), ("r",)))
    first, second = _script(
        bench,
        [f"buttons {word:x}", "reset 4", "frames 1", "show", "reset 4", "frames 1", "show"],
    )
    assert first == second
    assert (first.pressed0, first.pressed1, first.present) == (_mask("l"), _mask("r"), 3)


# The single-controller receiver (the common case) ---------------------------------------------
#
# `gamepad_pmod_single` has a 12-bit shift register, so after the 24 bits of a frame it holds the
# last 12 sent: player 1 (word bits 12..23). Player 2's half has been shifted out. A player 1
# that is absent (twelve ones) reads as "not present" and no buttons, whatever player 2 holds.


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_single_receiver_reads_each_button_of_player_1(single_bench) -> None:
    words = [gamepad_bits(((name,),)) for name in GAMEPAD_BUTTONS]
    for name, seen in zip(GAMEPAD_BUTTONS, _receive(single_bench, words), strict=True):
        assert (seen.pressed0, seen.present) == (_mask(name), 1), name


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_single_receiver_reports_player_1_only_while_player_2_holds_another(single_bench) -> None:
    names = GAMEPAD_BUTTONS
    cases = [(name, names[(i + 5) % len(names)]) for i, name in enumerate(names)]
    words = [gamepad_bits(((one,), (two,))) for one, two in cases]
    for (one, two), seen in zip(cases, _receive(single_bench, words), strict=True):
        assert one != two
        assert (seen.pressed0, seen.present) == (_mask(one), 1), (one, two)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_single_receiver_with_player_1_absent_and_player_2_present(single_bench) -> None:
    # `gamepad_bits` cannot say "player 1 absent, player 2 present" (an absent controller is
    # always the last one listed), so the word is built by hand: player 2 holds start in bits
    # 0..11, player 1's bits 12..23 are twelve ones, "no controller".
    word = 0xFFF000 | _mask("start")
    seen = _receive(single_bench, [word])[0]
    # The receiver's register holds twelve ones: not present, no button held.
    assert (seen.pressed0, seen.present) == (0, 0)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_single_receiver_with_no_controllers(single_bench) -> None:
    seen = _receive(single_bench, [gamepad_bits(()), gamepad_bits(((), ()))])
    assert (seen[0].pressed0, seen[0].present) == (0, 0)  # none listed: twelve ones
    assert (seen[1].pressed0, seen[1].present) == (0, 1)  # both present, nothing held


# A receiver that is reset while the emulator keeps running ------------------------------------


def _decode(register: int) -> tuple[int, int, int]:
    """What `gamepad_pmod_dual` decodes from its 24-bit `data_reg`: (pressed0, pressed1, present)."""

    def one(bits: int) -> tuple[int, int]:
        if bits == 0xFFF:
            return 0, 0
        # `b` is bit 11 of the register and button 0, `r` bit 0 and button 11.
        return sum(((bits >> (11 - k)) & 1) << k for k in range(12)), 1

    p0, present0 = one(register & 0xFFF)
    p1, present1 = one(register >> 12)
    return p0, p1, present0 | present1 << 1


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
def test_receiver_released_from_reset_mid_frame_is_right_within_two_frames(bench, request) -> None:
    freq = request.node.callspec.params["bench"]
    emu = GamepadEmulator(freq)
    slot = emu.slot_clocks
    word = gamepad_bits((("l", "up"), ("select",)))
    # The receiver is held in reset through the whole first frame. The second frame starts
    # `frame_clocks - 74 * slot` clocks after the first `frame` pulse; release in the middle of
    # slot 30 (data phase of bit 10), so the receiver sees the rising clock edges of bits 10..23.
    to_release = emu.frame_clocks - 74 * slot + 30 * slot + slot // 2
    seen = _script(
        bench,
        [
            "rxrst 1",
            "reset 4",
            f"buttons {word:x}",
            "frames 1",
            "show",  # still in reset: not present
            f"run {to_release}",
            "rxrst 0",
            "frames 1",
            "show",  # the frame that was half seen
            "frames 1",
            "show",  # the next, whole frame
        ],
    )
    held, partial, whole = seen
    assert (held.pressed0, held.pressed1, held.present) == (0, 0, 0)
    # The shift register starts at all ones, so the half-seen frame gives 14 ones... of the
    # register's 24 bits the low 14 are the bits that were sent, the rest are the reset ones.
    # The receiver decodes that: wrong buttons for one frame, as with the real Pmod.
    register = 0xFFFFFF
    for n in range(10, 24):
        register = (register << 1 | (word >> n) & 1) & 0xFFFFFF
    assert (partial.pressed0, partial.pressed1, partial.present) == _decode(register)
    assert (partial.pressed0, partial.pressed1) != (_mask("l", "up"), _mask("select"))
    assert (whole.pressed0, whole.pressed1, whole.present) == (
        _mask("l", "up"),
        _mask("select"),
        3,
    )
