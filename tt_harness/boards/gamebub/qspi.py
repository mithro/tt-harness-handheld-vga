# SPDX-License-Identifier: Apache-2.0
"""QSPI slave for the Game Bub MCU link, clocked by the SPI clock itself.

Implements the framing of docs/gamebub-mcu-protocol.md section 2: a single-line command
byte, a 32-bit quad address, then quad write data, or 16 dummy clocks and quad read data.
The slave turns the word stream into byte accesses on a simple bus.

Clocking. All state is in clock domain ``spi`` (rising SPI clock edges), except the
output nibble register in ``spi_n`` (falling edges, mode 0: the MCU samples on the rising
edge, so the slave changes ``dq`` after the falling edge). The SPI clock stops between
transactions, so nothing here waits for an edge after the last one: a byte write is
committed on the rising edge that delivers its last nibble.

Chip select. The only asynchronous element is ``AsyncSetFlag``, a flip-flop set while
``cs_n`` is high and cleared by the first rising edge after it falls. While it is set the
state machine treats the coming edge as the first edge of a new transaction, and every
strobe and ``dq_oe`` is masked, so a transaction cut short at any point leaves no trace.
``dq_oe`` is also gated by ``cs_n`` directly, so the slave releases the bus as soon as chip
select rises, without a clock edge.

Bus master port (domain ``spi``):

- ``adr``: byte address, valid throughout the cycle before each strobed edge;
- ``we``, ``dat_w``: on a rising edge with ``we`` high, write ``dat_w`` to ``adr``.
  ``dat_w``'s low nibble comes straight from the pads, so the slave of this port must
  commit on that same edge;
- ``re``, ``dat_r``: on a rising edge with ``re`` high, the slave samples ``dat_r`` for
  ``adr``; ``dat_r`` must therefore be combinational from ``adr``. Reads have no side
  effects: one byte past the end of a read may be fetched.

Byte order (section 2.2): with the command's byte-swap bit set, the bytes of each word go
to ascending addresses (little-endian memory); without it, descending. With
auto-increment each word advances the address by the word size, otherwise every word
goes to the same address. A command whose line-width field is not quad is ignored until
chip select rises.
"""

from __future__ import annotations

from migen import (
    SPECIAL_INPUT,
    SPECIAL_OUTPUT,
    Case,
    Cat,
    ClockDomain,
    ClockSignal,
    If,
    Instance,
    Module,
    Mux,
    Signal,
    wrap,
)
from migen.fhdl.specials import Special

DUMMY_CLOCKS = 16
LINE_QUAD = 2

PHASE_CMD, PHASE_ADDR, PHASE_WDATA, PHASE_DUMMY, PHASE_RDATA, PHASE_IGNORE = range(6)


class AsyncSetFlag(Special):
    """One flip-flop: ``q`` is 1 while ``async_set`` is high (asynchronously), and is
    cleared by a rising edge of clock domain ``cd`` with ``async_set`` low.

    Lowers to a Xilinx ``FDPE``. Simulations must supply their own lowering.
    """

    def __init__(self, cd: str, async_set):
        Special.__init__(self)
        self.clk = ClockSignal(cd)
        self.async_set = wrap(async_set)
        self.q = Signal(reset=1)

    def iter_expressions(self):
        yield self, "clk", SPECIAL_INPUT
        yield self, "async_set", SPECIAL_INPUT
        yield self, "q", SPECIAL_OUTPUT

    @staticmethod
    def lower(dr):
        return _XilinxAsyncSetFlag(dr)


class _XilinxAsyncSetFlag(Module):
    def __init__(self, flag: AsyncSetFlag):
        self.specials += Instance(
            "FDPE",
            p_INIT=1,
            i_C=flag.clk,
            i_CE=1,
            i_PRE=flag.async_set,
            i_D=0,
            o_Q=flag.q,
        )


class QspiSlave(Module):
    """QSPI slave. With ``clock_domains`` it creates ``spi`` and ``spi_n`` from ``clk``;
    without, the enclosing design (or a simulation) provides them."""

    def __init__(self, clock_domains: bool = True):
        # Pads.
        self.clk = Signal()
        self.cs_n = Signal(reset=1)
        self.dq_i = Signal(4)
        self.dq_o = Signal(4)
        self.dq_oe = Signal()
        # Bus master port.
        self.adr = Signal(32)
        self.dat_w = Signal(8)
        self.we = Signal()
        self.dat_r = Signal(8)
        self.re = Signal()

        if clock_domains:
            self.clock_domains.cd_spi = ClockDomain(reset_less=True)
            self.clock_domains.cd_spi_n = ClockDomain(reset_less=True)
            self.comb += [
                self.cd_spi.clk.eq(self.clk),
                self.cd_spi_n.clk.eq(~self.clk),
            ]

        flag = AsyncSetFlag("spi", self.cs_n)
        self.specials += flag
        first = flag.q  # the next rising edge starts a transaction

        phase = Signal(3)
        count = Signal(4)
        cmd = Signal(8)
        shift = Signal(28)
        word_base = Signal(32)
        byte_idx = Signal(3)
        nib = Signal()  # 0: next nibble is a high nibble
        hi = Signal(4)
        obyte = Signal(8)

        # The state as this edge sees it: a first edge always starts in the command phase.
        cur_phase = Signal(3)
        cur_count = Signal(4)
        self.comb += [
            cur_phase.eq(Mux(first, PHASE_CMD, phase)),
            cur_count.eq(Mux(first, 0, count)),
        ]

        is_read = cmd[0]
        word_size = cmd[1:3]
        byte_swap = cmd[3]
        auto_increment = cmd[4]
        last_byte = Signal(3)  # word size in bytes, minus one
        self.comb += Case(
            word_size,
            {0: last_byte.eq(0), 1: last_byte.eq(1), 2: last_byte.eq(3), 3: last_byte.eq(7)},
        )
        self.comb += self.adr.eq(word_base + Mux(byte_swap, byte_idx, last_byte - byte_idx))

        cmd_next = Cat(self.dq_i[0], cmd[:7])
        addr_next = Cat(self.dq_i, shift)

        self.comb += [
            self.we.eq((cur_phase == PHASE_WDATA) & nib),
            self.dat_w.eq(Cat(self.dq_i, hi)),
            self.re.eq(
                ((cur_phase == PHASE_DUMMY) & (cur_count == DUMMY_CLOCKS - 1))
                | ((cur_phase == PHASE_RDATA) & nib)
            ),
            # Drive from the second dummy clock: half a clock after the MCU has released.
            self.dq_oe.eq(
                ~self.cs_n
                & ~first
                & (((phase == PHASE_DUMMY) & (count != 0)) | (phase == PHASE_RDATA))
            ),
        ]

        advance = If(
            byte_idx == last_byte,
            byte_idx.eq(0),
            If(auto_increment, word_base.eq(word_base + last_byte + 1)),
        ).Else(byte_idx.eq(byte_idx + 1))

        self.sync.spi += [
            count.eq(cur_count + 1),
            Case(
                cur_phase,
                {
                    PHASE_CMD: [
                        cmd.eq(cmd_next),
                        phase.eq(PHASE_CMD),
                        If(
                            cur_count == 7,
                            count.eq(0),
                            If(cmd_next[5:7] == LINE_QUAD, phase.eq(PHASE_ADDR)).Else(
                                phase.eq(PHASE_IGNORE)
                            ),
                        ),
                    ],
                    PHASE_ADDR: [
                        shift.eq(addr_next[:28]),
                        If(
                            cur_count == 7,
                            count.eq(0),
                            word_base.eq(addr_next),
                            byte_idx.eq(0),
                            nib.eq(0),
                            If(is_read, phase.eq(PHASE_DUMMY)).Else(phase.eq(PHASE_WDATA)),
                        ),
                    ],
                    PHASE_WDATA: [
                        count.eq(0),
                        nib.eq(~nib),
                        If(~nib, hi.eq(self.dq_i)).Else(advance),
                    ],
                    PHASE_DUMMY: [
                        If(
                            cur_count == DUMMY_CLOCKS - 1,
                            obyte.eq(self.dat_r),
                            nib.eq(0),
                            phase.eq(PHASE_RDATA),
                            advance,
                        ),
                    ],
                    PHASE_RDATA: [
                        count.eq(0),
                        nib.eq(~nib),
                        If(nib, obyte.eq(self.dat_r), advance),
                    ],
                    "default": count.eq(0),
                },
            ),
        ]

        # Falling edge: present the high nibble after a byte is loaded, then the low one.
        self.sync.spi_n += self.dq_o.eq(Mux(nib, obyte[:4], obyte[4:]))
