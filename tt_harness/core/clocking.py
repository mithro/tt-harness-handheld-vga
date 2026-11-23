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
- UG953 (Vivado 7 Series Libraries Guide), MMCME2_ADV attributes; the same ranges are
  in UG472 (7 Series Clocking Resources), Chapter 3, MMCM attributes table:
  DIVCLK_DIVIDE 1 to 106; CLKFBOUT_MULT_F 2.000 to 64.000; CLKOUT0_DIVIDE_F 1.000 to
  128.000, both in 0.125 steps (fractional mode). Only CLKOUT0 has a fractional divider.

One conservative restriction beyond those documents: a non-integer CLKOUT0_DIVIDE_F must
be at least 2.000 (LiteX's S7MMCM uses the same lower bound for the fractional divider).
It only excludes outputs above 300 MHz, which no project clock needs.

All arithmetic is exact (`fractions.Fraction`); only the reported `vco_hz`, `achieved_hz`
and `error_ppm` are floats.
"""

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

FIN_MIN_HZ = 10_000_000
FIN_MAX_HZ = 800_000_000
VCO_MIN_HZ = 600_000_000
VCO_MAX_HZ = 1_200_000_000
PFD_MIN_HZ = 10_000_000
PFD_MAX_HZ = 450_000_000
OUT_MAX_HZ = 800_000_000

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


def solve(clk_in_hz: int, clock_hz: int, tolerance: float = 0.001) -> MmcmSetting:
    """The MMCME2 setting closest to `clock_hz` from `clk_in_hz`.

    Ties on error go to integer settings, then the lowest DIVCLK_DIVIDE, then the highest
    VCO. Raises `ClockError` naming the requested and nearest achievable frequency when the
    best setting is further than `tolerance` (a fraction: 0.001 is 0.1%) from the request.
    """
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
    tol = Fraction(str(tolerance))
    if tol <= 0:
        raise ValueError(f"tolerance must be positive, got {tolerance}")
    best = _best(clk_in_hz, Fraction(clock_hz))
    setting = _setting(clk_in_hz, clock_hz, best)
    if best[0][0] / clock_hz > tol:
        raise ClockError(
            f"cannot generate {clock_hz} Hz from {clk_in_hz} Hz within "
            f"{float(tol * 1_000_000):g} ppm: nearest achievable is "
            f"{setting.achieved_hz:.3f} Hz ({setting.error_ppm:+.1f} ppm; "
            f"DIVCLK_DIVIDE={setting.divclk}, CLKFBOUT_MULT_F={setting.mult:.3f}, "
            f"CLKOUT0_DIVIDE_F={setting.div:.3f})"
        )
    return setting
