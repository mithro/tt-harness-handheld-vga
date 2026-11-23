# SPDX-License-Identifier: Apache-2.0
"""Stepping recorder for the compare build.

The recorder steps a Tiny Tapeout project's clock from the system clock domain and sends
the project's `uo_out` after every project clock to the host over a UART.

Frame (all integers little-endian)::

    b"TTHC"   magic
    u8        version (1)
    u32       clocks
    clocks    bytes: byte i is uo_out after the rising edge of project clock i
    u32       CRC-32 (IEEE, as zlib.crc32) of the payload bytes only

One project clock, with system clock edges counted from t0, the edge at which `pclk` rises
(S is `settle`)::

    before t0     clock_index == i, the UART holding register is empty
    t0            pclk 0 -> 1                    (project rising edge i)
    t0+S-1..t0+S  sample_strobe high for one cycle
    t0+S          sampler registers uo           (uo has had S cycles to settle)
    t0+S+1        pclk 1 -> 0; byte queued to the UART; CRC updated
    t0+S+2        clock_index i -> i+1           (pclk low, away from both edges)
    >= t0+2S+2    pclk may rise again, once the UART can accept the next byte

`pclk` is high for S+1 system cycles and low for at least S+1. `clock_index` only changes
while `pclk` is low and is stable from before a rising edge until after the matching
falling edge, so a `ReferenceStimulus` fed from it holds `rst_n` steady across both edges
of every project clock. After the last clock `clock_index` equals `clocks`.
"""

from __future__ import annotations

import struct
from functools import reduce
from operator import xor

from migen import Array, Cat, If, Module, Signal
from migen.genlib.fsm import FSM, NextState, NextValue

from tt_harness.core.sampler import UoSampler

MAGIC = b"TTHC"
VERSION = 1
HEADER_LENGTH = len(MAGIC) + 1 + 4
TRAILER_LENGTH = 4
CRC32_POLY_REFLECTED = 0xEDB88320
MAX_BAUD_ERROR = 0.02


def frame_length(clocks: int) -> int:
    """Number of bytes in a frame carrying `clocks` payload bytes."""
    return HEADER_LENGTH + clocks + TRAILER_LENGTH


def header(clocks: int) -> bytes:
    """The frame header for `clocks` payload bytes."""
    return MAGIC + bytes([VERSION]) + struct.pack("<I", clocks)


def _crc32_byte_terms() -> list[tuple[list[int], list[int]]]:
    """For each bit of the updated CRC, the old-CRC bits and data bits XORed into it.

    This unrolls the reflected, LSB-first bitwise CRC-32 over one byte symbolically, so the
    gateware is one XOR tree per bit rather than eight nested multiplexers.
    """
    state = [frozenset({("c", j)}) for j in range(32)]
    for i in range(8):
        feedback = state[0] ^ {("d", i)}
        state = [*state[1:], frozenset()]
        for j in range(32):
            if (CRC32_POLY_REFLECTED >> j) & 1:
                state[j] = state[j] ^ feedback
    return [
        (
            sorted(n for kind, n in bit if kind == "c"),
            sorted(n for kind, n in bit if kind == "d"),
        )
        for bit in state
    ]


def crc32_update(crc: Signal, data: Signal) -> list:
    """Migen expressions for the 32 bits of `crc` after absorbing the byte `data`."""
    return [
        reduce(xor, [crc[n] for n in crc_bits] + [data[n] for n in data_bits])
        for crc_bits, data_bits in _crc32_byte_terms()
    ]


class _UartTx(Module):
    """8N1 transmitter with a one-byte holding register in front of the shifter.

    `ready` is high while the holding register is empty; a byte written with `we` while
    `ready` is high is always sent, exactly once. `idle` is high once every written byte has
    been sent, including its whole stop bit.
    """

    def __init__(self, divider: int) -> None:
        self.data = Signal(8)
        self.we = Signal()
        self.ready = Signal()
        self.idle = Signal()
        self.tx = Signal(reset=1)

        full = Signal()
        hold = Signal(8)
        busy = Signal()
        shift = Signal(9)  # data bits then the stop bit, sent LSB first
        bits_left = Signal(max=10)
        baud = Signal(max=max(divider, 2))

        self.comb += [
            self.ready.eq(~full),
            self.idle.eq(~full & ~busy),
        ]
        self.sync += [
            If(self.we & ~full, hold.eq(self.data), full.eq(1)),
            If(
                ~busy,
                If(
                    full,
                    full.eq(0),
                    busy.eq(1),
                    self.tx.eq(0),  # start bit
                    shift.eq(Cat(hold, 1)),
                    bits_left.eq(9),
                    baud.eq(divider - 1),
                ),
            ).Else(
                If(
                    baud == 0,
                    baud.eq(divider - 1),
                    If(bits_left == 0, busy.eq(0)).Else(
                        self.tx.eq(shift[0]),
                        shift.eq(shift[1:]),
                        bits_left.eq(bits_left - 1),
                    ),
                ).Else(baud.eq(baud - 1)),
            ),
        ]


class StepRecorder(Module):
    """Steps the project clock and sends one `uo` byte per project clock in a frame.

    Ports: input `uo` (8 bits); outputs `pclk` (project clock, a system-domain register),
    `clock_index` (32 bits, the project clock about to rise or in progress),
    `sample_strobe`, `tx` (UART, 8N1, idle high) and `done` (high once the whole frame,
    including the last stop bit, has been sent). See the module docstring for timing.
    """

    def __init__(self, sys_clk_freq: int, baud: int, clocks: int, settle: int = 2) -> None:
        if not 0 <= clocks < 2**32:
            raise ValueError(f"clocks must be 0..2**32-1, got {clocks}")
        if settle < 1:
            raise ValueError(f"settle must be at least 1 system cycle, got {settle}")
        if sys_clk_freq <= 0 or baud <= 0:
            raise ValueError(f"sys_clk_freq and baud must be positive, got {sys_clk_freq}, {baud}")
        divider = round(sys_clk_freq / baud)
        if divider < 1:
            raise ValueError(f"baud {baud} is above the system clock {sys_clk_freq}")
        actual = sys_clk_freq / divider
        if abs(actual - baud) / baud > MAX_BAUD_ERROR:
            raise ValueError(
                f"baud {baud} from {sys_clk_freq} Hz: nearest is {actual:.1f} "
                f"(divider {divider}), more than {MAX_BAUD_ERROR:.0%} off"
            )

        self.uo = Signal(8)
        self.pclk = Signal()
        self.clock_index = Signal(32)
        self.sample_strobe = Signal()
        self.tx = Signal()
        self.done = Signal()

        self.submodules.sampler = sampler = UoSampler()
        self.submodules.uart = uart = _UartTx(divider)
        self.comb += [
            sampler.uo.eq(self.uo),
            sampler.strobe.eq(self.sample_strobe),
            self.tx.eq(uart.tx),
        ]

        crc = Signal(32, reset=0xFFFFFFFF)
        self.sync += If(sampler.valid, crc.eq(Cat(*crc32_update(crc, sampler.data))))
        crc_out = Signal(32)
        self.comb += crc_out.eq(crc ^ 0xFFFFFFFF)

        header_bytes = Array(header(clocks))
        trailer_bytes = Array(crc_out[8 * n : 8 * n + 8] for n in range(TRAILER_LENGTH))
        index = Signal(max=HEADER_LENGTH)
        timer = Signal(max=max(settle, 2))

        self.submodules.fsm = fsm = FSM(reset_state="HEADER")
        fsm.act(
            "HEADER",
            uart.data.eq(header_bytes[index]),
            If(
                uart.ready,
                uart.we.eq(1),
                If(
                    index == HEADER_LENGTH - 1,
                    NextValue(index, 0),
                    NextState("TRAILER" if clocks == 0 else "RISE"),
                ).Else(NextValue(index, index + 1)),
            ),
        )
        # pclk is low here; rise once the low phase is long enough and the byte this clock
        # will produce is sure to be accepted.
        fsm.act(
            "RISE",
            If(
                (timer == 0) & uart.ready,
                NextValue(self.pclk, 1),
                NextValue(timer, settle - 1),
                NextState("HIGH"),
            ).Elif(timer != 0, NextValue(timer, timer - 1)),
        )
        fsm.act(
            "HIGH",
            If(
                timer == 0,
                self.sample_strobe.eq(1),
                NextState("CAPTURE"),
            ).Else(NextValue(timer, timer - 1)),
        )
        # sampler.valid is high in this state; the holding register is still empty because
        # RISE checked it and nothing else writes to the UART meanwhile.
        fsm.act(
            "CAPTURE",
            uart.data.eq(sampler.data),
            uart.we.eq(sampler.valid),
            NextValue(self.pclk, 0),
            NextState("ADVANCE"),
        )
        fsm.act(
            "ADVANCE",
            NextValue(self.clock_index, self.clock_index + 1),
            NextValue(timer, settle - 1),
            If(self.clock_index == clocks - 1, NextState("TRAILER")).Else(NextState("RISE")),
        )
        fsm.act(
            "TRAILER",
            uart.data.eq(trailer_bytes[index]),
            If(
                uart.ready,
                uart.we.eq(1),
                If(index == TRAILER_LENGTH - 1, NextState("DRAIN")).Else(
                    NextValue(index, index + 1)
                ),
            ),
        )
        fsm.act("DRAIN", If(uart.idle, NextState("DONE")))
        # Migen drops a state with no actions and falls back to the reset state (HEADER),
        # which would send the frame again; the explicit self-loop keeps DONE terminal.
        fsm.act("DONE", NextState("DONE"))
        self.comb += self.done.eq(fsm.ongoing("DONE"))
