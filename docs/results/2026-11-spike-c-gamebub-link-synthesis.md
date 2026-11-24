# Spike C: synthesis of the Game Bub MCU link (2026-11-24)

**Nothing here has run on a real Game Bub.** Both bitstreams were built and timed in the tools
only; none was loaded onto hardware. Every external timing number below is an assumption, marked
as such.

Result in one paragraph: `QspiSlave` + `McuLink` (as `link_top` on the Game Bub pins,
`xc7a100tcsg324-1`) build to a bitstream with both Vivado 2025.2 and the open flow (Yosys +
nextpnr-xilinx 0.8.2), but the two flows establish very different things. The open flow
establishes only register-to-register setup in the SPI domain against 12.5 ns (the clock is
constrained at 80 MHz, because nextpnr-xilinx times falling-edge flops as rising-edge ones) and
in `sys` at 50 MHz; nothing about pad timing, hold, recovery/removal or crossings. Vivado
constrains and meets every domain and also checks inputs, outputs, hold, recovery/removal and
the crossings: 40 MHz writes are met with about 5 ns of slack on the worst pad-to-flop path
(under an assumed 6 ns MCU output delay). The `dq` outputs cannot be timed at 40 MHz with a 3 ns
MCU setup (the kept first run misses by 1.406 ns) but meet at the 16 MHz read cap by 16.5 ns, so
they are timed at 16 MHz; reads never run faster than that.

## 1. What was built

- `tt_harness/boards/gamebub/link_top.py`: the top. `clk50` (P17) through a BUFG is `sys`; the
  SPI clock pad T14 through a BUFG is `spi`; `spi_n` is its inversion. `dq` are four explicit
  IOBUFs tristated by `~dq_oe`. Twelve buttons (active low, pull-ups) go through two flip-flops
  in `sys`, inverted, into `McuLink.buttons`. `McuLink.reset` is a power-on counter (held 15
  system cycles) through a flip-flop. `irq` drives N15; `running`, `cmd_overrun`, `cmd_valid`
  and `focus` drive the four test points; the CPU port (six 32-bit forwarded words, overrun,
  dock, vibrate, filter) is XOR-folded into an 8-bit register shown on the PMOD pins and is
  acknowledged one cycle after it arrives. Nothing is optimised away: all 192 forwarded-word
  flip-flops are live.
- `tt_harness/boards/gamebub/link_build.py`: the two flows and the gate. `tt_harness.build` is
  built around the compare build (clock plan, `pclk`, generated top), so only its lower-level
  pieces are reused: `verilate.run` (tool runner and log), `tools.find`, the chip database and
  X-Ray database paths, the nextpnr clock-report pattern, `TimingError`, and the Vivado summary
  table reader. The constraints, Vivado script, nextpnr pre-pack script and the gate are new.
- Gate (a failure raises `TimingError` and leaves no `.bit`): a bitstream exists; Vivado
  `check_timing` shows zero for `no_clock`, `constant_clock`, `pulse_width_clock`,
  `unconstrained_internal_endpoints`, `multiple_clock`, `generated_clocks`, `loops`,
  `partial_input_delay`, `partial_output_delay`, `latch_loops`; the clocks in the report are
  exactly `sys`, `spi`, `spi_rd` with the expected periods; each has non-negative setup, hold
  and pulse-width slack; Vivado says all user constraints are met. For nextpnr: exactly `sys`
  and `spi` were constrained by the pre-pack script, none is timed at a default, no clock pin
  is off the BUFG network, and both report PASS against the constrained target.
- `tests/gamebub/test_link_synth.py`: one unmarked test (ports and pins of the top), one
  `toolchain` test (open flow) and one `vivado` test; both build tests assert the bitstream
  exists and the clocks are constrained and met. The open-flow test also asserts the
  documented limitation that its hold slack is `None`.
- Also fixed (test-first): `McuLink`'s CPU-port acknowledge acts on the rising edge of
  `cmd_ack`. Before, a `cmd_ack` held high across the arrival of a forwarded command captured
  the command and cleared it a cycle later without setting `cmd_overrun`. New test:
  `test_held_cmd_ack_does_not_clear_a_new_command_silently`.

## 2. Constraints and the assumption behind each

All in `link_build.py` (constants at the top) and `build/gamebub-link/vivado/timing.tcl`.

| Constraint | Value | Basis |
|---|---|---|
| `sys` | 50 MHz on `clk50` | the board oscillator (platform file) |
| `spi` | 40 MHz on T14 | the firmware's register-write clock (protocol 1) |
| `spi_rd` | 16 MHz on T14 (`create_clock -add`), physically exclusive with `spi` | the firmware's cap on read clocks; reads run at 10 MHz (protocol 1) |
| input delay on `cs_n`, `dq`, vs `spi` falling edge (`-clock_fall`) | max 6.0 ns, min 0.0 ns | **ASSUMPTION.** Mode 0: the MCU changes data after the falling edge (protocol 2), so each input has half a period, 12.5 ns at 40 MHz. The ESP32-S3's real output delay and the board's flight time are not in this repository; 6 ns max is a guess for pad delay plus flight, min 0 is the safe bound for hold. |
| output delay on `dq`, vs `spi_rd` rising edge | max 3.0 ns, min 0.0 ns | **ASSUMPTION.** The MCU samples on the rising edge; 3 ns stands for its setup time plus flight; its hold requirement is taken as 0. |
| `cs_n` also given the input delay against `spi_rd` | as above | so that the combinational path cs_n to dq (the bus is released when chip select rises) exists against the same clock as the dq output delay. Chip select is really a software GPIO (protocol 1) and changes microseconds from any clock edge; launching it like the data is a conservative stand-in. |
| false path from the 12 `buttons_*` ports | | asynchronous inputs into a two-flip-flop synchroniser |
| false path to `mcu_irq`, `pmod`, `tp` | | a level interrupt and slow observation pins with no clock relation |
| false path to the D pin of every synchroniser first stage (43 flip-flops) | | single-bit crossings in both directions through two flip-flops; the stage is found by rule (ASYNC_REG flip-flop whose D is not driven by a flip-flop on its own clock) and listed in the log (`tt-harness first stage:`). 18 sys-to-spi (12 buttons, 5 pending, reset), 13 spi-to-sys, 12 buttons from pads. Hand-written stages in `McuLink.to_spi` and the top's button stage are covered the same way as LiteX's `MultiReg` ones. |
| `set_max_delay -datapath_only 20 ns` from `fwd_word*` (192) and `clr_mask*` (5) flip-flops to clock `sys` | 20 ns | the direct multi-bit crossings. The words are written at the same `spi` edge as their toggle, which then needs two `sys` flip-flops and a compare before `sys` captures the words: at least two `sys` periods (40 ns) after the spi edge, and the words cannot change again for a whole further transaction (>0.4 us). A data path under 40 ns is therefore enough; 20 ns is half of that. `-datapath_only` because the two clocks are unrelated. |
| no `set_clock_groups -asynchronous` between `spi` and `sys` | | In Vivado clock groups outrank `set_max_delay`, so the max-delay constraints above would have been silently dropped. Instead each crossing has its own exception and `report_clock_interaction` shows `sys -> spi`: False Path; `spi -> sys`: Max Delay Datapath Only; any other crossing would show as Timed and fail. |
| `set_clock_groups -physically_exclusive spi spi_rd` | | one pin cannot be at two rates at once. This one is a clock group, and the only one, used where a max-delay is not needed. |

`CFGBVS VCCO` and `CONFIG_VOLTAGE 3.3` are set so the DRC passes (assumed from the 3.3 V I/O on
that bank; the real board's configuration settings, slave-serial mode and the 80 MHz
configuration clock on the shared T14/V15 pins are out of scope).

### Why the outputs are timed at 16 MHz and not 40 MHz

The first full Vivado run constrained the `dq` outputs against `spi` at 40 MHz. They failed by
1.40 ns (four endpoints, `linktop_dq_o_reg[*]` to the pads; reports kept locally in `work/gamebub-link-run1/`, not committed): the
falling-edge flop sits 5.3 ns behind the pad on the clock network, its output goes through an
OBUFT (default LVCMOS33, slow slew) that adds 3.5 ns, and only 12.5 ns less the assumed 3 ns is
available. Reads never run at 40 MHz, so the outputs are timed against `spi_rd`. The path then
has 16.5 ns of slack; solving for the half period at which it would just meet gives about
14.7 ns, so under these assumptions the output path could be driven at up to about 34 MHz.
Nothing above 16 MHz is promised by the firmware.

## 3. Results

### Vivado 2025.2 (`build/gamebub-link/vivado/`, bitstream 3 825 909 bytes)

| Item | Result |
|---|---|
| `sys` 50 MHz | setup WNS +14.66 ns, hold WHS +0.12 ns, pulse width +9.50 ns |
| `spi` 40 MHz | setup WNS **+4.96 ns**, hold WHS +0.054 ns, pulse width +12.00 ns |
| `spi_rd` 16 MHz | setup WNS +13.38 ns, hold WHS +0.054 ns, pulse width +30.75 ns |
| Worst input path (pad to flop), 40 MHz | `mcu_qspi_dq[0]` to `core_mb5_reg[24]`: slack +4.96 ns; requirement 12.5 ns from the falling edge; input delay 6.0; data path 3.08 ns (IBUF, then 2.6 ns of routing); clock arrives 1.6 ns later than at the pad |
| Worst input hold | +9.36 ns (`dq[3]` to `hi_reg[3]`) |
| Worst output path, 16 MHz | `dq_o_reg` to pad: +16.54 ns; `cs_n` to `dq` (bus release): +13.38 ns (IBUF, one LUT6, OBUFT, 8.8 ns); output hold +3.3 ns |
| Recovery / removal of the async preset (`cs_n` to FDPE PRE) | recovery +6.77 ns, removal +9.94 ns, both against `spi` |
| Worst reg-to-reg inside `spi` | +10.43 ns (`nib_reg` to `dq_o_reg[0]`, a half-cycle path at 12.5 ns); the worst hold, +0.054 ns, is `host_mb3_reg[17]` to `fwd_word3_reg[17]` |
| Crossings | `spi -> sys` datapath-only worst +18.38 ns; `sys -> spi` false path (`report_clock_interaction`) |
| `check_timing` | all ten checks zero. `no_input_delay` 12 and `no_output_delay` 9 are the exceptions listed above (buttons; `mcu_irq`, `pmod`, `tp`), each "user has a false path" |
| Utilisation | 595 LUTs, 1138 flip-flops, 28 IOBs, 2 BUFG, no block RAM (about 1% of the part) |
| Inverted clock | four `FDRE` with `IS_C_INVERTED`, driven by the BUFG net `spi_clk` (which has one driver, `spi_bufg`, and 860 loads). No LUT on the clock net. |
| Async preset | one `FDPE`, `PRE` driven by `cs_n` through an IBUF, timed as recovery/removal |
| CDC report | CDC-3 (26, 1-bit synchronised), CDC-6 warnings (8: multi-bit buses of independent bits through ASYNC_REG stages: buttons, pending, irq_enable, dock), CDC-15 warnings (384: the clock-enable structure of the forwarded words, which is the intended design, with the datapath-only exception), **CDC-4 critical (2: `clr_mask` to `pending`, once per source clock `spi`/`spi_rd`)**. CDC-4 is Vivado saying it cannot recognise the circuit as a synchroniser: it is the direct multi-bit crossing justified in section 2 (one effective bit, bit 1; the others are constants), covered by the datapath-only exception. It is a real critical in the tool's terms and is reported as one. ``TIMING-9` (Unknown CDC Logic: the same crossings, through false-path and datapath-only exceptions) and `TIMING-18` (nine warnings, explained next) are the other methodology warnings. |

#### The nine TIMING-18 warnings (`methodology.rpt`), each explained

Verified by experiment on the routed checkpoint (`build/gamebub-link/exp/exp7.tcl`): adding
rising-edge-referenced input delays on both `spi` and `spi_rd` clears the five input warnings and
leaves the four output ones; adding output delays against the falling edge of `spi_rd` and both
edges of `spi` clears those too. The rule asks for a delay relative to both edges of every clock
that reaches the port, for every such clock.

- Input, `mcu_qspi_cs_n`, `mcu_qspi_dq[0..3]` (5): "missing ... relative to the rising and/or
  falling clock edge(s) of spi, spi_rd". These ports have input delays against the **falling**
  edge of `spi` (and of `spi_rd` for `cs_n` only), because mode 0 launches them there; none
  against the rising edge, and `dq` none against `spi_rd`. Intended: the MCU never launches
  on the rising edge, and `spi_rd` is the slower read clock whose inputs are covered by the
  stricter 40 MHz `spi` check. `cs_n` is flagged for the same reason as `dq` (rising edge
  missing); its `spi_rd` delay exists but is falling-edge only.
- Output, `mcu_qspi_dq[0..3]` (4): missing against `spi` (all edges) and against the falling
  edge of `spi_rd`. Intended: the slave drives `dq` only while the MCU reads, so the outputs are
  timed only against the `spi_rd` rising edge (at 40 MHz they would fail, section 2).

Closing them is not cheap or clean: with every missing delay added, the checkpoint's timing
summary shows WNS -5.37 ns and WHS -3.14 ns (540 failing hold endpoints). Rise-launched inputs
meeting the rising-edge flops cannot hold against the 5 ns of clock lag behind the pad, and the
outputs fail at 40 MHz. These are modelled data relationships the MCU does not produce, so they
are accepted as explained warnings and not waived: the constraints stay as they are.

One CDC finding was fixed in this task: the reset was a combinational function of the power-on
counter feeding a synchroniser (CDC-10 critical in the first run). It is now a flip-flop output.

Thin points: hold slack inside `spi` is 0.05 ns (positive, a flop-to-flop path the router
fixed; same on both clocks), and the input slack depends on the 1.6 ns by which the clock lags the
pad.

### Open flow: Yosys 0.71+77 and nextpnr-xilinx 0.8.2 (`build/gamebub-link/openxc7/`, bitstream 3 826 017 bytes)

| Item | Result |
|---|---|
| Result | placed, routed, bitstream written (`fasm2frames`, `xc7frames2bit`); seed 1 |
| `sys` 50 MHz | Fmax 260.96 MHz: PASS at 50 MHz (16.2 ns of period slack) |
| `spi`, constrained at 80 MHz (see below) | Fmax 86.64 MHz: PASS at 80.00 MHz; 0.96 ns of slack against the 12.5 ns half-period budget. The worst path is 11.5 ns: `cmd` register through the address adder and the register decode to a register, which has a real budget of 25 ns at 40 MHz |
| Cross-domain | nextpnr prints only `Max delay posedge spi_clk -> posedge sys_clk: 2.13 ns` and the reverse, 1.81 ns. It checks neither against a requirement |
| Inverted clock | Yosys maps the inversion to `FDRE_1`: 4 cells, no LUT on a clock net (the 14 `INV` cells in Yosys's statistics are data inverters, packed as LUT1; the off-network check found no clock pin driven by anything but the BUFG nets, so none is on a clock net); all 847 clock users are on the one BUFG net `spi_clk` (843 rising, 4 falling); the pre-pack script found no clock pin off the BUFG network. nextpnr-xilinx 0.8.2 times a falling-edge cell as a rising-edge one, so the clock is constrained at twice 40 MHz and every path in `spi`, full-cycle ones included, is checked against 12.5 ns. That is pessimistic for full-cycle paths and right for the half-cycle ones, but it also means the real half-cycle relation is not modelled. |
| Async preset | one `FDPE` (`PRE` = `cs_n`); nextpnr-xilinx does no recovery/removal check and no timing from the pad to it |
| Pull-ups | the FASM carries `PULLTYPE.PULLUP` on exactly 12 IOBs, the buttons, as the platform requires (checked by count). Task 7b's question for the open flow is answered by this file for this design |
| Utilisation (Yosys) | 1107 `FDRE` + 4 `FDRE_1` + 2 `FDSE` + 1 `FDPE`, 153 LUT2, 48 LUT3, 176 LUT4, 180 LUT5, 157 LUT6, 22 MUXF7, 20 CARRY4, 15 IBUF, 9 OBUF, 4 IOBUF, 2 BUFG |

## 4. Limitations: what was not timed, and what the open flow ignored or cannot express

Open flow (nextpnr-xilinx 0.8.2):

- No `set_input_delay` / `set_output_delay`: the pad-to-flop and flop-to-pad paths are not
  checked at all, so the 40 MHz input half-cycle and the output path have **no open-flow
  result**. The Vivado numbers are the only evidence, and the two tools place differently.
- No hold analysis (the result has hold `None`); no recovery/removal; no false paths, max delay
  or clock groups, so the exceptions of section 2 do not exist there and the cross-domain
  paths are only printed, unchecked. There is no way to state "these first stages are false
  paths" or "this is a half-cycle path".
- A clock with both edges in use is constrained at twice its frequency (a workaround in the
  pre-pack script), so full-cycle paths are over-constrained by a factor of two.
- The XDC `create_clock` is ignored (as in `docs/toolchains.md`): the clocks are set by the
  pre-pack script `ctx.addClock`, found by BUFG cell names.
- One seed was run; the result is placement-dependent (the mid-flow check printed 65 MHz and
  FAIL at 80 MHz before routing, 86.6 MHz after).

Both flows:

- The input and output delays are guesses (section 2). The input slack is 4.96 ns, so the
  slave tolerates an MCU that holds data up to about 11 ns after the falling edge; if the
  ESP32-S3's real delay is larger than that, the numbers fail, and nothing here measures it.
- Reads were checked at 16 MHz, writes at 40 MHz; 10 MHz reads are covered by the 16 MHz check
  for the outputs, and inputs of a read transaction (command, address) by the 40 MHz check.
- Only the Vivado "slow" and "fast" corners of the -1 part at nominal voltage and temperature,
  and no board-level skew: the PCB trace lengths of T14, U17 and the `dq` lines are not known.
- Vivado's `check_timing` leaves 12 + 9 ports to false paths; the setup of a button change
  against the sync clock is not timed (it is asynchronous by design).
- The CDC-4 critical above is real in the tool's classification; this document argues it is
  safe, it does not prove it. Metastability of the single-bit stages is not analysed.
- Stray SPI clock edges while `cs_n` is high are harmless by design: the asynchronous preset
  holds the slave in its initial phase and masks every strobe and the output enable, which is
  also why sharing pin T14 with the configuration clock is tolerable after configuration. That
  is a design argument, not tested on a board.
- Configuration (slave serial over the shared T14 and V15 pins, `CONFIG_MODE`, bitstream
  compression, the 80 MHz configuration clock) is not set or checked. T14 is used as an
  ordinary clock-capable input after configuration; nothing here checks that it is released
  by the configuration logic on the real board.

## 5. Does 40 MHz writes meet timing

Yes, in Vivado: setup WNS +4.96 ns on `spi` (about 11 ns of allowed MCU output delay on a 12.5 ns
half period, 6 ns assumed), with the logic-heavy paths (the handshake logic fed from `dq[3]`:
status 6 LUT levels, +5.56 ns; `running` +5.56; `fwd_toggle` +5.64; `cmd_done` +6.28; `req`
+6.59) comfortably inside it, and +10.4 ns on the worst path inside the domain. The worry that
the nibble-wide fan-out of `dq[3]` through the handshake would miss 40 MHz is not borne out. The
restructuring suggested earlier (acting on a registered copy one nibble later) is therefore not
needed for 40 MHz on this part; it would still be the first step if the real MCU output delay
turns out to be near the half period. No RTL was changed for timing in this task.

## 6. Conclusion

The link as designed is implementable on this FPGA with both toolchains at the firmware's
rates, subject to the assumptions: Vivado builds it and shows it met in every domain, including
the half-cycle inputs, the falling-edge outputs at the read clock, the asynchronous preset's
recovery/removal and the cross-domain exceptions. The open flow builds it, keeps the inversion
on a flip-flop type (`FDRE_1`, no clock through a LUT), emits the pull-ups, and meets its own
(pessimistic) clock checks, but it cannot check the interface timing at all, so for this link
the open flow produces a bitstream whose pad timing is unverified. Nothing has run on a real
Game Bub. The remaining risks are the assumed MCU timing (6 ns in, 3 ns out), the board skew,
and configuration of the shared pins.
