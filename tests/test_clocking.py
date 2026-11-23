# SPDX-License-Identifier: Apache-2.0
from fractions import Fraction

import pytest

from tt_harness import manifest
from tt_harness.core import clocking
from tt_harness.core.clocking import ClockError, solve

INPUTS = (50_000_000, 100_000_000)
EXACT = (31_500_000, 25_000_000, 50_000_000, 100_000_000)


def _check_limits(clk_in_hz, s):
    """Every setting the solver returns respects the MMCME2 -1 limits."""
    assert 1 <= s.divclk <= 106
    assert 2 <= s.mult <= 64 and (s.mult * 8).is_integer()
    assert 1 <= s.div <= 128 and (s.div * 8).is_integer()
    pfd = clk_in_hz / s.divclk
    assert 10e6 <= pfd <= 450e6
    assert 600e6 <= s.vco_hz <= 1200e6
    assert s.vco_hz == pytest.approx(clk_in_hz * s.mult / s.divclk, rel=1e-12)
    assert s.achieved_hz == pytest.approx(s.vco_hz / s.div, rel=1e-12)
    assert s.fractional == (not s.mult.is_integer() or not s.div.is_integer())


@pytest.mark.parametrize("clk_in_hz", INPUTS)
def test_25175_is_fractional_close_and_not_25000(clk_in_hz):
    s = solve(clk_in_hz, 25_175_000)
    _check_limits(clk_in_hz, s)
    assert abs(s.error_ppm) < 100
    assert s.achieved_hz != 25_000_000
    assert abs(s.achieved_hz - 25_000_000) > 100_000
    assert s.fractional


@pytest.mark.parametrize("clk_in_hz", INPUTS)
@pytest.mark.parametrize("clock_hz", EXACT)
def test_exact_values(clk_in_hz, clock_hz):
    s = solve(clk_in_hz, clock_hz)
    _check_limits(clk_in_hz, s)
    assert s.error_ppm == 0
    assert s.achieved_hz == clock_hz
    # An exact integer setting exists for each of these, and integer is preferred on a tie.
    assert not s.fractional


def test_tie_break_prefers_integer_then_lowest_divclk_then_highest_vco():
    # From 50 MHz, 31.5 MHz is exact with M=63/D=4/O=25 (integer, VCO 787.5 MHz),
    # M=63/D=5/O=20 (integer, VCO 630 MHz) and many fractional settings with D=1.
    s = solve(50_000_000, 31_500_000)
    assert (s.divclk, s.mult, s.div) == (4, 63.0, 25.0)
    assert s.vco_hz == 787_500_000


def test_spec_worked_example_25175_from_50mhz():
    # The design spec gives M=18.125, O=36 -> 25.1736 MHz (55 ppm low) as its example.
    # The solver finds a better one: M=18, D=1, O=35.75 -> 25.174825 MHz, 6.9 ppm low.
    # The spec's example is therefore within tolerance but not the optimum.
    spec_achieved = Fraction(50_000_000) * Fraction(145, 8) / 36
    spec_ppm = (spec_achieved - 25_175_000) / 25_175_000 * 1_000_000
    assert -56 < spec_ppm < -55

    s = solve(50_000_000, 25_175_000)
    assert (s.divclk, s.mult, s.div) == (1, 18.0, 35.75)
    assert s.vco_hz == 900_000_000
    exact = Fraction(900_000_000) / Fraction(143, 4)
    assert s.achieved_hz == float(exact)
    assert s.error_ppm == pytest.approx(float((exact - 25_175_000) / 25_175_000 * 10**6))
    assert -7 < s.error_ppm < -6.9
    assert abs(s.error_ppm) < abs(float(spec_ppm))
    assert s.fractional


def test_error_ppm_is_signed():
    low = solve(50_000_000, 25_175_000)
    assert low.error_ppm < 0
    # 25_174_000 sits between settings; whatever is chosen, the sign matches the arithmetic.
    s = solve(50_000_000, 25_174_000)
    assert (s.achieved_hz > 25_174_000) == (s.error_ppm > 0)


@pytest.mark.parametrize("clock_hz", [0, -1, -25_175_000])
def test_zero_and_negative_raise(clock_hz):
    with pytest.raises(ClockError) as exc:
        solve(50_000_000, clock_hz)
    assert str(clock_hz) in str(exc.value)


def test_above_part_limit_raises_naming_nearest():
    with pytest.raises(ClockError) as exc:
        solve(50_000_000, 3_000_000_000)
    msg = str(exc.value)
    assert "3000000000" in msg
    # The highest MMCM output is 800 MHz (DS181 MMCM_FOUTMAX).
    assert "800000000" in msg
    assert "ppm" in msg


def test_below_minimum_output_raises_naming_nearest():
    # VCO_min / 128 = 4.6875 MHz is the lowest CLKOUT0 frequency.
    with pytest.raises(ClockError) as exc:
        solve(50_000_000, 4)
    msg = str(exc.value)
    assert "4 Hz" in msg
    assert "4687500" in msg
    assert "ppm" in msg


def test_just_outside_tolerance_raises_and_names_nearest():
    # 4.6875 MHz * (1 - 0.0011) is below the lowest output by more than 0.1%.
    want = 4_682_000
    with pytest.raises(ClockError) as exc:
        solve(50_000_000, want)
    assert "4687500" in str(exc.value)
    # Within 0.1% of the lowest output solves.
    s = solve(50_000_000, 4_684_000)
    assert s.achieved_hz == 4_687_500
    assert 0 < s.error_ppm < 1000


def test_tolerance_parameter_is_respected():
    # 25.175 MHz is 6.9 ppm off from 50 MHz; a 1 ppm tolerance refuses it.
    with pytest.raises(ClockError):
        solve(50_000_000, 25_175_000, tolerance=1e-6)


@pytest.mark.parametrize("clk_in_hz", [0, 5_000_000, 900_000_000])
def test_input_clock_out_of_range_raises(clk_in_hz):
    with pytest.raises(ClockError):
        solve(clk_in_hz, 25_000_000)


def test_solve_is_deterministic():
    assert solve(100_000_000, 25_175_000) == solve(100_000_000, 25_175_000)


# Manifest clock values the MMCM alone cannot produce within 0.1%, with the reason.
# Do not add to this list to make the test pass; each entry needs a different mechanism.
UNSOLVABLE = {
    # ttsky26c/tt_um_wokwi_469806914724000769 declares clock_hz 4 (4 Hz). The MMCM's lowest
    # CLKOUT0 frequency is VCO_min/128 = 600 MHz/128 = 4.6875 MHz (DS181 MMCM_FOUTMIN), so a
    # 4 Hz clock needs a clock enable or divider in fabric, not an MMCM setting.
    4: "below the MMCM minimum output frequency of 4.6875 MHz",
}


def _manifest_clocks():
    repo = manifest.default_videos_repo()
    if not repo.is_dir():
        pytest.skip(f"TT_VIDEOS_REPO not present: {repo}")
    projects = manifest.load(repo)
    values = set()
    for p in projects.values():
        values.add(p.clock_hz)
        values.add(p.clock_declared)
    values.discard(0)
    return sorted(values)


@pytest.mark.parametrize("clk_in_hz", INPUTS)
def test_every_manifest_clock_solves_or_is_listed(clk_in_hz):
    failures = {}
    for clock_hz in _manifest_clocks():
        try:
            s = solve(clk_in_hz, clock_hz)
        except ClockError as e:
            failures[clock_hz] = str(e)
            continue
        assert abs(s.error_ppm) <= 1000
    unexpected = {hz: msg for hz, msg in failures.items() if hz not in UNSOLVABLE}
    assert not unexpected, f"clock_hz values with no MMCM setting within 0.1%: {unexpected}"


def test_limits_are_the_documented_ones():
    assert clocking.VCO_MIN_HZ == 600_000_000
    assert clocking.VCO_MAX_HZ == 1_200_000_000
    assert clocking.PFD_MIN_HZ == 10_000_000
    assert clocking.PFD_MAX_HZ == 450_000_000
    assert clocking.OUT_MAX_HZ == 800_000_000
