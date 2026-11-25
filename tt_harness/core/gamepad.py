# SPDX-License-Identifier: Apache-2.0
"""A Gamepad Pmod, in gateware: the three signals a design's `gamepad_pmod` receiver reads.

Tiny Tapeout game designs read their controllers through a Gamepad Pmod
(https://github.com/psychogenic/gamepad-pmod): three wires on `ui_in[6:4]`, `ui_in[4]` latch,
`ui_in[5]` clock, `ui_in[6]` data. The harness has no Pmod, so `GamepadEmulator` produces the
same three signals; the unmodified `gamepad_pmod_driver` in the design (see
`tests/data/gamepad/gamepad_pmod.v`) reads the buttons the harness wants pressed.

Protocol. Sources: `docs/demo-runner.md` (the frame the demo board's runner sends, which this
block reproduces level for level), the Pmod's firmware (`src/reportport.cpp`,
`src/controllerset.cpp`, `src/ch32gamepad_config.h` in the gamepad-pmod repository) and the
receiver `verilog/gamepad_pmod.v`.

- A frame is 24 bits, bit 0 first. Bit `n` of `buttons` is the n-th bit sent. `buttons` is the
  word `tt_harness.demo.validate.gamepad_bits` makes: bits 0..11 are the second controller (or
  twelve ones for none), bits 12..23 the first controller, which `gamepad_pmod_single` and index
  0 of `gamepad_pmod_dual` read; within a controller the order is `GAMEPAD_BUTTONS` (b first,
  r last), a held button is a 1. A socket with no controller sends twelve ones, which the
  receiver reads as "not present".
- Each bit is three levels: data valid with clock low, clock high, clock low. The receiver
  shifts in `data` on the rising edge of `clock`, so data changes only while the clock is low.
- After bit 23: `latch` high (data still holds bit 23), `latch` low (same data), then all three
  low. The receiver takes the 24 bits on the rising edge of `latch`.
- All three lines are low between frames.

Timing. The real Pmod's clock period is 8 us (4 us half period) with 2 us of data setup and a
10 us latch pulse (`REPORTER_*` in `ch32gamepad_config.h`), and it polls the controllers at
600 Hz (`CONTROLLER_POLLING_INTERVAL_HZ`), reporting on a change and at least once a second.
This block sends every level for one "slot", the Pmod's clock half period of 4 us (at least 4
project clocks), so a bit takes three slots (12 us, a little slower than the Pmod's 10 us), the
latch pulse is one slot, and a frame is `FRAME_SLOTS` = 75 slots (3 slots for each of 24 bits,
the latch high, the latch low, and the first idle slot). A frame is sent every 1/600 s whether
or not `buttons` changed, so a design that was reset gets its buttons back within 1.7 ms, and a
change of `buttons` reaches the receiver in at most that time plus one frame.

What the receiver needs: it synchronises each wire with two flip-flops of its own clock and then
detects edges, so a level must last at least one receiver clock, and the latch's rising edge
must come at least one receiver clock after the last clock's rising edge (here: two slots). A
slot is 100 clocks at 25 MHz and at least 4 at 1 MHz, a wide margin.

Clocking. The block runs in the clock domain it is instantiated in. In the harness that is the
project's own clock, and `latch`, `clock` and `data` are each the direct output of a register,
so the project sees three glitch-free inputs changing just after its own clock edge. All
timing constants are derived from `clk_freq_hz`, which is that clock's frequency.

Cycle timing. The cycle after reset is the first of the first frame (`frame_timer` starts at
0): `buttons` is registered at the end of that cycle, slot 0 (data = `buttons[0]`, clock low)
is on the outputs from the next cycle, and slot `s` occupies cycles `1 + s * slot_clocks`
onwards, `slot_clocks` cycles each. `frame` is high for one cycle, the first cycle of slot 74
(all low): the frame has been completely sent. The next frame starts `frame_clocks` cycles
after the first.
"""

from __future__ import annotations

from migen import Array, If, Module, Signal

BITS = 24
CLOCK_HALF_PERIOD_US = 4  # the Pmod's REPORTER_CLOCK_PERIOD_US (8) / 2
MIN_SLOT_CLOCKS = 4  # a slot lasts at least this many clocks, whatever the clock frequency
FRAME_RATE_HZ = 600  # the Pmod's CONTROLLER_POLLING_INTERVAL_HZ
FRAME_SLOTS = BITS * 3 + 3  # three per bit, latch high, latch low, first idle slot


class GamepadEmulator(Module):
    """Sends `buttons` to a `gamepad_pmod` receiver, as a frame every `1 / frame_rate_hz` s.

    Inputs: `buttons` (24 bits, the word of `demo.validate.gamepad_bits`), a level sampled in
    the cycle each frame starts. Outputs: `latch`, `clock`, `data`, each the direct output of a
    register; `frame`, a one-cycle pulse when a frame has been completely sent.

    `slot_clocks` and `frame_clocks` are the length of one level and the frame period in clocks
    of the domain this is instantiated in, derived from `clk_freq_hz`. `frame_rate_hz` is
    for tests; the frame must fit in the period.
    """

    def __init__(self, clk_freq_hz: int, frame_rate_hz: int = FRAME_RATE_HZ) -> None:
        if clk_freq_hz < 1 or frame_rate_hz < 1:
            raise ValueError(
                f"clk_freq_hz and frame_rate_hz must be positive, got {clk_freq_hz}, {frame_rate_hz}"
            )
        self.slot_clocks = max(MIN_SLOT_CLOCKS, round(clk_freq_hz * CLOCK_HALF_PERIOD_US / 1e6))
        self.frame_clocks = round(clk_freq_hz / frame_rate_hz)
        if self.frame_clocks <= FRAME_SLOTS * self.slot_clocks:
            raise ValueError(
                f"a frame ({FRAME_SLOTS * self.slot_clocks} clocks) does not fit in the frame "
                f"period of {self.frame_clocks} clocks (clk_freq_hz {clk_freq_hz}, "
                f"frame_rate_hz {frame_rate_hz})"
            )

        self.buttons = Signal(BITS)
        self.latch = Signal()
        self.clock = Signal()
        self.data = Signal()
        self.frame = Signal()

        frame_timer = Signal(max=self.frame_clocks)  # 0 starts a frame
        slot_timer = Signal(max=self.slot_clocks)  # 0 in the last clock of a slot
        bit_no = Signal(max=BITS + 1)  # 0..23 the bits, 24 the latch and what follows
        phase = Signal(max=3)  # slot within the bit, or within the latch part
        active = Signal()
        shift = Signal(BITS)  # `buttons` as sampled at the start of the frame

        # The slot that follows the current one.
        next_bit_no = Signal(max=BITS + 1)
        next_phase = Signal(max=3)
        self.comb += If(
            phase == 2,
            next_bit_no.eq(bit_no + 1),
            next_phase.eq(0),
        ).Else(
            next_bit_no.eq(bit_no),
            next_phase.eq(phase + 1),
        )
        # Index 24 stands for the latch slots, where data still holds bit_no 23.
        bit_value = Array([shift[i] for i in range(BITS)] + [shift[BITS - 1]])

        start = frame_timer == 0
        self.sync += [
            frame_timer.eq(frame_timer - 1),
            self.frame.eq(0),
            If(
                start,
                frame_timer.eq(self.frame_clocks - 1),
                shift.eq(self.buttons),
                bit_no.eq(0),
                phase.eq(0),
                slot_timer.eq(self.slot_clocks - 1),
                active.eq(1),
                self.latch.eq(0),
                self.clock.eq(0),
                self.data.eq(self.buttons[0]),
            ).Elif(
                active,
                slot_timer.eq(slot_timer - 1),
                If(
                    slot_timer == 0,
                    slot_timer.eq(self.slot_clocks - 1),
                    bit_no.eq(next_bit_no),
                    phase.eq(next_phase),
                    If(
                        next_bit_no < BITS,
                        self.latch.eq(0),
                        self.clock.eq(next_phase == 1),
                        self.data.eq(bit_value[next_bit_no]),
                    ).Else(
                        self.clock.eq(0),
                        self.latch.eq(next_phase == 0),
                        self.data.eq(bit_value[next_bit_no] & (next_phase != 2)),
                    ),
                    If(
                        (next_bit_no == BITS) & (next_phase == 2),
                        active.eq(0),
                        self.frame.eq(1),
                    ),
                ),
            ),
        ]
