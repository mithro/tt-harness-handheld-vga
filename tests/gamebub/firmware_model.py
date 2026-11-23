# SPDX-License-Identifier: Apache-2.0
"""Model of the stock Game Bub MCU firmware's side of the QSPI link.

Written from ``docs/gamebub-mcu-protocol.md`` (the firmware's observable behaviour), not
from the RTL, so that it checks the RTL independently. Section numbers below refer to
that document.

The model is a set of Migen simulator coroutines that drive the pads of a device under
test bit by bit. The device must expose:

- ``cs_n``, ``clk``, ``dq_i`` (4), ``dq_o`` (4), ``dq_oe``: the QSPI pads;
- ``fpga_irq``: the FPGA's interrupt pin (active high, into the board's n-FET);
- ``spi_rise``, ``spi_fall``: simulation-only strobes. The SPI clock is gated (it stops
  between transactions), which Migen's simulator cannot generate directly, so the device's
  SPI-clocked logic runs from a fast free-running ``tick`` clock with these as clock
  enables: a tick with ``spi_rise`` set *is* an SPI rising edge, one with ``spi_fall`` set
  is a falling edge, and all other ticks are no clock edge at all.

The coroutines must run in the ``tick`` clock domain at ``TICK_HZ``.

Time. Firmware durations (timeouts and sleeps) are multiplied by ``time_scale``
(default ``TIME_SCALE`` = 1e-3) so that a 100 ms timeout is 100 us of simulated time. SPI
clock rates are never scaled: every transaction takes its real time. Scaling therefore
only ever makes the firmware's deadlines tighter relative to the link, never looser.
"""

from __future__ import annotations

TICK_HZ = 160_000_000
TIME_SCALE = 1e-3

# Section 1: the firmware's SPI devices, fastest first, and its rate caps.
DRIVER_RATES_HZ = (40_000_000, 20_000_000, 10_000_000, 4_000_000, 2_000_000)
MAX_SPI_READ_CLOCK_HZ = 16_000_000
DEFAULT_SYSTEM_CLOCK_HZ = 8 * 1024 * 1024

# Section 8.
SETUP_TIMEOUT_S = 0.100
NOTIFY_TIMEOUT_S = 0.010
POLL_INTERVAL_S = 0.005

# Section 2.
DUMMY_CLOCKS = 16
WORD_8, WORD_16, WORD_32 = 0, 1, 2
LINE_QUAD = 2

# Section 3 and 4.
HOST_MAILBOX = 0xF000_0000
CORE_MAILBOX = 0xF000_1000
INFO_FRAMEWORK_VER = 0xF100_0000
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
OVERLAY = 0xF200_0000

# Section 5.2.
GET_STATUS = 0x0000
CORE_RUN = 0x0100
CORE_HALT = 0x0101
SETUP_COMPLETE = 0x0102
NOTIFY_FOCUS = 0x0200
STATUS_SETUP = 2
STATUS_CORE_HALT = 3

# Section 6.
IRQ_BUTTON = 1 << 1
IRQ_CORE_REQUEST = 1 << 4

# Section 4, STATUS_BUTTON: bit number of each button, 1 = pressed.
BUTTON_BITS = {
    "a": 11,
    "b": 10,
    "x": 9,
    "y": 8,
    "up": 7,
    "down": 6,
    "left": 5,
    "right": 4,
    "l": 3,
    "r": 2,
    "start": 1,
    "select": 0,
}

# Section 8, abort table.
_MESSAGES = {
    "CoreReadyTimeout": "Timed out waiting for core to be ready",
    "CommandTimeout": "Core command {code:04X} timed out",
    "CommandFailed": "Core command {code:04X} failed",
}


class FirmwareGaveUp(Exception):
    """The firmware abandoned loading the core (section 8, abort)."""

    def __init__(self, core_error: str, code: int | None = None):
        self.core_error = core_error
        self.code = code
        self.message = _MESSAGES[core_error].format(code=code or 0)
        super().__init__(f"{core_error}: {self.message}")


class FirmwareTimeout(FirmwareGaveUp):
    """Gave up because the core did not answer in time (whatever error the firmware reports)."""


class FirmwareError(FirmwareGaveUp):
    """Gave up because the core answered with the error bit set."""


class BusContention(AssertionError):
    """The device drove ``dq`` while the MCU was driving it. A model check, not firmware."""


class _CommandTimeout(Exception):
    def __init__(self, code: int):
        self.code = code


class _CommandFailed(Exception):
    def __init__(self, code: int):
        self.code = code


def decode_buttons(value: int) -> set[str]:
    """Names of the buttons pressed in a STATUS_BUTTON value (section 4)."""
    return {name for name, bit in BUTTON_BITS.items() if value & (1 << bit)}


class FirmwareModel:
    """The MCU side of the link. All public methods are simulator coroutines."""

    def __init__(
        self,
        dut,
        *,
        time_scale: float = TIME_SCALE,
        driver_rates_hz: tuple[int, ...] = DRIVER_RATES_HZ,
        cs_high_periods: int = 1,
        undriven: int = 0xF,
        idle_lines: int = 0b1110,
        docked: bool = False,
        use_builtin_screen: bool = False,
        overlay_span_pixels: int = 16,
        op_pause_periods: int = 3,
    ):
        self.dut = dut
        self.time_scale = time_scale
        self.driver_rates_hz = driver_rates_hz
        self.cs_high_periods = cs_high_periods
        self.undriven = undriven  # what the MCU reads from an undriven bus (Unverified U5)
        self.idle_lines = idle_lines  # dq[3:1] during the command byte (Unverified U4)
        self.docked = docked
        self.use_builtin_screen = use_builtin_screen
        self.overlay_span_pixels = overlay_span_pixels
        self.op_pause_periods = op_pause_periods  # clock pause between operations (U6)
        self.system_clock_hz = DEFAULT_SYSTEM_CLOCK_HZ

        self.now = 0  # ticks
        self.command_bytes: list[int] = []  # every command byte sent, in order
        self.interrupts = 0  # the firmware's copy of the enable mask (section 6)
        self._master_drives = False
        self.transactions: list[tuple[str, int, int, int]] = []  # (kind, addr, bytes, Hz)
        self.ignored_errors: list[FirmwareGaveUp] = []
        self.last_cmd_status: int | None = None
        self.input_caps: int | None = None
        self.input_state: set[str] | None = None
        self.running = False

    # -- time --------------------------------------------------------------------------

    def ticks(self, seconds: float) -> int:
        """Firmware duration in simulation ticks, after time scaling."""
        return round(seconds * self.time_scale * TICK_HZ)

    def _tick(self, n: int = 1):
        dut = self.dut
        for _ in range(n):
            if self._master_drives and (yield dut.dq_oe):
                raise BusContention(f"device drives dq while the MCU does, tick {self.now}")
            yield
            self.now += 1

    def sleep(self, seconds: float):
        """``std::thread::sleep`` of a firmware duration (scaled)."""
        yield from self._tick(self.ticks(seconds))

    # -- bit level (section 2) ---------------------------------------------------------

    @staticmethod
    def period_ticks(rate_hz: int) -> int:
        period, rem = divmod(TICK_HZ, rate_hz)
        if rem or period % 2:
            raise ValueError(f"{rate_hz} Hz is not an even divisor of the {TICK_HZ} Hz tick")
        return period

    def _clock(self, half: int, drive: int | None):
        """One SPI clock in mode 0, starting just after a falling edge (clock low).

        ``drive`` is the value the MCU puts on dq[3:0] for this clock, or None if it
        does not drive. Returns the value the MCU samples at the rising edge.
        """
        dut = self.dut
        if drive is not None:
            yield dut.dq_i.eq(drive)
        yield from self._tick(half - 1)
        # Sample in the last tick before the rising edge: what the MCU latches is what
        # the device has been presenting, not anything the edge itself causes.
        oe = yield dut.dq_oe
        value = (yield dut.dq_o) if oe else self.undriven
        yield dut.spi_rise.eq(1)
        yield from self._tick(1)  # this tick is the rising edge
        yield dut.spi_rise.eq(0)
        yield dut.clk.eq(1)
        yield from self._tick(half - 1)
        yield dut.spi_fall.eq(1)
        yield from self._tick(1)  # falling edge
        yield dut.spi_fall.eq(0)
        yield dut.clk.eq(0)
        return value

    def raw_transaction(
        self,
        rate_hz: int,
        command: int,
        address: int,
        data: bytes | None = None,
        read_len: int = 0,
        stop_after_clocks: int | None = None,
        samples: list[int] | None = None,
    ):
        """One chip-select period: command, address, then write data or dummy + read.

        The firmware issues these phases as separate driver operations under one chip
        select (section 2), so the clock is held low for ``op_pause_periods`` SPI periods
        between operations, with chip select still low. ``stop_after_clocks`` releases
        chip select early. ``samples`` collects what the MCU sampled on every clock.
        Returns the bytes read.
        """
        dut = self.dut
        half = self.period_ticks(rate_hz) // 2
        self.command_bytes.append(command)
        reading = command & 1
        ops: list[list[int | None]] = [
            [((command >> b) & 1) | self.idle_lines for b in range(7, -1, -1)],
            [(address >> (4 * n)) & 0xF for n in range(7, -1, -1)],
        ]
        if reading:
            ops += [[None] * DUMMY_CLOCKS, [None] * (2 * read_len)]
        else:
            ops.append([n for byte in data or b"" for n in (byte >> 4, byte & 0xF)])

        yield dut.cs_n.eq(0)
        self._master_drives = True
        got: list[int] = []
        budget = stop_after_clocks
        for index, op in enumerate(ops):
            if index and self.op_pause_periods:
                yield from self._tick(self.op_pause_periods * 2 * half)
            for drive in op:
                if budget is not None and len(got) >= budget:
                    break
                if drive is None:
                    self._master_drives = False
                got.append((yield from self._clock(half, drive)))
        yield dut.cs_n.eq(1)
        self._master_drives = False
        yield from self._tick(self.cs_high_periods * 2 * half)

        if samples is not None:
            samples.extend(got)
        first = 8 + 8 + DUMMY_CLOCKS
        nibbles = got[first:] if reading else []
        return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles) - 1, 2))

    # -- transactions (sections 1, 2) --------------------------------------------------

    def driver_rate(self, max_clock_hz: int | None) -> int:
        """The firmware's choice of SPI device for a transaction (section 1)."""
        for rate in self.driver_rates_hz:
            if max_clock_hz is None or rate <= max_clock_hz:
                return rate
        raise RuntimeError(f"no SPI device for max clock {max_clock_hz}")

    @staticmethod
    def command_byte(read: bool, word_size: int, byte_swap: bool, auto_increment: bool) -> int:
        return (
            int(read)
            | (word_size << 1)
            | (int(byte_swap) << 3)
            | (int(auto_increment) << 4)
            | (LINE_QUAD << 5)
        )

    def write(
        self,
        addr: int,
        data: bytes,
        word_size: int,
        *,
        max_clock_hz: int | None = None,
        byte_swap: bool = True,
        auto_increment: bool = True,
    ):
        rate = self.driver_rate(max_clock_hz)
        self.transactions.append(("w", addr, len(data), rate))
        cmd = self.command_byte(False, word_size, byte_swap, auto_increment)
        yield from self.raw_transaction(rate, cmd, addr, data=bytes(data))

    def read(
        self,
        addr: int,
        n: int,
        word_size: int,
        *,
        max_clock_hz: int | None = MAX_SPI_READ_CLOCK_HZ,
        byte_swap: bool = True,
        auto_increment: bool = True,
    ):
        rate = self.driver_rate(max_clock_hz)
        self.transactions.append(("r", addr, n, rate))
        cmd = self.command_byte(True, word_size, byte_swap, auto_increment)
        return (yield from self.raw_transaction(rate, cmd, addr, read_len=n))

    def write_u32(self, addr: int, value: int):
        yield from self.write(addr, value.to_bytes(4, "little"), WORD_32)

    def read_u32(self, addr: int):
        data = yield from self.read(addr, 4, WORD_32)
        return int.from_bytes(data, "little")

    def write_overlay(self, offset: int, data: bytes):
        """Section 7: 16-bit words at the overlay's rate cap."""
        max_clock = self.system_clock_hz * 16 // (4 * 2)
        yield from self.write(OVERLAY | offset, data, WORD_16, max_clock_hz=max_clock)

    def set_interrupts(self, mask: int):
        """Section 6: the firmware writes its whole copy of the mask."""
        self.interrupts = mask
        yield from self.write_u32(CTRL_IRQ_ENABLE, self.interrupts)

    # -- commands (section 5.1) --------------------------------------------------------

    def _run_core_command(self, request: list[int], n_response: int, timeout_s: float):
        for i, word in enumerate(request):
            yield from self.write_u32(HOST_MAILBOX + 4 * i, word)
        yield from self.write_u32(CTRL_CMD_HOST, 0b1000)
        start = self.now
        limit = self.ticks(timeout_s)
        while True:
            if self.now - start > limit:
                raise _CommandTimeout(request[0])  # CTRL_CMD_HOST is left as it is
            status = yield from self.read_u32(CTRL_CMD_HOST)
            if status & 0b0011:
                break
            yield from self.sleep(POLL_INTERVAL_S)
        self.last_cmd_status = status
        response = []
        for i in range(n_response):
            response.append((yield from self.read_u32(HOST_MAILBOX + 4 * i)))
        yield from self.write_u32(CTRL_CMD_HOST, 0)
        if status & 0b0001:
            raise _CommandFailed(request[0])
        return response

    def command(self, request: list[int], n_response: int = 0, timeout_s: float = NOTIFY_TIMEOUT_S):
        """Run one host command; raise as the firmware's callers see it."""
        try:
            return (yield from self._run_core_command(request, n_response, timeout_s))
        except _CommandTimeout as e:
            raise FirmwareTimeout("CommandTimeout", e.code) from None
        except _CommandFailed as e:
            raise FirmwareError("CommandFailed", e.code) from None

    def _command_ignoring_errors(self, request: list[int]):
        try:
            yield from self.command(request)
        except FirmwareGaveUp as e:
            self.ignored_errors.append(e)

    def poll_core_status(self, expected: int, timeout_s: float):
        start = self.now
        limit = self.ticks(timeout_s)
        while True:
            if self.now - start > limit:
                raise FirmwareTimeout("CoreReadyTimeout")
            try:
                (status,) = yield from self._run_core_command([GET_STATUS], 1, timeout_s)
            except _CommandTimeout:
                raise FirmwareTimeout("CommandFailed", GET_STATUS) from None
            except _CommandFailed:
                raise FirmwareError("CommandFailed", GET_STATUS) from None
            if status == expected:
                return
            yield from self.sleep(POLL_INTERVAL_S)

    # -- higher level ------------------------------------------------------------------

    def get_input_state(self):
        value = yield from self.read_u32(STATUS_BUTTON)
        self.input_state = decode_buttons(value)
        return self.input_state

    def focus_changed(self, has_focus: bool):
        yield from self.write_u32(CTRL_FOCUS, int(has_focus))
        yield from self.write_u32(CTRL_VIDEO_FILTER, 0 if has_focus else 1)
        yield from self._command_ignoring_errors([NOTIFY_FOCUS, int(has_focus)])

    def reset_core(self):
        yield from self._command_ignoring_errors([CORE_HALT])
        yield from self._command_ignoring_errors([CORE_RUN])

    def mcu_int_n(self):
        """Level of the MCU's shared, active-low interrupt line (section 6).

        On rev 2-4 the FPGA pin ``fpga_irq`` drives the gate of an n-MOSFET whose drain is
        that line, so a high FPGA pin pulls the line low. The firmware's interrupt is
        level-triggered on low.
        """
        return 0 if (yield self.dut.fpga_irq) else 1

    def service_interrupt(self):
        """Section 6: what the firmware does when irq_n is low. Returns the pending mask."""
        pending = yield from self.read_u32(CTRL_IRQ_PENDING)
        if pending:
            yield from self.write_u32(CTRL_IRQ_PENDING, pending)
        if pending & IRQ_BUTTON:
            yield from self.get_input_state()
        return pending

    def boot(self):
        """Section 8, from the end of configuration to "core running".

        Models a third-party core with no ``files.json`` or ``settings.json``. Raises
        ``FirmwareTimeout`` or ``FirmwareError`` where the firmware would abort, after
        performing the abort's link traffic.
        """
        # Steps 1-3: the tail of program_fpga.
        dock = int(self.docked) | (int(self.docked and self.use_builtin_screen) << 1)
        yield from self.write_u32(CTRL_DOCK, dock)
        yield from self.set_interrupts(self.interrupts | IRQ_BUTTON)
        yield from self.get_input_state()
        try:
            # Step 5.
            yield from self.poll_core_status(STATUS_SETUP, SETUP_TIMEOUT_S)
            # Step 6: the UI redraws a span of the overlay (asynchronous in reality).
            if self.overlay_span_pixels:
                pixels = bytes(range(256)) * (2 * self.overlay_span_pixels // 256 + 1)
                yield from self.write_overlay(0, pixels[: 2 * self.overlay_span_pixels])
            # Step 7. With no gamepads attached no slot is ever written (section 4).
            self.input_caps = yield from self.read_u32(INFO_INPUT_CAPS)
            # Steps 8-9: no files, no settings.
            # Step 10.
            yield from self.set_interrupts(self.interrupts | IRQ_CORE_REQUEST)
            # Step 11.
            yield from self.command([SETUP_COMPLETE])
            # Step 12.
            yield from self.poll_core_status(STATUS_CORE_HALT, SETUP_TIMEOUT_S)
        except FirmwareGaveUp:
            # Abort: not yet Running, so no CORE_HALT; drop CoreRequest from the mask.
            yield from self.set_interrupts(self.interrupts & ~IRQ_CORE_REQUEST)
            raise
        # Steps 13-16.
        self.running = True
        yield from self.write_u32(CTRL_VIBRATE, 1)
        yield from self._command_ignoring_errors([CORE_RUN])
        yield from self.focus_changed(True)
