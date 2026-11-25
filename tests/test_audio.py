# SPDX-License-Identifier: Apache-2.0
"""`AudioFilter` and its rate generator in the Migen simulator.

Most filter tests run at a 768,000 Hz clock (16 clocks per sample) so a tone and its
settling take thousands, not millions, of clocks. The decimator's rate is tested separately
at the clock frequencies that are not integer multiples of 48 kHz.
"""

from __future__ import annotations

import itertools
import math
from fractions import Fraction

import pytest
from migen import run_simulation

from tt_harness.core.audio import AudioFilter, RateTick, cascade_gain

FS = 48_000
CLK = 768_000  # 16 x 48 kHz
SETTLE = 300  # samples to settle the DC blocker (DC_SHIFT 5: time constant 32 samples)
MEASURE = 240  # samples measured: 5 ms, a whole number of periods of every test tone
DC_SHIFT = 5  # fast DC blocker for tests; the default (8) is tested separately


def run_audio(dut, bit_of, samples: int, oe: int = 0x80, out_mask: int = 0x80) -> list[int]:
    """Drive uio_out[7] from `bit_of(clock)` and collect `samples` samples at `valid`."""
    got: list[int] = []

    def run():
        yield dut.uio_oe.eq(oe)
        n = 0
        while len(got) < samples:
            yield dut.uio_out.eq(out_mask if bit_of(n) else 0)
            if (yield dut.valid):
                got.append((yield dut.sample))
            yield
            n += 1

    run_simulation(dut, run())
    return got


def square(clk: int, freq: int):
    half = clk // (2 * freq)
    assert half * 2 * freq == clk
    return lambda n: (n // half) % 2


def goertzel_amplitude(x: list[int], freq: int, fs: int = FS) -> float:
    """Amplitude of the sinusoid at `freq` in `x` (exact when x holds whole periods)."""
    w = 2 * math.pi * freq / fs
    re = sum(v * math.cos(w * i) for i, v in enumerate(x))
    im = sum(v * math.sin(w * i) for i, v in enumerate(x))
    return 2 * math.hypot(re, im) / len(x)


def blocker_gain(freq: int, shift: int = DC_SHIFT) -> float:
    """Magnitude of the DC blocker (1 - z^-1) / (1 - (1 - 2^-shift) z^-1) at `freq` (48 kHz)."""
    z = complex(math.cos(2 * math.pi * freq / FS), -math.sin(2 * math.pi * freq / FS))
    return abs((1 - z) / (1 - (1 - 2.0**-shift) * z))


def expected_gain(dut, freq: int) -> float:
    return cascade_gain(freq, dut.clk_freq_hz, dut.shift) * blocker_gain(freq, dut.dc_shift)


def tone_amplitude(freq: int, clk: int = CLK) -> float:
    dut = AudioFilter(clk, dc_shift=DC_SHIFT)
    out = run_audio(dut, square(clk, freq), SETTLE + MEASURE)
    return goertzel_amplitude(out[SETTLE:], freq)


def test_silent_when_pin_not_enabled() -> None:
    # uio_out[7] high all the time, but uio_oe[7] never set (other oe bits set: masked off)
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    out = run_audio(dut, lambda n: 1, 200, oe=0x7F, out_mask=0xFF)
    assert len(out) == 200
    assert set(out) == {0}
    # and a toggling pin with the output enable clear
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    assert set(run_audio(dut, lambda n: (n // 5) % 2, 200, oe=0x00)) == {0}


def test_only_bit_7_is_the_audio_pin() -> None:
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    out = run_audio(dut, lambda n: (n // 7) % 2, 300, oe=0xFF, out_mask=0x7F)
    assert set(out) == {0}


def test_square_wave_comes_out_at_its_frequency() -> None:
    # Frequency of the largest spectral component of the output, searched over 500 Hz steps.
    for freq in (1000, 3000, 6000):
        dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
        out = run_audio(dut, square(CLK, freq), SETTLE + MEASURE)[SETTLE:]
        grid = range(500, 23_501, 500)
        peak = max(grid, key=lambda f: goertzel_amplitude(out, f))
        assert peak == freq, f"{freq} Hz tone peaked at {peak} Hz"
        # a 50% square wave of 1-bit levels is +/-16383.75 after the blocker (half scale, as in
        # the playground); its fundamental is 4/pi of that, less the filter's loss
        ideal = (4 / math.pi) * 16383.75 * expected_gain(dut, freq)
        assert goertzel_amplitude(out, freq) == pytest.approx(ideal, rel=0.05)


@pytest.mark.parametrize("freq", [8000, 16_000])
def test_in_band_response_matches_the_documented_filter(freq: int) -> None:
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    reference = expected_gain(dut, freq) / expected_gain(dut, 1000)
    measured = tone_amplitude(freq) / tone_amplitude(1000)
    assert measured == pytest.approx(reference, rel=0.08)


def test_cutoff_is_about_where_the_playground_puts_it() -> None:
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    # VGA Playground's effective response: -3 dB at about 8.4 kHz
    assert 7000 <= dut.cutoff_hz <= 10_000
    assert cascade_gain(dut.cutoff_hz, CLK, dut.shift) == pytest.approx(2**-0.5, rel=1e-3)


def test_tone_above_nyquist_is_attenuated_when_it_aliases() -> None:
    # 32 kHz aliases to 16 kHz at 48 kHz; the filter must have taken most of it out first.
    alias = tone_amplitude(32_000)
    inband = tone_amplitude(1000)
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    expected = cascade_gain(32_000, CLK, dut.shift) / cascade_gain(1000, CLK, dut.shift)
    assert alias / inband == pytest.approx(expected, rel=0.25)
    assert alias / inband < 0.15  # at least 16 dB down


def test_dc_is_removed_and_a_step_stays_in_range() -> None:
    # Constant 1: the DC blocker takes the offset out. A step from silence to 1 is a +32767 step
    # at the blocker input, so the first samples are close to +32767 (never beyond: the output
    # is the input minus a low-pass of it); the output then decays to exactly 0 (the leak
    # rounds away from zero so it cannot stick). Dropping to 0 makes a step the other way,
    # close to -32767, and decays to exactly 0 again.
    clk = 384_000  # 8 clocks per sample: the whole settle fits in few clocks
    settle = 700
    dut = AudioFilter(clk, dc_shift=DC_SHIFT)
    out = run_audio(dut, lambda n: 1 if n < settle * 8 else 0, 2 * settle)
    high, low = out[:settle], out[settle:]
    assert 30_000 <= max(high) <= 32767
    assert min(high) >= -32768
    assert set(high[-100:]) == {0}
    assert -32768 <= min(low) <= -30_000
    assert max(low) <= 32767
    assert set(low[-100:]) == {0}


def test_default_dc_blocker_settles_to_exactly_zero_in_under_3000_samples() -> None:
    clk = 384_000
    dut = AudioFilter(clk)  # default corner: 48000 / 2**8 / 2 pi = 29.8 Hz
    assert dut.dc_shift == 8
    out = run_audio(dut, lambda n: 1, 3000)
    assert max(out) >= 30_000 and set(out[-100:]) == {0}


def test_constant_zero_is_exactly_zero() -> None:
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    assert set(run_audio(dut, lambda n: 0, 100)) == {0}


def test_full_scale_square_wave_does_not_overflow() -> None:
    dut = AudioFilter(CLK, dc_shift=DC_SHIFT)
    out = run_audio(dut, square(CLK, 500), 800)
    assert min(out) >= -32768 and max(out) <= 32767


def sample_times(dut, clocks: int) -> list[int]:
    """Clock indices (the n-th clock edge is index n) at which `valid` is seen high."""
    times: list[int] = []

    def run():
        for i in range(clocks + 1):
            if (yield dut.valid):
                times.append(i)
            yield

    run_simulation(dut, run())
    return times


@pytest.mark.parametrize("clk", [25_175_000, 50_000_000, 768_000, 12_000_000, 27_000_000])
def test_rate_generator_has_no_long_run_error_and_one_clock_jitter(clk: int) -> None:
    gen = RateTick(clk, FS)
    clocks = 3 * (clk // math.gcd(clk, FS))  # 3 x 48000/gcd samples: whole number of cycles
    times = sample_times(gen, clocks)
    period = Fraction(clk, FS)
    # the n-th strobe is exactly ceil(n x period) clocks after the start: never more than
    # one clock (the jitter) from the ideal instant n x period, and no accumulating error
    assert times == [math.ceil(n * period) for n in range(1, len(times) + 1)]
    assert len(times) == clocks * FS // clk


def test_audio_filter_sample_rate_is_exact_for_25_175_and_50_mhz() -> None:
    # 25.175 MHz / 48 kHz = 25175/48 clocks per sample: 25175 clocks hold exactly 48 samples.
    # 50 MHz / 48 kHz = 3125/3 clocks per sample: 12500 clocks hold exactly 12 samples.
    for clk, clocks, expected in ((25_175_000, 25_175, 48), (50_000_000, 12_500, 12)):
        times = sample_times(AudioFilter(clk, dc_shift=DC_SHIFT), clocks)
        assert len(times) == expected, (clk, len(times))
        # consecutive spacings are floor or ceil of the ideal period, nothing else
        period = Fraction(clk, FS)
        gaps = {b - a for a, b in itertools.pairwise(times)}
        assert gaps <= {math.floor(period), math.ceil(period)}


def test_rejects_unusable_arguments() -> None:
    with pytest.raises(ValueError):
        AudioFilter(100_000)  # fewer than 8 clocks per sample
    with pytest.raises(ValueError):
        AudioFilter(CLK, sample_rate=0)
