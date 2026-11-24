# SPDX-License-Identifier: Apache-2.0
"""A probe that measures the project clock on a real Arty A7-35T (plan 1, Task 7c).

The design: the board's 100 MHz oscillator, one `ProjectClock` (MMCME2_ADV, free-running
BUFG, gated BUFGCE) at an MMCM setting given at build time, and in the oscillator's domain

- `F`: a `FreqMeter` on the free-running BUFG output, 1 s gate (100,000,000 oscillator cycles);
- `G`: a second `FreqMeter` on the gated BUFGCE output, same gate. `ProjectClock` enables that
  buffer with `enable & locked` (enable is tied high here), so G equals F only if the clock
  was enabled for the whole gate;
- `H`: a `HighTimeMeter` on a replica of the free-running clock's waveform, the number of
  oscillator cycles in the gate at which the clock was high (see "Duty cycle" below);
- `L`, `U`: LOCKED through a synchroniser, and a saturating count of its falling edges since
  configuration;
- a UART (115200 baud, 8N1) that sends one line per gate.

Line format, fixed width, 51 bytes with the newline; all numbers are upper-case hexadecimal:

    F 0180232A G 0180232A H 02FAF080 L 1 U 00 N 03 *57

`N` is the gate's number since configuration (it wraps at 256); gate 0 includes the time the
MMCM took to lock. The two digits after `*` are the exclusive-or of every byte before the
`*`, so a corrupted line is rejected and not misread (`parse_line`).

The reading is a ratio: the gate and the MMCM's input are the same oscillator, so its error
cancels and the ideal count is exactly gate x MULT / (DIVCLK x DIVIDE) (`ideal_count`).

Duty cycle. A clock net cannot be routed to a data pin in the open flow, so the waveform is
rebuilt in fabric: one flop toggles on every rising edge of the free-running clock, a second
(an FDRE with an inverted clock) copies it on every falling edge, and their exclusive-or is
high from each rising edge to the next falling edge. That level is sampled by the oscillator
clock. The two clocks are coherent (one oscillator), so the samples visit a fixed set of
phases of the measured period: 143 for the 35.75 divider, 49 for 36.75, 4 for 36
(`freq_meter.sampling_phases`). H / gate is therefore the duty cycle only to within one
phase step (0.70% of the period for 35.75, 2.0% for 36.75, 25% for 36), plus the difference
between the two flops' clock-to-output delays and the rise and fall delays of the clock tree
and the LUT (some tens of picoseconds; one step of 1/143 is 278 ps).

Clock domain crossings. Every signal entering the oscillator domain does so through a
two-flop `MultiReg` (the Gray bits of each meter, the replica level, LOCKED), and
`ProjectClock`'s enable crosses the other way through one. They are asynchronous by design.
Vivado is told by LiteX's own constraints (the first flop of every MultiReg carries the
attribute `mr_ff`, and the XDC has a false path through nets driven by such flops, and
ASYNC_REG on both); everything else between the two clocks would be timed, and there is
nothing else. nextpnr-xilinx times only paths within one clock; it reports the crossings as
`<clock> -> <clock>` delays and applies no requirement to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from functools import reduce
from operator import xor

from migen import Array, ClockDomain, ClockSignal, If, Instance, Module, Signal
from migen.genlib.cdc import MultiReg

from tt_harness import clock_build as cb
from tt_harness.compare.recorder import _UartTx
from tt_harness.core.clocking import MmcmSetting, ProjectClock, explicit_setting
from tt_harness.core.freq_meter import FreqMeter, Gate, HighTimeMeter, sampling_phases

CLK_HZ = cb.ARTY.clk_hz
GATE_CYCLES = 100_000_000
BAUD = 115_200
# name -> (DIVCLK_DIVIDE, CLKFBOUT_MULT_F, CLKOUT0_DIVIDE_F), from the 100 MHz oscillator.
SETTINGS = {
    "A": (1, 9.0, 35.75),  # the solver's own choice for 25.175 MHz on the Arty
    "B": (2, 18.0, 35.75),  # the Game Bub's multiplier and divider: PFD 50 MHz, VCO 900 MHz
    "C": (1, 9.0, 36.75),  # fractional with an even integer part: 24.4898 MHz
    "D": (1, 9.0, 36.0),  # integer: 25.000 MHz
}
TOOLCHAINS = ("vivado", "openxc7")


def setting(name: str) -> MmcmSetting:
    return explicit_setting(CLK_HZ, *SETTINGS[name])


def ideal_count(s: MmcmSetting, gate_cycles: int = GATE_CYCLES) -> Fraction:
    """Rising edges of the MMCM output in `gate_cycles` cycles of its input clock, exactly."""
    return Fraction(gate_cycles) * Fraction(s.mult) / (s.divclk * Fraction(s.div))


def duty_phases(s: MmcmSetting) -> int:
    """How many phases of the output period the input clock's samples visit."""
    return sampling_phases(ideal_count(s, 1), 1)


# The line ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    free: int  # F: rising edges of the free-running output in the gate
    gated: int  # G: rising edges of the gated output
    high: int  # H: oscillator cycles at which the output was sampled high
    locked: int  # L: LOCKED at the end of the gate
    losses: int  # U: falling edges of LOCKED since configuration, saturating at 255
    gate: int  # N: the gate's number since configuration, modulo 256


LINE = re.compile(
    rb"F ([0-9A-F]{8}) G ([0-9A-F]{8}) H ([0-9A-F]{8}) L ([01]) U ([0-9A-F]{2}) N ([0-9A-F]{2}) "
    rb"\*([0-9A-F]{2})"
)
LINE_BYTES = 51


def format_line(r: Reading) -> bytes:
    body = (
        f"F {r.free:08X} G {r.gated:08X} H {r.high:08X} "
        f"L {r.locked:d} U {r.losses:02X} N {r.gate:02X} "
    )
    return f"{body}*{reduce(xor, body.encode()):02X}\n".encode()


def parse_line(line: bytes) -> Reading | None:
    """The reading of one line, or None if it is not a whole line with a good checksum.

    Bytes before the `F` (noise from the pin while the FPGA is being configured) are
    allowed; the line must be whole from there to its end.
    """
    text = line.rstrip(b"\r\n")
    m = LINE.search(text)
    if m is None or m.end() != len(text):
        return None
    if reduce(xor, text[m.start() : m.start(7) - 1]) != int(m[7], 16):
        return None
    return Reading(*(int(m[i], 16) for i in range(1, 7)))


def parse_capture(data: bytes) -> tuple[list[Reading], list[bytes]]:
    """The good lines of a UART capture, and every other non-empty line as it arrived."""
    good, bad = [], []
    for line in data.split(b"\n"):
        reading = parse_line(line)
        if reading is not None:
            good.append(reading)
        elif line.strip(b"\0\r "):
            bad.append(line)
    return good, bad


REPLICA_FALL = "replica_fall"


# Gateware ---------------------------------------------------------------------------------------


def _hex(nibble) -> Signal:
    """The ASCII code of a 4-bit value as an upper-case hexadecimal digit."""
    return Array([ord(c) for c in "0123456789ABCDEF"])[nibble]


class LockMonitor(Module):
    """LOCKED in the system domain, and how many times it has fallen since configuration."""

    def __init__(self) -> None:
        self.locked = Signal()  # asynchronous input
        self.locked_sys = Signal()
        self.losses = Signal(8)
        before = Signal()
        self.specials += MultiReg(self.locked, self.locked_sys, odomain="sys", n=2)
        self.sync += [
            before.eq(self.locked_sys),
            If(before & ~self.locked_sys & (self.losses != 0xFF), self.losses.eq(self.losses + 1)),
        ]


class LineSender(Module):
    """Sends one `format_line` line over a UART each time `strobe` is high for a clock.

    The inputs are taken at the strobe. A strobe while a line is still being sent is ignored
    (a gate must be longer than a line: 51 bytes, 510 bit times).
    """

    def __init__(self, uart_divider: int) -> None:
        self.free = Signal(32)
        self.gated = Signal(32)
        self.high = Signal(32)
        self.locked = Signal()
        self.losses = Signal(8)
        self.strobe = Signal()
        self.tx = Signal()
        self.busy = Signal()

        free, gated, high = Signal(32), Signal(32), Signal(32)
        locked, losses, gate = Signal(), Signal(8), Signal(8)
        checksum = Signal(8)

        def digits(value, count: int) -> list:
            return [_hex(value[4 * i : 4 * i + 4]) for i in reversed(range(count))]

        def text(s: str) -> list:
            return [ord(c) for c in s]

        body = (
            text("F ")
            + digits(free, 8)
            + text(" G ")
            + digits(gated, 8)
            + text(" H ")
            + digits(high, 8)
            + text(" L ")
            + [ord("0") + locked]
            + text(" U ")
            + digits(losses, 2)
            + text(" N ")
            + digits(gate, 2)
            + text(" ")
        )
        line = body + text("*") + digits(checksum, 2) + text("\n")
        assert len(line) == LINE_BYTES
        index = Signal(max=len(line))
        byte = Signal(8)
        self.comb += byte.eq(Array(line)[index])

        self.submodules.uart = uart = _UartTx(uart_divider)
        self.comb += [self.tx.eq(uart.tx), uart.data.eq(byte), uart.we.eq(self.busy & uart.ready)]
        self.sync += [
            If(
                self.busy,
                If(
                    uart.ready,
                    If(index < len(body), checksum.eq(checksum ^ byte)),
                    If(index == len(line) - 1, self.busy.eq(0), gate.eq(gate + 1)).Else(
                        index.eq(index + 1)
                    ),
                ),
            ).Elif(
                self.strobe,
                free.eq(self.free),
                gated.eq(self.gated),
                high.eq(self.high),
                locked.eq(self.locked),
                losses.eq(self.losses),
                checksum.eq(0),
                index.eq(0),
                self.busy.eq(1),
            )
        ]


class ProbeCore(Module):
    """The meters, the lock monitor and the line sender: everything but the clock primitives.

    The parent provides the clock domains `sys`, `free_domain` and `gated_domain`, and drives
    `locked` (the MMCM's LOCKED) and `level` (the replica of the free-running clock).
    """

    def __init__(
        self, gate_cycles: int, uart_divider: int, free_domain: str, gated_domain: str
    ) -> None:
        self.locked = Signal()
        self.level = Signal()
        self.tx = Signal()
        # One gate for the three meters, so that the line holds three counts of one gate.
        self.submodules.gate = gate = Gate(gate_cycles)
        self.submodules.free = free = FreqMeter(gate, free_domain)
        self.submodules.gated = gated = FreqMeter(gate, gated_domain)
        self.submodules.high = high = HighTimeMeter(gate)
        self.submodules.lock = lock = LockMonitor()
        self.submodules.sender = sender = LineSender(uart_divider)
        self.comb += [
            lock.locked.eq(self.locked),
            high.level.eq(self.level),
            # The three meters share the gate: their `valid`s are the same clock.
            sender.strobe.eq(free.valid),
            sender.free.eq(free.count),
            sender.gated.eq(gated.count),
            sender.high.eq(high.count),
            sender.locked.eq(lock.locked_sys),
            sender.losses.eq(lock.losses),
            self.tx.eq(sender.tx),
        ]


class ClockProbe(Module):
    """The probe on the Arty: oscillator, `ProjectClock` at `setting`, `ProbeCore`, the UART.

    LD4 shows LOCKED (synchronised) and LD5 the UART's receive pin, which is otherwise unused.
    """

    def __init__(
        self, platform, setting: MmcmSetting, gate_cycles: int = GATE_CYCLES, baud: int = BAUD
    ) -> None:
        clk_in = platform.request(cb.ARTY.clk_resource)
        self.clock_domains.cd_sys = ClockDomain("sys", reset_less=True)
        self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
        self.specials += Instance("BUFG", name=cb.SYS_BUFG, i_I=clk_in, o_O=self.cd_sys.clk)

        self.submodules.pc = pc = ProjectClock(
            platform, clk_in, setting.clk_in_hz, setting.clock_hz, setting=setting
        )
        self.comb += [self.cd_project.clk.eq(pc.clk), pc.enable.eq(1)]

        # The free-running clock's waveform as a level: high from a rising edge to the next
        # falling edge (module docstring, "Duty cycle").
        rose = Signal()
        fell = Signal()
        self.sync.project_free += rose.eq(~rose)
        self.specials += Instance(
            "FDRE",
            name=REPLICA_FALL,
            p_IS_C_INVERTED=1,
            i_C=ClockSignal("project_free"),
            i_CE=1,
            i_R=0,
            i_D=rose,
            o_Q=fell,
        )

        self.submodules.core = core = ProbeCore(
            gate_cycles, round(setting.clk_in_hz / baud), "project_free", "project"
        )
        serial = platform.request("serial")
        self.comb += [
            core.locked.eq(pc.locked),
            core.level.eq(rose ^ fell),
            serial.tx.eq(core.tx),
            platform.request("user_led", 0).eq(core.lock.locked_sys),
            platform.request("user_led", 1).eq(serial.rx),
        ]
