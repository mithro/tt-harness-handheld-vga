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
ROM_SIZE = 0x10000  # the Builder shrinks it to the BIOS
SRAM_SIZE = 0x2000
MAIN_RAM_SIZE = 0x8000  # for the firmware
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
    both dividers multiples of 1/8. In a simulation build the bench drives the requested
    clock (`clock_hz / 1`); a stopped clock (`clock_hz` 0) is `0 / 1`.
    """
    if slot.clock is None:
        return slot.clock_hz, 1
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
        if with_audio and slot.clock_hz > 0:
            if slot.clock_hz < 8 * 48_000:
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
