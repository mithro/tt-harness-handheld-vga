# Spike B: the project clock under both toolchains

Date: 2026-11-24. Vivado 2025.2, yosys 0.71+77 (OSS CAD Suite 2026-11-22), nextpnr-xilinx 0.8.2 (openXC7 snap) with the repository's chip databases, prjxray-db from the same snap. No hardware was used: every "the bitstream encodes X" statement below is a decode of the bitstream, not a measurement of a clock.

## Conclusion

The clock scheme of the design spec (a fractional `MMCME2_ADV` followed by a `BUFGCE`) builds, constrains and meets timing in all four builds (Arty and Game Bub, openXC7 and Vivado). Both flows accept `COMPENSATION="INTERNAL"` with `CLKFBOUT` wired to `CLKFBIN`, and openXC7 accepts the fractional divider, the wired `BUFGCE` enable and the button pull-ups.

It stands for Vivado without change. For openXC7 it stands with one open risk and one rule:

- **Risk (key result).** For a fractional divider whose integer part is odd (`CLKOUT0_DIVIDE_F` 35.75, the setting `solve()` picks for 25.175 MHz from both 50 and 100 MHz), nextpnr-xilinx writes the fraction bits exactly as Vivado does, but writes a different integer counter value and different fractional waveform bits (HIGH_TIME 16 against 17, WF_R and WF_F set, PHASE_MUX_F 3 against 7). The fraction is not dropped or rounded. Which of the two encodings gives 25.1748 MHz on silicon cannot be settled without hardware, and I could not derive it from first principles. Treat the open-flow output frequency as **unverified** until Task 10 measures it.
- **Rule.** Where the integer part of `CLKOUT0_DIVIDE_F` or `CLKFBOUT_MULT_F` is even, or the value is whole, the two flows' encodings are identical (tested: 36.75, 36.0, 18.5, 18.125, 54.125, 26.875). So a solver restricted to "a fraction only on an even integer part" gives bit-identical MMCM encodings in both flows, at a small cost in accuracy (below). This is a change to `solve()` that the coordinator should decide on; I did not make it.

Everything else asked about is in the sections below. The open-flow timing result needs one qualification: nextpnr-xilinx does not propagate the input constraint through the MMCM, so the project clock was constrained from `achieved_hz` by a pre-pack script (section 4), and it does not time the BUFGCE enable path (section 5).

## Results table

Part names: Arty `xc7a35ticsg324-1L` (Vivado) and `xc7a35tcsg324-1` (openXC7), Game Bub `xc7a100tcsg324-1`. Setting from `solve(clk_in, 25_175_000)`: achieved 25 174 825.175 Hz, -6.94 ppm. Slack is the worst setup slack in the domain.

| Board | Tool | Setting | Bitstream | Input clock | MMCM output clock | Result |
|---|---|---|---|---|---|---|
| Arty (100 MHz) | Vivado | DIVCLK 1, MULT 9.000, DIV 35.750, VCO 900 MHz | yes | 10.000 ns, slack +7.464 ns, hold +0.252 | 39.722 ns (25.1749 MHz), slack +36.732 ns, hold +0.089 | pass, check_timing clean except ports |
| Arty | openXC7 | same | yes | 100 MHz target, Fmax 264.48 | 25.17 MHz target, BUFGCE domain 275.63, BUFG domain 1179.25 | pass |
| Game Bub (50 MHz) | Vivado | DIVCLK 1, MULT 18.000, DIV 35.750, VCO 900 MHz | yes | 20.000 ns, slack +17.606 ns, hold +0.122 | 39.722 ns, slack +36.476 ns, hold +0.131 | pass |
| Game Bub | openXC7 | same | yes | 50 MHz target, Fmax 274.20 | 25.17 MHz target, BUFGCE domain 268.38, BUFG domain 1057.08 | pass |

Logs, all under `build/clock/` (git-ignored; rebuilt by `uv run pytest tests/test_clock_build.py`): `<board>-openxc7/{build.log,nextpnr.log,top.fasm,top.bit}`, `<board>-vivado/{build.log,vivado.log,timing.rpt,check_timing.rpt,clocks.rpt,drc.rpt,ce_path.rpt,io.rpt,top.bit}`.

The design: board oscillator, `ProjectClock` at 25.175 MHz, `enable` from a button (Arty `user_btn0`, Game Bub `buttons.a`), a 24-bit counter on the project clock (Arty LED0, Game Bub test point 0 and 2) and a 24-bit counter on the input clock through its own BUFG (LED1, test points 1 and 3). On the Game Bub all twelve buttons reach logic (eleven through a synchroniser). Driver: `tt_harness/clock_build.py`; tests: `tests/test_clock_build.py`.

Why a separate driver and not `build_compare`: `build.py` is built around a project in a slot with clocks named sys, pclk and project, found from a recorder's pclk register. The clocks here are "the input" and "the MMCM output", both made by primitives, so `clock_build.py` reuses the tool runner (`verilate.run`), the error and `TimingDomain` types and the nextpnr "Max frequency" regex from `build.py`, and writes its own pre-pack script, Vivado script and parsers. The design and its XDC come from the real LiteX platforms (`Platform.build(run=False)`), so the platform's pins and `PULLTYPE` reach the tools as LiteX writes them.

## 1. Fractional MMCM in the open flow

Accepted, and the fraction is encoded. nextpnr-xilinx placed `MMCME2_ADV` (`Constrained MMCME2_ADV_MMCME2_ADV 'MMCME2_ADV' to bel 'MMCME2_ADV_X0Y1/MMCME2_ADV'` on the Game Bub, `X1Y1` on the Arty) with no warning. The FASM for the 35.75 divider contains:

```
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT2_FRAC_EN[0]
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT2_FRAC_WF_R[0]
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT2_FRAC[2:0] = 3'b110      (6/8 = 0.75)
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT1_HIGH_TIME[5:0] = 6'b010000
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT1_LOW_TIME[5:0]  = 6'b010001
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT0_CLKOUT2_EDGE[0]
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT5_CLKOUT2_FRACTIONAL_FRAC_WF_F[0]
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKOUT5_CLKOUT2_FRACTIONAL_PHASE_MUX_F[1:0] = 2'b11
CMT_TOP_R_LOWER_B_X8Y61.MMCME2_ADV.CLKFBOUT_CLKOUT1_HIGH_TIME[5:0] = 6'b001001   (9 + 9 = 18, MULT 18.000)
```

(`build/clock/gamebub-openxc7/top.fasm`.) Decoding the bitstream back with `bit2fasm` returns the same features (the round trip is in `build/clock/gamebub-openxc7.decoded.fasm`). The test `test_open_flow_fractional_divider_is_in_the_fasm` asserts `FRAC_EN` and the fraction value.

**Comparison with Vivado.** The Vivado bitstream of the same design, decoded the same way (`build/clock/gamebub-vivado.decoded.fasm`), has the same `FRAC_EN` and `FRAC = 6` but different counter and waveform fields. Field values for CLKOUT0_DIVIDE_F = 35.75, MULT 18.000, DIVCLK 1 (decoded to integers, open flow first):

| Field | openXC7 | Vivado |
|---|---|---|
| CLKOUT0 HIGH_TIME | 16 | 17 |
| CLKOUT0 LOW_TIME | 17 | 17 |
| CLKOUT0 EDGE | 1 | 1 |
| CLKOUT0 FRAC, FRAC_EN | 6, 1 | 6, 1 |
| CLKOUT0 FRAC_WF_R | 1 | 0 |
| CLKOUT5 FRACTIONAL FRAC_WF_F | 1 | 0 |
| CLKOUT5 FRACTIONAL PHASE_MUX_F | 3 | 7 |
| CLKOUT5 FRACTIONAL NO_COUNT (unused CLKOUT5) | 0 | 1 |
| CLKFBOUT HIGH/LOW, DIVCLK | identical | identical |

The last-but-one row also differs for an integer divider (35.0), where every other field is identical: it concerns the unused CLKOUT5 and is not a clock-frequency difference as far as I can tell. The test `test_open_and_vivado_encode_the_fractional_divider_differently` asserts exactly this set of differences, so it fails if either flow changes.

**What was probed** (`scripts/mmcm_encoding_probe.py`, `scripts/mmcm_encoding_compare.py`; one tiny MMCM design per setting on the `xc7a100tcsg324-1`, both flows, bitstreams decoded; outputs in `build/fracprobe/<case>/`; all with the divider on CLKOUT0, DIVCLK 1 unless stated):

| MULT / DIVIDE_F | Result |
|---|---|
| 18 / 35.0 | identical (except the unused-CLKOUT5 bit) |
| 18 / 35.25, 35.5, 35.75 | FRAC and FRAC_EN identical; HIGH_TIME, WF_R, WF_F, PHASE_MUX_F differ. Vivado PHASE_MUX_F is 5, 6, 7 and the open flow 1, 2, 3 |
| 18 / 36.75 | identical |
| 18.5 / 36, 18.125 / 36 (fractional MULT, even integer part) | identical |
| DIVCLK 4, 53.875 / 26.75 (odd integer part 53 in MULT) | CLKFBOUT HIGH_TIME and WF_R differ: the same divergence, on the feedback divider |
| DIVCLK 4, 54.125 / 26.875 (both even integer parts) | identical |

So the divergence is whenever a fractional divider (output or feedback) has an odd integer part. The pattern in Vivado's encoding for the odd case is HIGH = LOW = 17, WF_R and WF_F clear, PHASE_MUX_F = 4 + fraction/2; the open flow instead uses HIGH = 16, WF_R and WF_F set, PHASE_MUX_F = fraction/2, which is how both flows encode the even case. I could not find the 7-series fractional-counter register semantics (XAPP888's algorithm) offline, and the two encodings cannot be reconciled by counting cycles with an assumed meaning for the WF bits: under the readings I tried the open flow's period is either equal to or 1 VCO cycle (about 2.8%) off from Vivado's. So I cannot say that it is wrong, only that it is not what the vendor tool writes. **To close it:** measure the Game Bub or Arty project clock from an open-flow bitstream (a frequency counter or the logic analyser on the 24-bit counter's LED), which is the first thing Task 10 should do for an openXC7 bitstream.

**Alternatives, per the brief (the open flow did not reject the fractional MMCM, so this is for information).** From the solver's space:

| Class of setting | From 50 MHz | From 100 MHz |
|---|---|---|
| Solver's choice (odd-integer fractional divider) | DIVCLK 1, 18.0 / 35.75, -6.94 ppm | DIVCLK 1, 9.0 / 35.75, -6.94 ppm |
| Fractions only on even integer parts (identical encodings) | DIVCLK 4, 54.125 / 26.875 (VCO 676.6), -23.09 ppm | DIVCLK 5, 58.375 / 46.375, +8.03 ppm: 58 and 46 are even |
| Integer MULT and DIVIDE only (MMCM) | DIVCLK 5, 63 / 25 (VCO 630), 25.200 MHz, **+993 ppm** (inside 0.1% only just) | DIVCLK 3, 34 / 45, +404.6 ppm |
| `PLLE2_ADV`, integer only (VCO 800 to 1600 MHz) | best is 25.000 MHz, -6951 ppm, outside 0.1% | DIVCLK 3, 37 / 49, -195.9 ppm |

The DIVCLK 4, 54.125 / 26.875 row was built in both flows and encodes identically (above). The 100 MHz row of that class was computed only. `PLLE2_ADV` was not built: it is not needed on the Game Bub (it cannot reach the frequency), and on the Arty an MMCM setting that is better and encodes identically exists. No integer-only setting from 50 MHz is within 100 ppm; the best, 25.2 MHz, is within the project's 0.1% limit but 0.099% off is a poor clock for a video capture.

## 2. BUFGCE with a wired enable

Accepted. nextpnr-xilinx 0.8.2 reads Yosys's `BUFGCE` as its own cell type and packs it to a `BUFGCTRL` (`Constrained BUFGCTRL 'BUFGCE' to bel 'BUFGCTRL_X0Y0/BUFGCTRL'` on the Game Bub, `X0Y16` on the Arty). The FASM routes the fabric flop to the buffer's CE0 pin and does not invert it:

```
CLK_BUFG_BOT_R_X78Y100.CLK_BUFG_R_BUFGCTRL0_CE0.CLK_BUFG_IMUX20_0
CLK_BUFG_BOT_R_X78Y100.BUFGCTRL.BUFGCTRL_X0Y0.IN_USE
CLK_BUFG_BOT_R_X78Y100.BUFGCTRL.BUFGCTRL_X0Y0.ZINV_CE0
```

`ZINV_CE0` is the "not inverted" bit (the same bit is set on the two plain BUFGs, whose CE0 is tied high instead of routed, which is the difference). Vivado implements the BUFGCE the same way, as a `BUFGCTRL` whose `CE0` pin is `xilinxmultiregimpl1_reg/Q`. Test: `test_open_flow_bufgce_enable_is_routed`. This is a netlist and bitstream check; whether the enable gates the clock on silicon is not measured.

## 3. COMPENSATION="INTERNAL" with CLKFBOUT wired to CLKFBIN

Accepted by both flows without a warning or DRC about it. Vivado reads it back (`COMPENSATION INTERNAL`) and its DRC report (`drc.rpt`) has one warning, CFGBVS-1 (neither `CFGBVS` nor `CONFIG_VOLTAGE` is set; the platform does not set them, which a real board build should). Vivado's synthesis log has `Synth 8-7071` (unconnected MMCM outputs), `Synth 8-7023` (33 connections, 17 given) and `Netlist 29-345` ("SIM_DEVICE on instance 'BUFGCE' is 'ULTRASCALE'; it is being changed to match ... '7SERIES'"), all harmless. In the openXC7 FASM `COMP.Z_ZHOLD` is set; the Vivado bitstream decodes to the same feature, so both write the same INTERNAL compensation bit.

## 4. Clock constraints

**Vivado.** Only the input clock is constrained (the LiteX platform's `create_clock -name clk50 -period 20.0` or `clk100 ... 10.0`). Vivado derives both MMCM outputs itself (`report_clocks`):

```
Clock          Period(ns)  Waveform(ns)    Attributes  Sources
clk50          20.000      {0.000 10.000}  P           {clk50}
pc_clkfb       20.000      {0.000 10.000}  P,G,A       {MMCME2_ADV/CLKFBOUT}
pc_clkout_raw  39.722      {0.000 19.861}  P,G,A       {MMCME2_ADV/CLKOUT0}
```

The output period 39.722 ns is 1 / 25.1748 MHz (the report prints three decimals; 39.7222 exactly). The feedback clock `pc_clkfb` has no paths, as expected. `check_timing` has 0 in every category except `no_input_delay` (12 on the Game Bub, buttons) and `no_output_delay` (4): those are ports, not clock domains, and `build.py` leaves them out of its gate for the same reason. Same counts are in `check_timing.rpt`. Both domains meet setup and hold (table above). `Timing constraints are not met.` does not appear.

**openXC7.** The input constraint does not propagate. `create_clock` in the XDC is accepted and ignored (as `docs/toolchains.md` already says for the plain BUFG case) and nothing is derived through the MMCM, so nextpnr-xilinx times every clock at its default 12 MHz unless told otherwise (`Annotating ports with timing budgets for target frequency 12.00 MHz`). As the compare build does, the pre-pack script (`ctx.addClock` in `clocks.py` in each build directory) constrains the output net of every clock buffer: the buffer fed by the input IBUF at the board's frequency, the two buffers fed by the MMCM at `achieved_hz`:

```
tt-harness clock: domain project:BUFG net 'project_free_clk' 25.174825174825177 MHz (buffer BUFG type BUFG driven by MMCME2_ADV)
tt-harness clock: domain input net 'sys_clk' 50.0 MHz (buffer sys_bufg type BUFG driven by IBUF)
tt-harness clock: domain project:BUFGCE net 'pc_clk0' 25.174825174825177 MHz (buffer BUFGCE type BUFGCE driven by MMCME2_ADV)
Info: Max frequency for clock          'sys_clk': 274.20 MHz (PASS at 50.00 MHz)
Info: Max frequency for clock 'project_free_clk': 1057.08 MHz (PASS at 25.17 MHz)
Info: Max frequency for clock          'pc_clk0': 268.38 MHz (PASS at 25.17 MHz)
```

(Game Bub final report. Arty: 264.48 / 1179.25 / 275.63 MHz.) nextpnr prints the target to two decimals. The gate fails if a reported clock is not one the pre-pack constrained, or is timed against a different target. The open flow's numbers for the input clock include the 24-bit counter's carry chain on the global clock and so are not comparable with Vivado's slack; neither is a statement about the 25.1748 MHz being the real output frequency, since the pre-pack script takes that from the solver. The `project_free_clk` figure on the Game Bub's first pre-route report (5000 MHz) is only the cap on a flop-to-flop path with no logic.

## 5. The CE path

- **Vivado: timed.** `report_timing -to BUFGCE/CE0` finds the path from the second synchroniser flop to the buffer. Arty (`build/clock/arty-vivado/ce_path.rpt`):
  ```
  tt-harness ce path: slack 36.732 from xilinxmultiregimpl1_reg/C to BUFGCE/CE0 clock pc_clkout_raw requirement 39.722
  ```
  Game Bub: slack 36.476, requirement 39.722. It is also the worst setup path of the whole project-clock domain, so the domain slack above is the enable path's. The first synchroniser flop's input (from the button and from `LOCKED`) is a false path to a `mr_ff` cell, as LiteX's `MultiReg` constraints say.
- **openXC7: not timed (inferred).** The only path nextpnr-xilinx reports for `project_free_clk` (the clock the two synchroniser flops are on) is flop to flop, `xilinxmultiregimpl00` to `$auto$ff.cc:337:slice$1197.D`; no path ends at the BUFGCTRL's CE. nextpnr-xilinx has no timing endpoint for that pin. I did not find a log line that says so outright: this is the absence of such a path in the critical path reports, not a statement from the tool. The path is a single flop to a global buffer pin, so the risk is small, but it is not checked in this flow.

## 6. Game Bub button pull-ups

Both flows honour `PULLTYPE=PULLUP`.

- **Vivado:** all twelve buttons read back `PULLUP` after implementation (`tt-harness port:` lines from `get_property PULLTYPE`), the clock and test points have none:
  ```
  tt-harness port: buttons_a LOC IOB_X1Y88 IOSTANDARD LVCMOS33 PULLTYPE PULLUP
  ... (all twelve)
  tt-harness port: clk50 LOC IOB_X0Y76 IOSTANDARD LVCMOS33 PULLTYPE 
  ```
  and the Vivado bitstream decodes with `PULLTYPE.PULLUP` on the button IOBs.
- **openXC7:** the FASM has twelve `PULLTYPE.PULLUP` lines, one per button IOB tile and half, and `PULLTYPE.NONE` on the clock and the four test points:
  ```
  RIOB33_X57Y87.IOB_Y0.PULLTYPE.PULLUP        (buttons_a)
  LIOB33_X0Y75.IOB_Y0.PULLTYPE.NONE           (clk50)
  ```
  Test: `test_open_flow_gamebub_button_pullups` (count is 12). No alternative (a `PULLUP` primitive, the `PULLUP` property form) was needed, so none was tried. The nextpnr-xilinx 0.8.2 XDC reader takes `set_property PULLTYPE PULLUP [get_ports {...}]` as LiteX writes it.

## Changes outside the new files

None to `clocking.py`, the platform or `build.py`. The Game Bub platform's comment says "Task 7b must confirm the open flow actually emits the pull-up in its FASM": it does.

## Notes and loose ends

- LiteX's own `openxc7` toolchain object looks for `$CHIPDB/xc7a100tcsg324.bin` (no speed grade) while `scripts/setup_toolchains.py` builds `xc7a100tcsg324-1.bin`; `clock_build.py` drives the tools directly and uses the repository's names, so this does not matter here, but `platform.build()` without `run=False` on the openxc7 backend would not find the chip database.
- Vivado gives a CFGBVS warning for the Game Bub design: the platform sets no `CFGBVS` / `CONFIG_VOLTAGE`. Worth adding before a bitstream goes on the board.
- The `SIM_DEVICE` warning on `BUFGCE` is cosmetic.
- Not done: `PLLE2_ADV` builds (not needed, see above); a second seed or a hold check in the open flow (nextpnr-xilinx does no hold analysis); any hardware measurement.
