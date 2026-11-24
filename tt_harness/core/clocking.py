# SPDX-License-Identifier: Apache-2.0
"""Project clock: an exact MMCME2 setting solver and the gated clock it drives.

`solve` picks a 7-series MMCME2 setting for a Tiny Tapeout project's declared clock and
refuses when none is within tolerance. It never substitutes a nearby integer setting: a
fractional setting that is closer always wins.

Limits are the Artix-7 speed grade -1 (and -1LI, the Arty A7-35T's -1L part) figures:

- DS181 (v1.27, 2022-02-10), Table 37 "MMCM Specification", -1 column:
  MMCM_FINMIN 10 MHz, MMCM_FINMAX 800 MHz, MMCM_FVCOMIN 600 MHz, MMCM_FVCOMAX 1200 MHz,
  MMCM_FPFDMIN 10 MHz, MMCM_FPFDMAX 450 MHz, MMCM_FOUTMAX 800 MHz,
  MMCM_FOUTMIN 4.69 MHz (note 5: F_VCO / 128).
- DS181 (v1.27), Table 32 "Global Clock Switching Characteristics (Including BUFGCTRL)",
  -1 and -1LI columns: F_MAX_BUFG 464 MHz. CLKOUT0 always drives a BUFG and a BUFGCE
  here, so this, not MMCM_FOUTMAX, is the highest usable output.
- UG953 (Vivado 7 Series Libraries Guide), MMCME2_ADV attributes; the same ranges are
  in UG472 (7 Series Clocking Resources), Chapter 3, MMCM attributes table:
  DIVCLK_DIVIDE 1 to 106; CLKFBOUT_MULT_F 2.000 to 64.000; CLKOUT0_DIVIDE_F 1.000 to
  128.000, both in 0.125 steps (fractional mode). Only CLKOUT0 has a fractional divider.

One restriction is unconfirmed: a non-integer CLKOUT0_DIVIDE_F is required to be at least
2.000. Its only source is LiteX's practice (S7MMCM's CLKOUT0 range starts at 2); no
primary Xilinx statement of a minimum fractional divide was found. It only excludes
outputs above 300 MHz, which the 464 MHz buffer limit nearly excludes anyway.

Confirmed from AMD PG065 (Clocking Wizard): fractional divide cannot be combined with a
phase shift or a non-50% duty cycle on that output. `ProjectClock` uses CLKOUT0 with phase
0 and duty cycle 0.5, so it satisfies both.

All arithmetic is exact (`fractions.Fraction`); only the reported `vco_hz`, `achieved_hz`
and `error_ppm` are floats.
"""

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from migen import ClockDomain, Constant, Instance, Module, Signal
from migen.genlib.cdc import MultiReg

FIN_MIN_HZ = 10_000_000
FIN_MAX_HZ = 800_000_000
VCO_MIN_HZ = 600_000_000
VCO_MAX_HZ = 1_200_000_000
PFD_MIN_HZ = 10_000_000
PFD_MAX_HZ = 450_000_000
MMCM_FOUT_MAX_HZ = 800_000_000
# F_MAX_BUFG, speed grade -1 (DS181 Table 32). The effective output cap.
BUFG_FMAX_HZ = 464_000_000
OUT_MAX_HZ = min(MMCM_FOUT_MAX_HZ, BUFG_FMAX_HZ)

DIVCLK_RANGE = (1, 106)
# Multiplier and divider are searched in eighths (0.125 steps).
MULT8_RANGE = (2 * 8, 64 * 8)
DIV8_RANGE = (1 * 8, 128 * 8)
# Lowest non-integer CLKOUT0_DIVIDE_F, in eighths (see module docstring).
DIV8_FRACTIONAL_MIN = 2 * 8


class ClockError(ValueError):
    """No MMCM setting reaches the requested clock within tolerance."""


@dataclass(frozen=True)
class MmcmSetting:
    clk_in_hz: int
    clock_hz: int
    divclk: int  # DIVCLK_DIVIDE
    mult: float  # CLKFBOUT_MULT_F, a multiple of 0.125
    div: float  # CLKOUT0_DIVIDE_F, a multiple of 0.125
    vco_hz: float
    achieved_hz: float
    error_ppm: float  # signed: achieved minus requested
    fractional: bool  # True when mult or div is not an integer

    @property
    def clkin_period_ns(self) -> float:
        return 1e9 / self.clk_in_hz


def _div8_candidates(ideal: Fraction, lo: int, hi: int):
    for d8 in {floor(ideal), ceil(ideal)}:
        d8 = min(max(d8, lo), hi)
        if d8 % 8 == 0 or d8 >= DIV8_FRACTIONAL_MIN:
            yield d8
        else:
            # A fractional divider below 2.000: the valid neighbours are 1.000 and 2.000.
            yield from (x for x in (8, DIV8_FRACTIONAL_MIN) if lo <= x <= hi)


def _search(clk_in_hz: int, target: Fraction):
    """Yield (sort key, divclk, mult8, div8, vco, out) for the best div8 at each divclk/mult8."""
    fin = Fraction(clk_in_hz)
    for divclk in range(DIVCLK_RANGE[0], DIVCLK_RANGE[1] + 1):
        pfd = fin / divclk
        if not PFD_MIN_HZ <= pfd <= PFD_MAX_HZ:
            continue
        for m8 in range(MULT8_RANGE[0], MULT8_RANGE[1] + 1):
            vco = pfd * m8 / 8
            if not VCO_MIN_HZ <= vco <= VCO_MAX_HZ:
                continue
            lo = max(DIV8_RANGE[0], ceil(vco * 8 / OUT_MAX_HZ))
            for d8 in _div8_candidates(vco * 8 / target, lo, DIV8_RANGE[1]):
                out = vco * 8 / d8
                frac_parts = (m8 % 8 != 0) + (d8 % 8 != 0)
                key = (abs(out - target), frac_parts > 0, frac_parts, divclk, -vco, d8)
                yield key, divclk, m8, d8, vco, out


def _best(clk_in_hz: int, target: Fraction):
    return min(_search(clk_in_hz, target), key=lambda c: c[0])


def _setting(clk_in_hz: int, clock_hz: int, best) -> MmcmSetting:
    _, divclk, m8, d8, vco, out = best
    err = (out - clock_hz) / clock_hz * 1_000_000
    return MmcmSetting(
        clk_in_hz=clk_in_hz,
        clock_hz=clock_hz,
        divclk=divclk,
        mult=m8 / 8,
        div=d8 / 8,
        vco_hz=float(vco),
        achieved_hz=float(out),
        error_ppm=float(err),
        fractional=(m8 % 8 != 0) or (d8 % 8 != 0),
    )


def _lowest_output(clk_in_hz: int) -> Fraction:
    return _best(clk_in_hz, Fraction(1))[-1]


def _hz(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{float(value):.3f}"


def solve(clk_in_hz: int, clock_hz: int, tolerance: float = 0.001) -> MmcmSetting:
    """The MMCME2 setting closest to `clock_hz` from `clk_in_hz`.

    Ties on error go to integer settings, then the lowest DIVCLK_DIVIDE, then the highest
    VCO. Raises `ClockError` naming the requested and nearest achievable frequency when the
    best setting is further than `tolerance` (a fraction: 0.001 is 0.1%) from the request.
    """
    tol = Fraction(str(tolerance))
    if tol <= 0:
        raise ValueError(f"tolerance must be positive, got {tolerance}")
    if not FIN_MIN_HZ <= clk_in_hz <= FIN_MAX_HZ:
        raise ClockError(
            f"input clock {clk_in_hz} Hz is outside the MMCM input range "
            f"{FIN_MIN_HZ} to {FIN_MAX_HZ} Hz"
        )
    if clock_hz <= 0:
        lowest = _setting(clk_in_hz, 1, _best(clk_in_hz, Fraction(1)))
        raise ClockError(
            f"cannot generate {clock_hz} Hz: a project clock must be positive "
            f"(the nearest achievable from {clk_in_hz} Hz is {lowest.achieved_hz:.3f} Hz)"
        )
    best = _best(clk_in_hz, Fraction(clock_hz))
    setting = _setting(clk_in_hz, clock_hz, best)
    if best[0][0] / clock_hz > tol:
        where = f"cannot generate {clock_hz} Hz from {clk_in_hz} Hz within {float(tol * 100):g}%"
        nearest = (
            f"nearest achievable is {setting.achieved_hz:.3f} Hz "
            f"(DIVCLK_DIVIDE={setting.divclk}, CLKFBOUT_MULT_F={setting.mult:.3f}, "
            f"CLKOUT0_DIVIDE_F={setting.div:.3f})"
        )
        lowest = _lowest_output(clk_in_hz)
        if clock_hz > OUT_MAX_HZ:
            reason = f"above the global clock buffer maximum {OUT_MAX_HZ} Hz"
        elif clock_hz < lowest:
            reason = f"below the MMCM minimum output {_hz(lowest)} Hz from this input"
        else:
            reason = f"{setting.error_ppm:+.1f} ppm off"
        raise ClockError(f"{where}: {reason}; {nearest}")
    return setting


def explicit_setting(clk_in_hz: int, divclk: int, mult: float, div: float) -> MmcmSetting:
    """The `MmcmSetting` for given MMCM parameters, for settings `solve` would not choose.

    The parameters are checked against the same limits `solve` searches within, and a
    `ClockError` names the one that is broken. `clock_hz` is the achieved frequency rounded
    to a whole hertz and `error_ppm` is relative to that.
    """
    if not FIN_MIN_HZ <= clk_in_hz <= FIN_MAX_HZ:
        raise ClockError(f"input clock {clk_in_hz} Hz is outside the MMCM input range")
    if type(divclk) is not int or not DIVCLK_RANGE[0] <= divclk <= DIVCLK_RANGE[1]:
        raise ClockError(f"DIVCLK_DIVIDE {divclk!r} is not an integer in {DIVCLK_RANGE}")
    m8, d8 = Fraction(mult) * 8, Fraction(div) * 8
    if m8.denominator != 1 or d8.denominator != 1:
        raise ClockError(
            f"CLKFBOUT_MULT_F {mult} and CLKOUT0_DIVIDE_F {div} must be in 0.125 steps"
        )
    m8, d8 = int(m8), int(d8)
    if not MULT8_RANGE[0] <= m8 <= MULT8_RANGE[1]:
        raise ClockError(f"CLKFBOUT_MULT_F {mult} is outside 2.000 to 64.000")
    if not DIV8_RANGE[0] <= d8 <= DIV8_RANGE[1] or (d8 % 8 and d8 < DIV8_FRACTIONAL_MIN):
        raise ClockError(
            f"CLKOUT0_DIVIDE_F {div} is outside 1.000 to 128.000 (2.000 up when fractional)"
        )
    pfd = Fraction(clk_in_hz) / divclk
    if not PFD_MIN_HZ <= pfd <= PFD_MAX_HZ:
        raise ClockError(f"PFD frequency {float(pfd):.0f} Hz is outside the MMCM range")
    vco = pfd * m8 / 8
    if not VCO_MIN_HZ <= vco <= VCO_MAX_HZ:
        raise ClockError(f"VCO frequency {float(vco):.0f} Hz is outside the MMCM range")
    out = vco * 8 / d8
    if out > OUT_MAX_HZ:
        raise ClockError(f"output {float(out):.0f} Hz is above the buffer maximum {OUT_MAX_HZ} Hz")
    clock_hz = round(out)
    key = (abs(out - clock_hz), None)
    return _setting(clk_in_hz, clock_hz, (key, divclk, m8, d8, vco, out))


class ProjectClock(Module):
    """The project clock: an MMCME2_ADV at `solve(clk_in_hz, clock_hz)`, gated by a BUFGCE.

    Inputs: `enable` (any domain), `reset` (MMCM reset, active high; tie low or leave).
    Outputs: `clk` (the gated project clock), `locked` (MMCM LOCKED, asynchronous).
    Attribute: `setting`, the `MmcmSetting` instantiated. A `setting` given to the constructor
    (see `explicit_setting`) is used as it is and `clock_hz` is then not consulted; the
    free-running copy of the clock is the clock of the domain `<name>_free`.

    Lock. CE is driven by `enable & locked`, synchronised, so `clk` only runs while the
    MMCM is locked and stops (two output clocks later) if lock is lost. The harness must
    still hold the project's reset until the clock is running.

    Gating. A 7-series BUFGCE is a BUFGCTRL whose CE is a synchronous input: it is
    sampled against the buffer's own input clock and the output stops or restarts without
    a runt pulse only if CE meets its setup and hold time to that clock (UG472, "BUFGCE
    and BUFGCE_1"; DS181 Table 32 T_BCCCK_CE / T_BCCKC_CE, note 1). So `enable & locked`
    is synchronised by two flops
    (a MultiReg, which LiteX's Xilinx overrides mark ASYNC_REG) clocked by the un-gated
    MMCM output through its own BUFG, and the second flop drives CE. Those flops must not
    be clocked by the gated output, which stops while CE is low and could never re-enable
    it. The flop-to-CE path is then an ordinary same-clock path that static timing checks.

    `platform` is accepted for the later build task (constraints); it is not used here and
    may be None when the module is only converted to Verilog.
    """

    def __init__(
        self,
        platform,
        clk_in,
        clk_in_hz: int,
        clock_hz: int,
        name: str = "project",
        setting: MmcmSetting | None = None,
    ):
        self.platform = platform
        if setting is None:
            setting = solve(clk_in_hz, clock_hz)
        elif setting.clk_in_hz != clk_in_hz:
            raise ValueError(
                f"the setting is for an input clock of {setting.clk_in_hz} Hz, not {clk_in_hz}"
            )
        self.setting = s = setting

        self.enable = Signal()
        self.reset = Signal()
        self.clk = Signal()
        self.locked = Signal()

        clkfb = Signal()
        clkout_raw = Signal()
        ce = Signal()

        # The free-running copy of the project clock, only for the CE synchroniser.
        cd_free = ClockDomain(f"{name}_free", reset_less=True)
        self.clock_domains += cd_free

        self.specials += Instance(
            "MMCME2_ADV",
            p_BANDWIDTH="OPTIMIZED",
            # CLKFBOUT feeds CLKFBIN directly (no feedback buffer): internal compensation.
            p_COMPENSATION="INTERNAL",
            p_CLKIN1_PERIOD=s.clkin_period_ns,
            p_DIVCLK_DIVIDE=s.divclk,
            p_CLKFBOUT_MULT_F=s.mult,
            p_CLKFBOUT_PHASE=0.0,
            p_CLKOUT0_DIVIDE_F=s.div,
            p_CLKOUT0_PHASE=0.0,
            p_CLKOUT0_DUTY_CYCLE=0.5,
            i_CLKIN1=clk_in,
            i_CLKIN2=0,
            i_CLKINSEL=1,
            i_CLKFBIN=clkfb,
            o_CLKFBOUT=clkfb,
            i_RST=self.reset,
            i_PWRDWN=0,
            i_DCLK=0,
            i_DEN=0,
            i_DWE=0,
            i_DADDR=Constant(0, 7),
            i_DI=Constant(0, 16),
            i_PSCLK=0,
            i_PSEN=0,
            i_PSINCDEC=0,
            o_CLKOUT0=clkout_raw,
            o_LOCKED=self.locked,
        )
        self.specials += Instance("BUFG", i_I=clkout_raw, o_O=cd_free.clk)
        self.specials += MultiReg(self.enable & self.locked, ce, odomain=cd_free.name, n=2)
        self.specials += Instance("BUFGCE", i_I=clkout_raw, i_CE=ce, o_O=self.clk)
