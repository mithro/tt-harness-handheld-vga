# SPDX-License-Identifier: Apache-2.0
"""A 1-bit audio stream (`uio_out[7]`) turned into 16-bit PCM at 48 kHz.

Where the sound comes from. On the Tiny Tapeout demo board, designs with sound put a 1-bit
stream (PWM / sigma-delta) on `uio_out[7]`, which the Audio Pmod's RC low-pass filter and
amplifier turn into sound. The source bit here is `uio_out[7] & uio_oe[7]`: a pin that is
not driven makes no sound.

What the VGA Playground does (reference: `vga-playground`, https://github.com/TinyTapeout/vga-playground,
commit 3e3c77e46ae7bd51609f680851aabb81a30564a6; files `src/AudioEngine.ts`, `src/AudioPlayer.ts`,
`src/index.ts` `getAudioSignal`, `public/resampler.js`). Each simulated clock it reads
`(uio_out & uio_oe) >> 7`. `AudioEngine` sums the bits over `ticksPerSample = 25.175 MHz / 192 kHz`
= 131.1 clocks (a boxcar average, counter compared against the fractional ratio, so the bits of a
window are not an exact count of whole clocks) and so makes one 0..1 sample at 192 kHz; the
sample rate is fixed at 192 kHz "for quality". A second moving average of
`ceil(192000 / 20000) = 10` of those samples is commented as the "Audio PMOD low pass filter"
(20 kHz); in fact a 10-tap boxcar at 192 kHz has -3 dB at 8.5 kHz, nulls at 19.2 kHz and
38.4 kHz, and -13 dB at 28 kHz. The result is fed unchanged to the browser's audio context at
192 kHz, whose `resampler.js` worklet matches rates by linear interpolation and, for an integer
downsampling factor, a boxcar average of that many samples (so the playground's last stage is
a further, gentle, low-pass at the sound card's rate). There is no gain stage and no DC removal:
the samples stay in 0..1 (silence is 0, the offset of a 1-bit stream is half scale) and the
browser's output path removes DC (the real Pmod has a coupling capacitor).

What this module does, for the same audible result in gateware.

Structure (one clock domain, the design's):

1. `RateTick`: a phase accumulator (`acc += 48000`, wrapping at `clk_freq_hz`) makes the sample
   strobe. After n clocks there have been exactly floor(n x 48000 / clk_freq_hz) strobes, so the
   long-run sample rate error is zero (any rational ratio, e.g. 25,175,000 / 48,000 =
   25175/48 clocks per sample, is exact: 25175 clocks hold exactly 48 samples). The n-th strobe
   is at clock ceil(n x clk_freq_hz / 48000) - 1: spacing is floor or ceil of the ideal period
   (jitter at most 1 clock, i.e. 40 ns at 25.175 MHz, and never accumulating).
2. Three cascaded one-pole low-pass sections at the clock rate, `y += (x - y) >> shift` with
   the step rounded away from zero (so `y` reaches its input exactly: no residue, a constant
   input gives exactly a constant output). `shift` is chosen from `clk_freq_hz` so that the
   -3 dB point of the three sections together is as near 8.4 kHz as a power of two allows:
   8.0 kHz at 25.175 MHz (shift 8), 7.9 kHz at 50 MHz (shift 9), 7.6 kHz at 12 MHz, 10.8 kHz
   at 1 MHz. This follows the playground's effective response (-3 dB at 8.5 kHz) and
   the Pmod's RC character with a steeper skirt than the playground: -13 dB at 20 kHz,
   -19 dB at 28 kHz (the alias of 20 kHz at 48 kHz; the playground has -13 dB) at 25.175 MHz.
   The same sections average the MHz-rate stream well below the 48 kHz rate before it is
   picked: a CIC would give a flat gain only for integer ratios, these sections give
   exactly unity gain at DC for any clock. The sections are sampled (not averaged) at the
   strobe, so the jitter above is a timing error on a smooth signal: it adds nothing audible.
   `cascade_gain()` and `cascade_cutoff_hz()` give the exact discrete response, which the
   tests measure against.
3. A DC blocker at 48 kHz, `y[n] = x[n] - x[n-1] + y[n-1] - leak(y[n-1])` with
   `leak = y >> 8` rounded away from zero: a pole at 1 - 2**-8, -3 dB at about 29.8 Hz
   (`dc_shift=8`; a constructor argument for tests). The playground leaves the offset (its
   0..1 samples are made bipolar by the browser output); here the output must be a signed
   PCM stream for a DAC, silent must be exactly 0, and a 1-bit stream's offset (half scale
   when the pin is toggling, full scale when stuck high) would otherwise come out as a loud
   click and constant offset: the real Audio Pmod is AC coupled, so this is the same.
4. Gain and saturation: the blocker takes the top 15 bits of the third section, so a 1 is
   32767 and a 0 is 0. The blocker's output is x minus a low-pass of x, which always lies in
   -32767..+32767, so it cannot overflow; the output is nevertheless clamped to
   -32768..32767 as a guard. Half scale of 16 bits is deliberate: a full-swing 50% square wave
   comes out as +/-16384 (-6 dBFS), exactly the playground's amplitude (0..1 samples whose
   offset the browser's output path removes, +/-0.5). Full 16-bit gain would clip the edges of
   square waves below about 3 kHz, because the blocker's droop adds to the edge height.

Numbers. Gain at 1 kHz is -0.05 dB (cascade) and -0.03 dB (blocker corner 29.8 Hz, tested).
A constant 1 (pin enabled and stuck high) or constant 0 input gives samples that decay to
exactly 0: after a step up from silence the first samples are about +32767, then decay with
the blocker's time constant of 256 samples (5.3 ms), reaching exactly 0 well within 3000
samples (62 ms; tested); a pin never enabled gives exactly 0 always, from the first sample.
Latency: the strobe is one clock after the accumulator decides; the signal passes
3 sections of group delay about 3 x 2**shift clocks (30.5 us at 25.175 MHz, 31 us at 50 MHz)
plus the DC blocker's (negligible at audio frequencies), so under 35 us.

Aliasing: the response above is the only anti-alias filter. Content between 24 kHz and
72 kHz folds into 0-24 kHz attenuated by at least 15.7 dB (at 24 kHz) rising to 30.5 dB
at 48 kHz and 40.3 dB at 72 kHz (25.175 MHz); a tone at 28 kHz folds to 20 kHz at -18.7 dB.
This is similar to the playground (-13 dB at 28 kHz), whose 10-tap boxcar has the same
weakness and which leaves the rest to the browser's resampler. A 1-bit stream's shaped
noise lies mostly well above 72 kHz and is removed by the three sections before sampling.

Resources (25.175 MHz, shift 8, width 26): phase accumulator 26 bits + valid 1; three
sections 3 x 26 = 78; DC blocker x_prev 15, accumulator 17, sample 16: about 153 flip-flops, no
multiplier, no block RAM; adders: a 26-bit compare-and-subtract for the strobe, per section a
27-bit subtract plus rounding add plus 26-bit add, and 18-bit adders in the blocker. The
register count grows by 3 bits per doubling of the clock (one more bit in each section, one in
the accumulator).
"""

from __future__ import annotations

import math

from migen import If, Module, Mux, Signal

TARGET_CUTOFF_HZ = 8500.0
"""-3 dB frequency of the whole low-pass the filter aims for (see the module docstring)."""

STAGES = 3
DEFAULT_DC_SHIFT = 8  # DC blocker: pole at 1 - 2**-8 per output sample


def _pole_power_gain(freq_hz: float, clk_freq_hz: float, shift: int) -> float:
    """|H|**2 of one section y += (x - y) * 2**-shift at one clock per step."""
    alpha = 2.0**-shift
    w = 2 * math.pi * freq_hz / clk_freq_hz
    return alpha**2 / (1 - 2 * (1 - alpha) * math.cos(w) + (1 - alpha) ** 2)


def cascade_gain(freq_hz: float, clk_freq_hz: float, shift: int) -> float:
    """Magnitude response of the three cascaded sections (before sampling), 1.0 at DC."""
    return _pole_power_gain(freq_hz, clk_freq_hz, shift) ** (STAGES / 2)


def cascade_cutoff_hz(clk_freq_hz: float, shift: int) -> float:
    """The -3 dB frequency of the cascade, found by bisection."""
    lo, hi = 0.0, clk_freq_hz / 2
    for _ in range(60):
        mid = (lo + hi) / 2
        if cascade_gain(mid, clk_freq_hz, shift) > 2**-0.5:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def choose_shift(clk_freq_hz: int) -> int:
    """The section shift whose cascade has its -3 dB point closest (in ratio) to the target."""
    candidates = range(1, 24)
    return min(
        candidates,
        key=lambda s: abs(math.log(cascade_cutoff_hz(clk_freq_hz, s) / TARGET_CUTOFF_HZ)),
    )


class RateTick(Module):
    """`tick`: one clock in every `clk_freq_hz / rate` on average, exactly, by a phase accumulator.

    `acc` advances by `rate` every clock and wraps at `clk_freq_hz`; `tick` is high (in the
    clock in which the wrap happens) when the next value would reach `clk_freq_hz`. After n
    clocks exactly floor(n * rate / clk_freq_hz) ticks have occurred, so the long-run rate error
    is zero, and the n-th tick is at clock ceil(n * clk_freq_hz / rate) - 1: never more than
    one clock from its ideal instant, spacing floor or ceil of the ideal period. `valid` is
    `tick` registered.
    """

    def __init__(self, clk_freq_hz: int, rate: int) -> None:
        if type(clk_freq_hz) is not int or type(rate) is not int or rate < 1 or clk_freq_hz < rate:
            raise ValueError(f"need integers 1 <= rate <= clk_freq_hz: {rate!r}, {clk_freq_hz!r}")
        self.clk_freq_hz = clk_freq_hz
        self.rate = rate
        self.tick = Signal()
        self.valid = Signal()
        bits = clk_freq_hz.bit_length() + 1
        acc = Signal(bits)
        total = Signal(bits)
        self.comb += [
            total.eq(acc + rate),
            self.tick.eq(total >= clk_freq_hz),
        ]
        self.sync += [
            acc.eq(Mux(self.tick, total - clk_freq_hz, total)),
            self.valid.eq(self.tick),
        ]


class AudioFilter(Module):
    """See the module docstring. Inputs `uio_out`, `uio_oe` (8 bits); outputs `sample`, `valid`."""

    def __init__(
        self, clk_freq_hz: int, sample_rate: int = 48000, dc_shift: int = DEFAULT_DC_SHIFT
    ) -> None:
        if type(sample_rate) is not int or sample_rate < 1:
            raise ValueError(f"sample_rate must be a positive integer: {sample_rate!r}")
        if type(clk_freq_hz) is not int or clk_freq_hz < 8 * sample_rate:
            raise ValueError(
                f"clk_freq_hz must be an integer of at least 8 x sample_rate: {clk_freq_hz!r}"
            )
        self.clk_freq_hz = clk_freq_hz
        self.sample_rate = sample_rate
        self.dc_shift = dc_shift
        self.shift = shift = choose_shift(clk_freq_hz)
        self.cutoff_hz = cascade_cutoff_hz(clk_freq_hz, shift)

        self.uio_out = Signal(8)
        self.uio_oe = Signal(8)
        self.sample = Signal((16, True))
        self.valid = Signal()

        self.submodules.rate = rate = RateTick(clk_freq_hz, sample_rate)
        self.comb += self.valid.eq(rate.valid)

        bit = Signal()
        self.comb += bit.eq(self.uio_out[7] & self.uio_oe[7])

        # Three cascaded one-pole sections at the clock rate. State is unsigned, W bits, full
        # scale 2**W - 1; 16 bits of result plus `shift` bits for the step plus two guard bits.
        width = 16 + shift + 2
        full = (1 << width) - 1
        previous: object = Mux(bit, full, 0)
        sections = []
        for i in range(STAGES):
            y = Signal(width, name=f"pole{i}")
            diff = Signal((width + 1, True), name=f"diff{i}")
            step = Signal((width + 1, True), name=f"step{i}")
            self.comb += [
                diff.eq(previous - y),
                # round away from zero so that y reaches its input exactly, in both directions
                step.eq((diff + Mux(diff > 0, (1 << shift) - 1, 0)) >> shift),
            ]
            self.sync += y.eq(y + step)
            sections.append(y)
            previous = y
        self.state = sections

        # DC blocker at the output rate, then saturation to 16 bits signed.
        x = Signal(15)
        x_prev = Signal(15)
        acc = Signal((17, True))
        leak = Signal((17, True))
        new = Signal((18, True))
        self.comb += [
            x.eq(sections[-1][width - 15 :]),
            leak.eq((acc + Mux(acc > 0, (1 << dc_shift) - 1, 0)) >> dc_shift),
            new.eq(x - x_prev + acc - leak),
        ]
        self.sync += If(
            rate.tick,
            x_prev.eq(x),
            acc.eq(new),
            self.sample.eq(Mux(new > 32767, 32767, Mux(new < -32768, -32768, new))),
        )
