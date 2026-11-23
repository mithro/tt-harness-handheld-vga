# SPDX-License-Identifier: Apache-2.0
"""Simulation harness for the Game Bub QSPI slave and MCU link.

The SPI clock is gated, and Migen's simulator only produces free-running clocks. So the
harness clocks the ``spi`` and ``spi_n`` domains from one fast ``tick`` clock and inserts
clock enables: ``spi_rise`` marks the ticks that are SPI rising edges and ``spi_fall``
those that are falling edges (see ``firmware_model``). The ``sys`` domain is an ordinary
50 MHz clock, so the two sides are genuinely asynchronous in the simulation.

The QSPI slave's one asynchronous element, the chip-select flag (an ``AsyncSetFlag``),
is simulated on the tick clock: set on any tick with ``cs_n`` high, cleared on an SPI
rising edge. The model keeps chip select high for at least one tick between
transactions.
"""

from __future__ import annotations

from migen import CEInserter, ClockDomainsRenamer, If, Module, Signal, passive, run_simulation

from tt_harness.boards.gamebub.link import McuLink
from tt_harness.boards.gamebub.qspi import AsyncSetFlag, QspiSlave

from .firmware_model import TICK_HZ

# Simulator time unit is 1/8 ns: tick 160 MHz = 50 units, sys 50 MHz = 160 units.
SYS_HZ = 50_000_000
CLOCKS = {"tick": 8_000_000_000 // TICK_HZ, "sys": 8_000_000_000 // SYS_HZ}


class _Harness(Module):
    def __init__(self, with_link: bool, link_kwargs: dict):
        self.submodules.qspi = qspi = QspiSlave(clock_domains=False)
        self.cs_n = qspi.cs_n
        self.clk = qspi.clk
        self.dq_i = qspi.dq_i
        self.dq_o = qspi.dq_o
        self.dq_oe = qspi.dq_oe
        if with_link:
            self.submodules.link = McuLink(qspi, sys_clk_hz=SYS_HZ, **link_kwargs)
            self.fpga_irq = self.link.irq


def make_dut(with_link: bool = True, **link_kwargs):
    """The device under test with simulation-only ``spi_rise``/``spi_fall`` strobes."""
    dut = CEInserter(["spi", "spi_n"])(_Harness(with_link, link_kwargs))
    dut.spi_rise = dut.ce_spi
    dut.spi_fall = dut.ce_spi_n
    return ClockDomainsRenamer({"spi": "tick", "spi_n": "tick"})(dut)


def _sim_async_set_flag(edge: Signal):
    class _Impl(Module):
        def __init__(self, flag: AsyncSetFlag):
            self.sync.tick += If(flag.async_set, flag.q.eq(1)).Elif(edge, flag.q.eq(0))

    class _Lowering:
        @staticmethod
        def lower(flag):
            return _Impl(flag)

    return _Lowering


def run(dut, tick_generators, sys_generators=(), vcd_name: str | None = None):
    """Run the simulation until every non-passive generator has finished."""
    run_simulation(
        dut,
        {"tick": list(tick_generators), "sys": list(sys_generators)},
        clocks=CLOCKS,
        special_overrides={AsyncSetFlag: _sim_async_set_flag(dut.spi_rise)},
        vcd_name=vcd_name,
    )


@passive
def bus_memory(dut, mem: dict[int, int], writes: list[tuple[int, int]], reads: list[int]):
    """A byte memory on the QSPI slave's bus master port, for testing the slave alone.

    Serves ``dat_r`` from ``mem`` (0 where unset) and performs a write on every SPI
    rising edge with ``we`` set, as an SPI-domain register file would.
    """
    q = dut.qspi
    while True:
        edge = yield dut.spi_rise
        adr = yield q.adr
        if edge and (yield q.we):
            data = yield q.dat_w
            mem[adr] = data
            writes.append((adr, data))
        if edge and (yield q.re):
            reads.append(adr)
        yield q.dat_r.eq(mem.get(adr, 0))
        yield


def wait_ticks(n: int):
    for _ in range(n):
        yield
