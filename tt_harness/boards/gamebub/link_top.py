# SPDX-License-Identifier: Apache-2.0
"""Synthesis top for the Game Bub MCU link: ``QspiSlave`` and ``McuLink`` on the board's pins.

This is the part of the real Game Bub build that the MCU link occupies, with just enough
around it to be synthesised and timed as it would be used:

- ``sys`` is the 50 MHz oscillator (``clk50``, P17) through a BUFG.
- ``spi`` is the MCU's SPI clock (T14) straight from its pad through a BUFG, and ``spi_n``
  is its inverse. T14 is ``IO_L14P_T2_SRCC_14``, a clock-capable pin.
- ``dq[3:0]`` are bidirectional through explicit IOBUFs, tristated by ``~dq_oe``.
- The twelve buttons are active low with pull-ups: two flip-flops in ``sys`` and an inversion
  give ``McuLink.buttons``.
- ``McuLink.reset`` is a power-on counter in ``sys`` (the core is held in reset for 15 system
  cycles after configuration), so that the reset crossing is a real one.
- Everything the link puts out is observable, so none of it is optimised away: ``irq`` on
  ``mcu_irq`` (N15), ``running``, ``cmd_overrun``, ``cmd_valid`` and ``focus`` on the four test
  points, and the CPU port (the six forwarded command words and the remaining outputs)
  folded by XOR into an 8-bit register that is shown on the four PMOD pins (low nibble XOR
  high nibble). A forwarded command is acknowledged one system cycle after it arrives.
"""

from __future__ import annotations

from pathlib import Path

from migen import Cat, ClockDomain, If, Instance, Module, Signal

from .link import BUTTONS, FORWARDED_WORDS, McuLink
from .platform import Platform
from .qspi import QspiSlave

NAME = "link_top"
SYS_BUFG = "sys_bufg"
SPI_BUFG = "spi_bufg"
SYS_HZ = 50_000_000
SPI_WRITE_HZ = 40_000_000  # the firmware's register-write clock; its reads are at 10 MHz (16 cap)


class LinkTop(Module):
    def __init__(self, platform: Platform):
        clk50 = platform.request("clk50")
        qspi_pads = platform.request("mcu_qspi")
        irq_pad = platform.request("mcu_irq")
        button_pads = platform.request("buttons")
        pmod = platform.request("pmod")
        tp = platform.request("tp")

        self.clock_domains.cd_sys = ClockDomain("sys", reset_less=True)
        self.clock_domains.cd_spi = ClockDomain("spi", reset_less=True)
        self.clock_domains.cd_spi_n = ClockDomain("spi_n", reset_less=True)
        self.specials += [
            Instance("BUFG", name=SYS_BUFG, i_I=clk50, o_O=self.cd_sys.clk),
            Instance("BUFG", name=SPI_BUFG, i_I=qspi_pads.clk, o_O=self.cd_spi.clk),
        ]
        self.comb += self.cd_spi_n.clk.eq(~self.cd_spi.clk)

        self.submodules.qspi = qspi = QspiSlave(clock_domains=False)
        self.submodules.link = link = McuLink(qspi, sys_clk_hz=SYS_HZ)

        # Pads.
        self.comb += qspi.cs_n.eq(qspi_pads.cs_n)
        for i in range(4):
            self.specials += Instance(
                "IOBUF",
                i_I=qspi.dq_o[i],
                o_O=qspi.dq_i[i],
                i_T=~qspi.dq_oe,
                io_IO=qspi_pads.dq[i],
            )
        self.comb += irq_pad.eq(link.irq)

        # Buttons: active low, two flip-flops in sys, then inverted.
        raw = Cat(*[getattr(button_pads, name) for name in BUTTONS])
        stage0 = Signal(len(BUTTONS), reset=0xFFF, attr={"async_reg"})
        stage1 = Signal(len(BUTTONS), reset=0xFFF, attr={"async_reg"})
        self.sync.sys += [stage0.eq(raw), stage1.eq(stage0)]
        self.comb += link.buttons.eq(~stage1)

        # Power-on reset: held for 15 system cycles.
        por = Signal(4)
        # The reset is a flip-flop output: a crossing into spi must not start from logic.
        reset = Signal(reset=1)
        self.sync.sys += [If(por != 15, por.eq(por + 1)), reset.eq(por != 15)]
        self.comb += link.reset.eq(reset)

        # CPU port: acknowledge a forwarded command one cycle after it arrives, and fold
        # everything the port and the control outputs carry into a register.
        ack = Signal()
        self.sync.sys += ack.eq(link.cmd_valid & ~ack)
        self.comb += link.cmd_ack.eq(ack)
        folded = Signal(8)
        bits = Cat(
            *link.cmd_words,
            link.cmd_overrun,
            link.focus,
            link.dock,
            link.vibrate,
            link.video_filter,
        )
        fold = Signal(8)
        self.comb += fold.eq(xor_fold(bits, 8))
        self.sync.sys += If(link.cmd_valid & ~ack, folded.eq(folded ^ fold))
        self.comb += pmod.eq(folded[:4] ^ folded[4:])
        self.comb += tp.eq(Cat(link.running, link.cmd_overrun, link.cmd_valid, link.focus))


def xor_fold(bits, width: int):
    """XOR of ``bits`` taken ``width`` at a time (a parity fold into ``width`` bits)."""
    n = len(bits)
    acc = bits[0:width]
    for lo in range(width, n, width):
        chunk = bits[lo : lo + width]
        acc = acc ^ chunk if len(chunk) == width else acc ^ Cat(chunk, Signal(width - len(chunk)))
    return acc


assert FORWARDED_WORDS == 6


def top_verilog(platform: Platform, path: Path) -> tuple[Path, LinkTop]:
    """Write the top as Verilog module ``link_top`` to ``path``; return it and the top."""
    top = LinkTop(platform)
    output = platform.get_verilog(top, name=NAME)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Migen's ports carry no net type: keep the file out of any `default_nettype none`.
    output.write(str(path))
    text = path.read_text()
    path.write_text("`default_nettype wire\n" + text)
    return path, top


def pin_xdc(platform: Platform) -> str:
    """XDC placing every top-level port: LOC, IOSTANDARD and pull-ups, from the platform."""
    lines = []
    for sig, pins, others, _resource in platform.constraint_manager.get_sig_constraints():
        name = sig.name
        standard = next((o.name for o in others if type(o).__name__ == "IOStandard"), None)
        misc = [o.misc for o in others if type(o).__name__ == "Misc"]
        for i, pin in enumerate(pins):
            port = f"{name}[{i}]" if len(sig) > 1 else name
            lines.append(f"set_property LOC {pin} [get_ports {{{port}}}]")
            if standard:
                lines.append(f"set_property IOSTANDARD {standard} [get_ports {{{port}}}]")
            for m in misc:
                key, _, value = (m if isinstance(m, str) else str(m)).partition("=")
                lines.append(f"set_property {key} {value} [get_ports {{{port}}}]")
    return "\n".join(lines) + "\n"
