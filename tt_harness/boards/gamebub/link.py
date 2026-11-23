# SPDX-License-Identifier: Apache-2.0
"""Game Bub MCU link: the 0xF1 registers, the two mailboxes and the host command
handshakes, all answered in RTL (docs/gamebub-mcu-protocol.md sections 4-8).

Everything the MCU addresses lives in the ``spi`` domain and is updated on the very SPI
edge that writes it, so commands complete before the MCU's first status read and no
system-clock activity (or CPU) is needed for the load sequence to finish.

Clock-domain crossing (``spi`` and ``sys`` must be declared asynchronous clock groups):

- sys to spi (``buttons``, IRQ pending, ``reset``): two flip-flops in ``spi``. They only
  advance on SPI edges, but every read's data phase starts 32 edges after chip select
  falls, so the value read is never more than one transaction old.
- spi to sys, single-bit levels (``running``, ``focus``, control bits, IRQ enable bits):
  ``MultiReg``. Each changes only at an SPI write and then holds while the SPI clock is
  stopped.
- spi to sys, events (IRQ pending clear, forwarded commands): a toggle in ``spi``
  crossed with ``MultiReg``; the data that goes with it (clear mask, command words) is
  held in ``spi`` registers that cannot change again for at least one further
  transaction (well over 0.5 us), and sys samples it within four sys cycles of the toggle.
"""

from __future__ import annotations

from migen import Array, Case, Cat, If, Module, Signal
from migen.genlib.cdc import MultiReg

from .qspi import QspiSlave

# Bit i of ``buttons`` (and of STATUS_BUTTON) is BUTTONS[i]; 1 = pressed (section 4).
BUTTONS = ("select", "start", "r", "l", "right", "left", "down", "up", "y", "x", "b", "a")

HOST_MAILBOX = 0xF000_0000
CORE_MAILBOX = 0xF000_1000
MAILBOX_WORDS = 8

INFO_FRAMEWORK_VER = 0xF100_0000
INFO_SYSCLK_HZ = 0xF100_0004
INFO_VIDEO_DIM = 0xF100_0100
INFO_VIDEO_DEPTH = 0xF100_0104
INFO_INPUT_CAPS = 0xF100_0108
CTRL_IRQ_ENABLE = 0xF100_1000
CTRL_IRQ_PENDING = 0xF100_1004
CTRL_DOCK = 0xF100_100C
CTRL_FOCUS = 0xF100_1010
CTRL_VIBRATE = 0xF100_1014
CTRL_VIDEO_FILTER = 0xF100_1018
CTRL_CMD_HOST = 0xF100_1100
CTRL_CMD_CORE = 0xF100_1104
STATUS_BUTTON = 0xF100_2000
STATUS_CART_SWITCH = 0xF100_2004

GET_STATUS = 0x0000
CORE_RUN = 0x0100
CORE_HALT = 0x0101
SETUP_COMPLETE = 0x0102
NOTIFY_FOCUS = 0x0200

STATUS_SETUP = 2
STATUS_CORE_HALT = 3
STATUS_CORE_RUN = 4

IRQ_SOURCES = 5
IRQ_BUTTON = 1 << 1

FORWARDED_WORDS = 6  # command and five arguments


def _word(addr: int) -> int:
    return addr >> 2


class McuLink(Module):
    """Register file and handshake engine on a ``QspiSlave``'s bus port.

    System-domain ports:

    - ``buttons`` (in, 12): bit order ``BUTTONS``, 1 = pressed;
    - ``reset`` (in): while high the link answers no command (so the MCU times out),
      holds the status at SETUP and ``running`` low;
    - ``irq_n`` (out): low while an enabled interrupt is pending; drive the pin open-drain;
    - ``running`` (out): the MCU has sent CORE_RUN (and no CORE_HALT since);
    - ``focus`` (out): last NOTIFY_FOCUS argument;
    - ``dock`` (out, 2), ``vibrate``, ``video_filter`` (out): last values written;
    - CPU port: ``cmd_valid`` (out) rises when a command the RTL does not handle arrives
      (it has already been answered with an error), ``cmd_words`` (6 x 32) hold that
      command and its five arguments, ``cmd_ack`` (in) clears ``cmd_valid``.
    """

    def __init__(
        self,
        qspi: QspiSlave,
        *,
        sys_clk_hz: int = 50_000_000,
        input_caps: int = 0,
        framework_version: int = 0,
        video_dim: int = 0,
        video_depth: int = 0,
    ):
        self.buttons = Signal(len(BUTTONS))
        self.reset = Signal()
        self.irq_n = Signal(reset=1)
        self.running = Signal()
        self.focus = Signal()
        self.dock = Signal(2)
        self.vibrate = Signal()
        self.video_filter = Signal()
        self.cmd_valid = Signal()
        self.cmd_ack = Signal()
        self.cmd_words = [Signal(32, name=f"cmd_word{i}") for i in range(FORWARDED_WORDS)]

        # -- sys to spi -----------------------------------------------------------------

        def to_spi(sig: Signal, reset: int = 0) -> Signal:
            stages = [Signal(len(sig), reset=reset, attr={"async_reg", "mr_ff"}) for _ in range(2)]
            self.sync.spi += [stages[0].eq(sig), stages[1].eq(stages[0])]
            return stages[1]

        def to_sys(sig, out: Signal) -> None:
            self.specials += MultiReg(sig, out, "sys")

        buttons_spi = to_spi(self.buttons)
        reset_spi = to_spi(self.reset, reset=1)  # assume reset until the first edges
        pending = Signal(IRQ_SOURCES)  # sys
        pending_spi = to_spi(pending)

        # -- spi-domain state -----------------------------------------------------------

        status = Signal(3, reset=STATUS_SETUP)
        running_spi = Signal()
        focus_spi = Signal()
        req = Signal()
        cmd_done = Signal()
        cmd_error = Signal()
        irq_enable = Signal(IRQ_SOURCES)
        dock = Signal(2)
        focus_reg = Signal()
        vibrate = Signal()
        video_filter = Signal()
        clr_mask = Signal(IRQ_SOURCES)
        clr_toggle = Signal()
        fwd_toggle = Signal()
        fwd_words = [Signal(32, name=f"fwd_word{i}") for i in range(FORWARDED_WORDS)]
        host_mb = [Signal(32, name=f"host_mb{i}") for i in range(MAILBOX_WORDS)]
        core_mb = [Signal(32, name=f"core_mb{i}") for i in range(MAILBOX_WORDS)]

        word = qspi.adr[2:]
        lane = qspi.adr[:2]
        data = qspi.dat_w

        def lane_write(reg: Signal):
            return Case(lane, {k: reg[8 * k : 8 * k + 8].eq(data) for k in range(4)})

        def low_bits(reg: Signal):
            return If(lane == 0, reg.eq(data[: len(reg)]))

        host_cmd = host_mb[0]
        handshake = If(
            lane == 0,
            req.eq(data[3]),
            cmd_done.eq(0),
            cmd_error.eq(0),
            If(
                data[3] & ~reset_spi,
                cmd_done.eq(1),
                Case(
                    host_cmd,
                    {
                        GET_STATUS: host_mb[0].eq(status),
                        SETUP_COMPLETE: If(status == STATUS_SETUP, status.eq(STATUS_CORE_HALT)),
                        CORE_RUN: If(
                            status == STATUS_CORE_HALT,
                            status.eq(STATUS_CORE_RUN),
                            running_spi.eq(1),
                        ),
                        CORE_HALT: If(
                            status == STATUS_CORE_RUN,
                            status.eq(STATUS_CORE_HALT),
                            running_spi.eq(0),
                        ),
                        NOTIFY_FOCUS: focus_spi.eq(host_mb[1][0]),
                        "default": [
                            cmd_error.eq(1),
                            fwd_toggle.eq(~fwd_toggle),
                            *[f.eq(m) for f, m in zip(fwd_words, host_mb)],
                        ],
                    },
                ),
            ),
        )

        writes = {
            _word(CTRL_IRQ_ENABLE): low_bits(irq_enable),
            _word(CTRL_IRQ_PENDING): If(
                lane == 0, clr_mask.eq(data[:IRQ_SOURCES]), clr_toggle.eq(~clr_toggle)
            ),
            _word(CTRL_DOCK): low_bits(dock),
            _word(CTRL_FOCUS): low_bits(focus_reg),
            _word(CTRL_VIBRATE): low_bits(vibrate),
            _word(CTRL_VIDEO_FILTER): low_bits(video_filter),
            _word(CTRL_CMD_HOST): handshake,
        }
        for i in range(MAILBOX_WORDS):
            writes[_word(HOST_MAILBOX) + i] = lane_write(host_mb[i])
            writes[_word(CORE_MAILBOX) + i] = lane_write(core_mb[i])

        self.sync.spi += [
            If(qspi.we, Case(word, writes)),
            If(reset_spi, status.eq(STATUS_SETUP), running_spi.eq(0)),
        ]

        reads = {
            _word(INFO_FRAMEWORK_VER): framework_version,
            _word(INFO_SYSCLK_HZ): sys_clk_hz,
            _word(INFO_VIDEO_DIM): video_dim,
            _word(INFO_VIDEO_DEPTH): video_depth,
            _word(INFO_INPUT_CAPS): input_caps,
            _word(CTRL_IRQ_ENABLE): irq_enable,
            _word(CTRL_IRQ_PENDING): pending_spi,
            _word(CTRL_DOCK): dock,
            _word(CTRL_FOCUS): focus_reg,
            _word(CTRL_VIBRATE): vibrate,
            _word(CTRL_VIDEO_FILTER): video_filter,
            _word(CTRL_CMD_HOST): Cat(cmd_error, cmd_done, 0, req),
            _word(STATUS_BUTTON): buttons_spi,
        }
        for i in range(MAILBOX_WORDS):
            reads[_word(HOST_MAILBOX) + i] = host_mb[i]
            reads[_word(CORE_MAILBOX) + i] = core_mb[i]

        rd_word = Signal(32)
        self.comb += [
            Case(word, {k: rd_word.eq(v) for k, v in reads.items()} | {"default": rd_word.eq(0)}),
            qspi.dat_r.eq(Array(rd_word[8 * k : 8 * k + 8] for k in range(4))[lane]),
        ]

        # -- spi to sys -----------------------------------------------------------------

        enable_sys = Signal(IRQ_SOURCES)
        running_sys = Signal()
        clr_toggle_sys = Signal()
        fwd_toggle_sys = Signal()
        for src, dst in [
            (irq_enable, enable_sys),
            (running_spi, running_sys),
            (focus_spi, self.focus),
            (dock, self.dock),
            (vibrate, self.vibrate),
            (video_filter, self.video_filter),
            (clr_toggle, clr_toggle_sys),
            (fwd_toggle, fwd_toggle_sys),
        ]:
            to_sys(src, dst)
        self.comb += [
            self.running.eq(running_sys & ~self.reset),
            *[c.eq(f) for c, f in zip(self.cmd_words, fwd_words)],
        ]

        # -- sys-domain interrupt and CPU-port logic --------------------------------------

        buttons_prev = Signal(len(BUTTONS))
        clr_prev = Signal()
        fwd_prev = Signal()
        clear = Signal(IRQ_SOURCES)
        self.comb += If(clr_toggle_sys != clr_prev, clear.eq(clr_mask))
        self.sync += [
            buttons_prev.eq(self.buttons),
            clr_prev.eq(clr_toggle_sys),
            fwd_prev.eq(fwd_toggle_sys),
            pending.eq((pending & ~clear) | Cat(0, self.buttons != buttons_prev, 0, 0, 0)),
            self.irq_n.eq((pending & enable_sys) == 0),
            If(fwd_toggle_sys != fwd_prev, self.cmd_valid.eq(1)).Elif(
                self.cmd_ack, self.cmd_valid.eq(0)
            ),
        ]
