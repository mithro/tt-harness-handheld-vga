# SPDX-License-Identifier: Apache-2.0
"""The demo build's SoC: a VexRiscv that runs `demo.json`, and the harness blocks as CSRs.

Two layers:

- `HarnessCore` (a plain Migen module, no CSRs): the `ProjectSlot` it is given, the at-speed
  check attached to it, VGA timing, capture and framebuffer, the seven-segment sampler and,
  optionally, the audio filter. It owns every crossing between `sys` and `project` that the
  slot and the check do not: three `MultiBitCrossing` words (below) and the framebuffer, a
  true dual-clock block RAM written in `project` and read in `sys`. Its `sys` side is plain
  levels (`sys_inputs()` lists them), so its crossings can be checked structurally
  (`tests/test_soc.py`, with `tests/cdc_structure.py`).
- `HarnessSoC` (a LiteX `SoCCore`): VexRiscv (`standard`), UART at 115200 baud, timer,
  integrated ROM (the BIOS), SRAM and main RAM, and the CSR modules `slot`, `gamepad`, `vga`,
  `sevenseg`, `atspeed` and `info`, at fixed CSR locations (`CSR_LOCATIONS`) so their
  addresses do not move between boards. Each register is described where it is made; the
  generated map with the firmware rules is `docs/registers.md`.
- The test configuration, `HarnessSoC(..., cpu_type=None)`: the same CSR modules on a bus
  with no CPU, for register tests that drive the bus from a simulated master. It omits the
  CPU, the ROM, the SRAM, the main RAM and the UART (whose FIFOs do not run in Migen's
  simulator, and which nothing would use); the CSR map is otherwise the same.

Crossings added here (the slot's and the check's are in their modules):

- `vga_settings_x`, `sys` -> `project`, 31 bits: the capture settings `{divider (3),
  h_offset (12), v_offset (11), h_step (2), v_step (2), blank (1)}`. All six arrive together.
- `vga_timing_x`, `project` -> `sys`, 66 bits: `VgaTiming`'s outputs, one publication whole
  (`TIMING_FIELDS`). Its value before the first delivery is `VgaTiming`'s reset value
  (`no_signal` 1), so with the project clock stopped from power-up the registers read "no
  signal". With the clock stopped later they keep the last delivered word.
- `sevenseg_x`, `project` -> `sys`, 64 bits: the eight duty bytes of one window.

The capture is blanked in `project` by `VgaTiming.no_signal` as well as by the `blank`
register, so a design that stops producing sync is blanked without the firmware (the
"no signal" picture, never a frozen frame) as long as the project clock runs.

`demo.json` is validated at build time with the configurator's messages (an invalid file is a
`DemoError`), must hold exactly one project, and only its `clock_hz` is used, to check that
the slot was built for it; the bytes go into the firmware image unchanged through the C
header `demo_json_header` writes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from fractions import Fraction
from typing import ClassVar

from litex.gen import LiteXModule
from litex.soc.integration.soc_core import SoCCore
from litex.soc.interconnect.csr import AutoCSR, CSRField, CSRStatus, CSRStorage
from migen import Cat, ClockDomainsRenamer, Module, Signal

from tt_harness.core.atspeed import AtSpeedCheck
from tt_harness.core.audio import AudioFilter
from tt_harness.core.cdc import MultiBitCrossing
from tt_harness.core.sevenseg import SevenSegSampler
from tt_harness.core.slot import ProjectSlot
from tt_harness.core.vga import PIXEL_BITS, Framebuffer, VgaCapture, VgaTiming
from tt_harness.demo.validate import DemoError, Entry, parse

# The Pmods a project may declare (the manifest's `pmods`); bit n of `info_pmods` is PMODS[n].
PMODS: tuple[str, ...] = (
    "tiny-vga",
    "tt-audio",
    "gamepad",
    "spi",
    "qspi-pmod",
    "uart-pmod",
    "uart-usb",
)
CSR_MODULES: tuple[str, ...] = ("slot", "gamepad", "vga", "sevenseg", "atspeed", "info")
CSR_LOCATIONS = {name: 16 + i for i, name in enumerate(CSR_MODULES)}
UART_BAUD = 115200
ROM_SIZE = 0x8000  # the BIOS is about 23 KiB; the ROM is this size whatever it holds
SRAM_SIZE = 0x2000
MAIN_RAM_SIZE = 0x10000  # the firmware's data: the parser's 32 KiB of JSON tokens and the rest
SEVENSEG_FRAME_HZ = 60  # the seven-segment window is about one display frame
# (name, bits) of the timing word, low bits first.
TIMING_FIELDS: tuple[tuple[str, int], ...] = (
    ("clocks_per_line", 12),
    ("lines_per_frame", 11),
    ("hsync_width", 12),
    ("vsync_lines", 11),
    ("hsync_positive", 1),
    ("vsync_positive", 1),
    ("locked", 1),
    ("no_signal", 1),
    ("frames", 16),
)
SETTINGS_FIELDS: tuple[tuple[str, int], ...] = (
    ("divider", 3),
    ("h_offset", 12),
    ("v_offset", 11),
    ("h_step", 2),
    ("v_step", 2),
    ("blank", 1),
)
SEGMENTS = ("a", "b", "c", "d", "e", "f", "g", "dp")


def shuttle_of(project_name: str) -> str:
    """The shuttle of a project name `<shuttle>/<macro>` (the manifest's `Project.id`)."""
    parts = project_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"project_name must be <shuttle>/<macro>, got {project_name!r}")
    return parts[0]


def demo_entry(demo_json: bytes, shuttle: str) -> Entry:
    """The one project of a `demo.json` for this chip; `DemoError` if it is not usable."""
    try:
        text = demo_json.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DemoError([f"not UTF-8: {e}"]) from e
    demo = parse(text, shuttle)
    if len(demo.projects) != 1:
        raise DemoError(
            [f"this build runs exactly one project; demo.json has {len(demo.projects)}"]
        )
    return demo.projects[0]


def achieved_clock(slot: ProjectSlot) -> tuple[int, int]:
    """The project clock the slot produces, exactly, as (numerator, denominator) in Hz.

    From the MMCM setting: `clk_in_hz * CLKFBOUT_MULT_F / (DIVCLK_DIVIDE * CLKOUT0_DIVIDE_F)`,
    both dividers multiples of 1/8. In a simulation build (`slot.sim_clock`) the bench drives
    exactly the requested clock (`clock_hz / 1`); a stopped clock (`clock_hz` 0, no MMCM)
    is `0 / 1`.
    """
    if slot.sim_clock:
        return slot.clock_hz, 1
    if slot.clock is None:
        assert slot.clock_hz == 0
        return 0, 1
    s = slot.clock.setting
    f = Fraction(s.clk_in_hz) * Fraction(s.mult) / (s.divclk * Fraction(s.div))
    return f.numerator, f.denominator


def pmod_flags(pmods: Sequence[str]) -> int:
    flags = 0
    for name in pmods:
        if name not in PMODS:
            raise ValueError(f"unknown Pmod {name!r}; known: {', '.join(PMODS)}")
        flags |= 1 << PMODS.index(name)
    return flags


def sevenseg_window_log2_for(clock_hz: int) -> int:
    """log2 of the seven-segment window: the power of two of clocks nearest one display frame
    (2**19 at 25.175 MHz, 20.8 ms)."""
    if clock_hz <= 0:
        return 19  # no clock: no window ever ends; any valid size
    return max(1, round(math.log2(clock_hz / SEVENSEG_FRAME_HZ)))


def demo_json_header(demo_json: bytes) -> str:
    """A C header holding `demo.json`'s bytes unchanged: `demo_json[]` and `DEMO_JSON_LEN`."""
    rows = []
    for i in range(0, len(demo_json), 12):
        rows.append("    " + ", ".join(f"0x{b:02x}" for b in demo_json[i : i + 12]) + ",")
    return (
        "/* SPDX-License-Identifier: Apache-2.0 */\n"
        "/* Generated by tt_harness/soc/core.py (demo_json_header): the build's demo.json. */\n"
        "#ifndef TT_HARNESS_DEMO_JSON_H\n"
        "#define TT_HARNESS_DEMO_JSON_H\n"
        "\n"
        f"#define DEMO_JSON_LEN {len(demo_json)}\n"
        "\n"
        "static const unsigned char demo_json[] = {\n" + "\n".join(rows) + "\n};\n"
        "\n"
        "#endif /* TT_HARNESS_DEMO_JSON_H */\n"
    )


def _fields(signal, layout):
    """{name: slice of `signal`} for a (name, bits) layout, low bits first."""
    out, at = {}, 0
    for name, bits in layout:
        out[name] = signal[at : at + bits]
        at += bits
    return out


class HarnessCore(Module):
    """The harness blocks around a `ProjectSlot`, with a `sys` side of plain levels.

    `sys` inputs: the slot's controls (`slot.ena`, `slot.rst_n`, ...), the at-speed check's
    `start`, `abort` and `clocks`, the capture settings `settings[name]` and the framebuffer
    read address `fb.rd_addr`. `sys` outputs: `slot.locked`, `slot.clock_running`, the check's
    `busy`, `done` and `crc`, `timing[name]` (`TIMING_FIELDS`), `duty` (8 bytes, segment a in
    the low byte) and `fb.rd_data`. `project` outputs: `audio_sample` and `audio_valid`
    (constant 0 without audio).
    """

    def __init__(
        self,
        slot: ProjectSlot,
        fb_size: tuple[int, int],
        *,
        reference_ui_in: int,
        with_audio: bool,
        sevenseg_window_log2: int,
    ) -> None:
        width, height = fb_size
        self.submodules.slot = slot
        self.submodules.atspeed = check = AtSpeedCheck(reference_ui_in)
        slot.attach(check)

        # VGA: measure, capture, buffer.
        self.submodules.timing = timing = ClockDomainsRenamer("project")(VgaTiming())
        self.submodules.capture = capture = ClockDomainsRenamer("project")(
            VgaCapture(width, height)
        )
        self.submodules.fb = fb = Framebuffer(width, height, read_domain="sys")
        self.comb += [
            timing.uo_out.eq(slot.uo_out),
            capture.uo_out.eq(slot.uo_out),
            capture.hsync_positive.eq(timing.hsync_positive),
            capture.vsync_positive.eq(timing.vsync_positive),
            fb.we.eq(capture.we),
            fb.addr.eq(capture.addr),
            fb.data.eq(capture.data),
        ]

        self.settings = {name: Signal(bits, name=f"vga_{name}") for name, bits in SETTINGS_FIELDS}
        n_settings = sum(bits for _, bits in SETTINGS_FIELDS)
        self.submodules.vga_settings_x = sx = MultiBitCrossing(n_settings)
        self.comb += sx.src.eq(Cat(*self.settings.values()))
        got = _fields(sx.dst, SETTINGS_FIELDS)
        self.comb += [
            capture.divider.eq(got["divider"]),
            capture.h_offset.eq(got["h_offset"]),
            capture.v_offset.eq(got["v_offset"]),
            capture.h_step.eq(got["h_step"]),
            capture.v_step.eq(got["v_step"]),
            capture.blank.eq(got["blank"] | timing.no_signal),
        ]

        n_timing = sum(bits for _, bits in TIMING_FIELDS)
        reset = 0
        at = 0
        for name, bits in TIMING_FIELDS:
            reset |= getattr(timing, name).reset.value << at
            at += bits
        self.submodules.vga_timing_x = tx = MultiBitCrossing(
            n_timing, reset=reset, idomain="project", odomain="sys"
        )
        self.comb += tx.src.eq(Cat(*(getattr(timing, name) for name, _ in TIMING_FIELDS)))
        self.timing = _fields(tx.dst, TIMING_FIELDS)

        # Seven-segment duty.
        self.submodules.sevenseg = seg = ClockDomainsRenamer("project")(
            SevenSegSampler(sevenseg_window_log2)
        )
        self.submodules.sevenseg_x = segx = MultiBitCrossing(64, idomain="project", odomain="sys")
        self.comb += [
            seg.uo_out.eq(slot.uo_out),
            segx.src.eq(Cat(*(seg.duty[i] for i in range(8)))),
        ]
        self.duty = segx.dst

        # Audio, in `project`, for a board layer that plays it.
        self.audio = None
        self.audio_sample = Signal((16, True))
        self.audio_valid = Signal()
        if with_audio:
            if slot.clock_hz < 8 * 48_000:  # including 0: a stopped clock makes no sound
                raise ValueError(
                    f"audio needs a project clock of at least {8 * 48_000} Hz, "
                    f"not {slot.clock_hz}: build without audio"
                )
            self.submodules.audio = audio = ClockDomainsRenamer("project")(
                AudioFilter(slot.clock_hz)
            )
            self.comb += [
                audio.uio_out.eq(slot.uio_out),
                audio.uio_oe.eq(slot.uio_oe),
                self.audio_sample.eq(audio.sample),
                self.audio_valid.eq(audio.valid),
            ]
        else:
            self.comb += [self.audio_sample.eq(0), self.audio_valid.eq(0)]

    def sys_inputs(self) -> list[Signal]:
        s, c = self.slot, self.atspeed
        return [
            s.ena,
            s.rst_n,
            s.ui_in,
            s.gamepad_enable,
            s.gamepad_buttons,
            s.clock_enable,
            c.start,
            c.abort,
            c.clocks,
            *self.settings.values(),
            self.fb.rd_addr,
        ]


# CSR modules ---------------------------------------------------------------------------------


class SlotCSR(LiteXModule, AutoCSR):
    def __init__(self, slot: ProjectSlot) -> None:
        self._ena = CSRStorage(description="The project's `ena` (1: enabled). A level.")
        self._rst_n = CSRStorage(
            description=(
                "The project's `rst_n` (0: in reset; 1: released). A level. Reset is asserted"
                " at once, with or without a project clock, and released on a project clock"
                " edge 6 to 9 project clocks after the write, carrying the `ena`, `ui_in` and"
                " `gamepad_enable` written before it."
            )
        )
        self._ui_in = CSRStorage(
            8,
            description=(
                "The project's `ui_in`. A level. Bits 6:4 come from the gamepad emulator"
                " instead while `gamepad_enable` is set."
            ),
        )
        self._clock_enable = CSRStorage(
            description="1: the project clock runs (the BUFGCE enable); 0: it is stopped."
        )
        self._status = CSRStatus(
            fields=[
                CSRField("locked", description="The MMCM is locked."),
                CSRField(
                    "clock_running",
                    description=(
                        "The project clock has been seen running in the last 512 system"
                        " clocks (reads 1 for a clock of 1 MHz and up; flickers below about"
                        " 0.39 MHz)."
                    ),
                ),
            ],
            description="Project clock status.",
        )
        self.comb += [
            slot.ena.eq(self._ena.storage),
            slot.rst_n.eq(self._rst_n.storage),
            slot.ui_in.eq(self._ui_in.storage),
            slot.clock_enable.eq(self._clock_enable.storage),
            self._status.fields.locked.eq(slot.locked),
            self._status.fields.clock_running.eq(slot.clock_running),
        ]


class GamepadCSR(LiteXModule, AutoCSR):
    def __init__(self, slot: ProjectSlot) -> None:
        self._enable = CSRStorage(
            description=(
                "1: the gamepad emulator drives `ui_in[6:4]` (latch, clock, data). A level,"
                " delivered with the slot's control word, so it is in place at the release."
            )
        )
        self._buttons = CSRStorage(
            24,
            reset=0xFFFFFF,
            description=(
                "The 24 bits each gamepad frame sends, bit n sent n-th"
                " (`tt_harness.demo.validate.gamepad_bits`): bits 23:12 the first controller,"
                " 11:0 the second, a held button 1, an empty socket all ones. A level, not"
                " ordered against the release; the reset value is no controller."
            ),
        )
        self.comb += [
            slot.gamepad_enable.eq(self._enable.storage),
            slot.gamepad_buttons.eq(self._buttons.storage),
        ]


class VgaCSR(LiteXModule, AutoCSR):
    def __init__(self, core: HarnessCore, with_readback: bool) -> None:
        t = core.timing
        self._clocks_per_line = CSRStatus(
            12, description="Measured project clocks from one hsync leading edge to the next."
        )
        self._lines_per_frame = CSRStatus(11, description="Measured lines per frame.")
        self._hsync_width = CSRStatus(12, description="Measured project clocks hsync is active.")
        self._vsync_lines = CSRStatus(11, description="Measured lines vsync is active.")
        self._status = CSRStatus(
            fields=[
                CSRField("hsync_positive", description="hsync is active high."),
                CSRField("vsync_positive", description="vsync is active high."),
                CSRField("locked", description="The last two publications were the same timing."),
                CSRField(
                    "no_signal",
                    description=(
                        "No vsync within 2**23 project clocks, or none yet: the measurements"
                        " are stale and the capture is blanked."
                    ),
                ),
            ],
            description="Sync polarity and lock of the measured timing.",
        )
        self._frames = CSRStatus(
            16,
            description=(
                "Publications of the timing so far, wrapping. Every timing register holds"
                " the same publication: read `frames` before and after the others."
            ),
        )
        self.comb += [
            self._clocks_per_line.status.eq(t["clocks_per_line"]),
            self._lines_per_frame.status.eq(t["lines_per_frame"]),
            self._hsync_width.status.eq(t["hsync_width"]),
            self._vsync_lines.status.eq(t["vsync_lines"]),
            self._status.fields.hsync_positive.eq(t["hsync_positive"]),
            self._status.fields.vsync_positive.eq(t["vsync_positive"]),
            self._status.fields.locked.eq(t["locked"]),
            self._status.fields.no_signal.eq(t["no_signal"]),
            self._frames.status.eq(t["frames"]),
        ]
        s = core.settings
        self._divider = CSRStorage(3, description="Capture: project clocks per pixel (0 is 1).")
        self._h_offset = CSRStorage(
            12,
            description=(
                "Capture: project clocks from the hsync leading edge to the first pixel (0 is 1)."
            ),
        )
        self._v_offset = CSRStorage(
            11, description="Capture: lines from the vsync leading edge to the first row (0 is 1)."
        )
        self._h_step = CSRStorage(
            2, description="Capture: store every n-th pixel of a row (0 is 1)."
        )
        self._v_step = CSRStorage(2, description="Capture: store every n-th row (0 is 1).")
        self._blank = CSRStorage(
            description="Capture: write the picture as black; setting it clears the buffer."
        )
        self.comb += [
            s["divider"].eq(self._divider.storage),
            s["h_offset"].eq(self._h_offset.storage),
            s["v_offset"].eq(self._v_offset.storage),
            s["h_step"].eq(self._h_step.storage),
            s["v_step"].eq(self._v_step.storage),
            s["blank"].eq(self._blank.storage),
        ]
        if with_readback:
            n = core.fb.width * core.fb.height
            self._readback_addr = CSRStorage(
                len(core.fb.rd_addr),
                description=(f"Framebuffer pixel to read: y * {core.fb.width} + x, 0 to {n - 1}."),
            )
            self._readback_data = CSRStatus(
                fields=[
                    CSRField("b", size=2, description="Blue, 0 to 3."),
                    CSRField("g", size=2, description="Green, 0 to 3."),
                    CSRField("r", size=2, description="Red, 0 to 3."),
                ],
                description=(
                    "The pixel at `readback_addr`, valid one system clock after the address"
                    " is written. No tearing protection: a pixel being captured may be new."
                ),
            )
            assert PIXEL_BITS == 6
            self.comb += [
                core.fb.rd_addr.eq(self._readback_addr.storage),
                # A CSRStatus with fields is driven through its fields.
                self._readback_data.fields.b.eq(core.fb.rd_data[0:2]),
                self._readback_data.fields.g.eq(core.fb.rd_data[2:4]),
                self._readback_data.fields.r.eq(core.fb.rd_data[4:6]),
            ]


class SevenSegCSR(LiteXModule, AutoCSR):
    def __init__(self, core: HarnessCore, window_log2: int) -> None:
        self._duty = CSRStatus(
            fields=[
                CSRField(
                    seg,
                    size=8,
                    description=(
                        f"Duty of `uo_out[{i}]` (segment {seg}) over the last window of"
                        f" 2**{window_log2} project clocks: 0 never lit to 255 always lit."
                    ),
                )
                for i, seg in enumerate(SEGMENTS)
            ],
            description="The virtual seven-segment display's brightness, one byte a segment.",
        )
        self.comb += [
            getattr(self._duty.fields, seg).eq(core.duty[8 * i : 8 * i + 8])
            for i, seg in enumerate(SEGMENTS)
        ]


class AtSpeedCSR(LiteXModule, AutoCSR):
    def __init__(self, check: AtSpeedCheck) -> None:
        self._clocks = CSRStorage(
            32, description="N: the number of project clocks the check CRCs, from reset."
        )
        self._control = CSRStorage(
            fields=[
                CSRField(
                    "start",
                    pulse=True,
                    description="Start a check of `clocks`; cancels any earlier one.",
                ),
                CSRField(
                    "abort",
                    pulse=True,
                    description="Abort the check; wins over a start in the same write.",
                ),
            ],
            description="At-speed check commands.",
        )
        self._status = CSRStatus(
            fields=[
                CSRField("busy", description="A start was given and has not finished."),
                CSRField("done", description="The most recent start finished: `crc` is valid."),
            ],
            description="At-speed check state.",
        )
        self._crc = CSRStatus(
            32,
            description=(
                "zlib CRC-32 of the first N bytes of `uo_out` from the first reset clock,"
                " valid while `done`."
            ),
        )
        self.comb += [
            check.clocks.eq(self._clocks.storage),
            check.start.eq(self._control.fields.start),
            check.abort.eq(self._control.fields.abort),
            self._status.fields.busy.eq(check.busy),
            self._status.fields.done.eq(check.done),
            self._crc.status.eq(check.crc),
        ]


class InfoCSR(LiteXModule, AutoCSR):
    def __init__(
        self, clock_hz: int, achieved: tuple[int, int], fb_size: tuple[int, int], pmods: int
    ) -> None:
        num, den = achieved
        self._clock_hz = CSRStatus(
            32, reset=clock_hz, description="`clock_hz` of demo.json: the requested clock, Hz."
        )
        self._clock_hz_num = CSRStatus(
            64,
            reset=num,
            description="Numerator of the achieved project clock in Hz (0 when stopped).",
        )
        self._clock_hz_den = CSRStatus(
            32, reset=den, description="Denominator of the achieved project clock in Hz."
        )
        self._fb_width = CSRStatus(16, reset=fb_size[0], description="Framebuffer width.")
        self._fb_height = CSRStatus(16, reset=fb_size[1], description="Framebuffer height.")
        self._pmods = CSRStatus(
            fields=[
                CSRField(
                    name.replace("-", "_"),
                    reset=(pmods >> i) & 1,
                    description=f"The project declares the `{name}` Pmod.",
                )
                for i, name in enumerate(PMODS)
            ],
            description="The project's declared Pmods.",
        )
        for csr in (
            self._clock_hz,
            self._clock_hz_num,
            self._clock_hz_den,
            self._fb_width,
            self._fb_height,
        ):
            self.comb += csr.status.eq(csr.status.reset)
        self.comb += [
            getattr(self._pmods.fields, name.replace("-", "_")).eq((pmods >> i) & 1)
            for i, name in enumerate(PMODS)
        ]


# The SoC -------------------------------------------------------------------------------------


class HarnessSoC(SoCCore):
    """The demo build's SoC; see the module docstring.

    `slot` is built by the caller (board clock, `clock_hz` from `demo.json`); `platform` must
    provide `serial`, and the caller adds the CRG (`sys` domain). `project_name` is the
    manifest id `<shuttle>/<macro>`. `pmods`: the project's declared Pmods (`PMODS`).
    `reference_ui_in`: the reference stimulus' fixed `ui_in`, for the at-speed check.
    `cpu_type=None` is the test configuration (module docstring): no CPU, ROM, SRAM, main
    RAM or UART, only the CSR bus, which a register test drives from a simulated master.
    `sevenseg_window_log2` overrides the window chosen from the clock.
    """

    csr_map: ClassVar[dict[str, int]] = dict(CSR_LOCATIONS)

    def __init__(
        self,
        platform,
        sys_clk_freq: int,
        slot: ProjectSlot,
        fb_size: tuple[int, int],
        demo_json: bytes,
        project_name: str,
        with_audio: bool,
        with_readback: bool,
        *,
        pmods: Sequence[str],
        reference_ui_in: int,
        cpu_type: str | None = "vexriscv",
        sevenseg_window_log2: int | None = None,
        main_ram_size: int = MAIN_RAM_SIZE,
    ) -> None:
        shuttle = shuttle_of(project_name)
        self.demo_entry = entry = demo_entry(demo_json, shuttle)
        self.demo_json = bytes(demo_json)
        self.project_name = project_name
        if slot.clock_hz != entry.clock_hz:
            raise ValueError(
                f"demo.json sets clock_hz {entry.clock_hz} but the slot's clock is "
                f"{slot.clock_hz} Hz"
            )
        flags = pmod_flags(pmods)
        if sevenseg_window_log2 is None:
            sevenseg_window_log2 = sevenseg_window_log2_for(slot.clock_hz)

        with_cpu = cpu_type is not None
        SoCCore.__init__(
            self,
            platform,
            sys_clk_freq,
            cpu_type=cpu_type,
            cpu_variant="standard",
            integrated_rom_size=ROM_SIZE if with_cpu else 0,
            integrated_sram_size=SRAM_SIZE if with_cpu else 0,
            integrated_main_ram_size=main_ram_size if with_cpu else 0,
            with_uart=with_cpu,
            uart_name="serial",
            uart_baudrate=UART_BAUD,
            with_timer=True,
            ident=f"tt-harness-handheld-vga {project_name}",
            ident_version=False,
        )

        self.core = core = HarnessCore(
            slot,
            fb_size,
            reference_ui_in=reference_ui_in,
            with_audio=with_audio,
            sevenseg_window_log2=sevenseg_window_log2,
        )
        self.slot = SlotCSR(slot)
        self.gamepad = GamepadCSR(slot)
        self.vga = VgaCSR(core, with_readback)
        self.sevenseg = SevenSegCSR(core, sevenseg_window_log2)
        self.atspeed = AtSpeedCSR(core.atspeed)
        self.info = InfoCSR(entry.clock_hz, achieved_clock(slot), fb_size, flags)


__all__ = [
    "CSR_MODULES",
    "PMODS",
    "HarnessCore",
    "HarnessSoC",
    "achieved_clock",
    "demo_entry",
    "demo_json_header",
]
