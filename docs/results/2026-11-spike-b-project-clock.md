# Spike B: the project clock under both toolchains

Date: 2026-11-24. Vivado 2025.2, yosys 0.71+77 (OSS CAD Suite 2026-11-22), nextpnr-xilinx 0.8.2 (openXC7 snap) with the repository's chip databases, prjxray-db from the same snap. Sections 1 to 6 used no hardware: every "the bitstream encodes X" statement there is a decode of the bitstream, not a measurement of a clock. The clock was then measured on a real Arty A7-35T (Task 7c, same day); that is the section "Measured on hardware", and where it settles or overturns a statement of the earlier sections, the statement now says so.

## Conclusion

The clock scheme of the design spec (a fractional `MMCME2_ADV` followed by a `BUFGCE`) builds, constrains and meets timing in all four builds (Arty and Game Bub, openXC7 and Vivado). Both flows accept `COMPENSATION="INTERNAL"` with `CLKFBOUT` wired to `CLKFBIN`, and openXC7 accepts the fractional divider, the wired `BUFGCE` enable and the button pull-ups. **Accepting is not working: on hardware the open-flow MMCM built this way never locks** ("Measured on hardware"): nextpnr-xilinx 0.8.2 disconnects the feedback input for `COMPENSATION="INTERNAL"`. With Vivado the clock is exact and locked.

It stands for Vivado without change. For openXC7 it builds, but **as specified it does not work on hardware** (no feedback, no lock, the gated clock never starts), and the MMCM bitstream is **not the same as Vivado's** in several further fields. Two of those differences were open questions when this was written; the measurement answered both, for open-flow bitstreams whose feedback had been corrected (diagnostic variants, not the design as specified):

1. **The fractional divider (odd integer part).** For `CLKOUT0_DIVIDE_F` 35.75, the setting `solve()` picks for 25.175 MHz from both 50 and 100 MHz, nextpnr-xilinx writes the fraction bits exactly as Vivado does, but a different integer counter and different fractional-waveform bits (section 1). Reasoning from the source of both tools (section 1), the output frequency is then probably still 25.1748 MHz and the duty cycle differs. **Measured: the frequency is right** (25,174,825 or 25,174,826 counts per 100,000,000 oscillator cycles, ideal 25,174,825.17) **and the duty cycle is about 51.5% high**, against Vivado's 50%.
2. **The loop filter and power register.** nextpnr-xilinx 0.8.2 writes a hard-coded lock/filter `TABLE` of 0x3d4 that is the right value only for CLKFBOUT_MULT_F 10, 17 and 18, and `POWER_REG` 0xffff where Vivado writes 0x9900. This changes loop bandwidth and possibly lock behaviour, not the frequency. The Game Bub (MULT 18) gets the correct table; the Arty (MULT 9) does not (section 1). **Measured: with the wrong table (MULT 9) the MMCM locked and did not lose lock in the observations made** (ten and sixty seconds, one board, room temperature); jitter was not measured.

Neither is a reason to change the solver: **the measurement found no need for a toolchain-specific solver rule**. What must change is the feedback in the open-flow build ("Measured on hardware", "What must change"); that is a follow-up, not done here. The paragraph as first written follows. ~~The decision on the solver (whether to restrict it for the open flow) waits for a hardware measurement~~ (made in Task 7c). Any restriction on this toolchain version would have to keep both the integer-part rule below and `MULT` in {10, 17, 18}, or wait for the upstream filter fix; the earlier draft's "fractions only on even integer parts" suggestion is withdrawn as stated (section 1, alternatives), because for the Game Bub it would trade a divider difference that is probably duty-cycle-only for a definitely wrong loop filter (MULT 54) and cost 16 ppm.

The open-flow timing result needs one qualification: nextpnr-xilinx does not propagate the input constraint through the MMCM, so the project clock was constrained from `achieved_hz` by a pre-pack script (section 4), and it does not time the BUFGCE enable path (section 5).

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

(`build/clock/gamebub-openxc7/top.fasm`.) Decoding the bitstream back with `bit2fasm` returns the same features (the round trip is in `build/clock/gamebub-open.decoded.fasm`). The test `test_open_flow_fractional_divider_is_in_the_fasm` asserts `FRAC_EN` and the fraction value.

**Comparison with Vivado: scope and result.** The bitstream of each built design was decoded with `bit2fasm` for both flows (`build/clock/<board>-{open,vivado}.decoded.fasm`) and every MMCME2_ADV feature at the site was compared as an integer (bit slices of one field summed; `tt_harness.clock_build.mmcm_values`). The test `test_open_and_vivado_mmcm_encodings_differ_as_documented` asserts exactly the sets below for both boards, so it fails if either toolchain changes. Everything not listed is equal in the two bitstreams (the input divider, `CLKFBOUT` high/low, the lock table `LKTABLE`, compensation, `LOCKREG3`, the clock output enables and the rest).

Differences, each board, open flow first (hex; "unused" means outputs CLKOUT1 to CLKOUT6, which the design does not use):

| Field | Game Bub (MULT 18) | Arty (MULT 9) | Cause |
|---|---|---|---|
| CLKOUT0 HIGH_TIME | 16 vs 17 | 16 vs 17 | odd-integer fractional divide (below) |
| CLKOUT0 FRAC_WF_R | 1 vs 0 | 1 vs 0 | same |
| CLKOUT5 FRACTIONAL FRAC_WF_F | 1 vs 0 | 1 vs 0 | same |
| CLKOUT5 FRACTIONAL PHASE_MUX_F | 3 vs 7 | 3 vs 7 | same |
| `TABLE` (loop filter) | equal, 0x3d4 | **0x3d4 vs 0x394** | nextpnr hard-codes 0x3d4 |
| `FILTREG1_RESERVED` | 0x3d4 vs 0x008 | 0x394 vs 0x008 | nextpnr writes the filter value here instead of 0x008 |
| `POWER_REG` | 0xffff vs 0x9900 | 0xffff vs 0x9900 | nextpnr enables everything |
| unused CLKOUT1 to CLKOUT4: HIGH_TIME, LOW_TIME | 0 vs 1 | 0 vs 1 | Vivado sets 1 |
| unused CLKOUT1 to CLKOUT4: NO_COUNT | 0 vs 1 | 0 vs 1 | Vivado sets it |
| unused CLKOUT5 and CLKOUT6: CLKOUT1 HIGH_TIME and LOW_TIME, FRACTIONAL NO_COUNT | 0 vs 1 | 0 vs 1 | same |

The same fields agree: `CLKOUT0 FRAC` = 6, `FRAC_EN` = 1, `EDGE` = 1, LOW_TIME 17, `CLKFBOUT` 9 + 9 (MULT 18) or the Arty's 9, `DIVCLK`. The Arty `TABLE` figures and the Game Bub's matching `TABLE` were checked against the decoded files (`tmp/alldiff.py` output, reproduced by the test); the proposed even-integer setting below was probed separately and has `TABLE` 0x3d4 against Vivado's 0x184.

**Cause: the fractional divider with an odd integer part.** Vivado's values follow the fractional-counter calculation of Xilinx's XAPP888 reference design (`mmcm_frac_count_calc` in `mmcm_drp_func.h`; the register layout is XAPP888 v1.8, Tables 2, 5 and 7: `FRAC`, `FRAC_EN`, `FRAC_WF_R` in CLKOUT0's ClkReg2, `PHASE_MUX_F` and `FRAC_WF_F` in CLKOUT5's ClkReg2). With `odd` the odd bit of the integer part, `f` the fraction in eighths and `oaf = 8*odd + f`:

```
HIGH = int/2 - (oaf <= 8)    LOW = int/2 - (oaf <= 9)    (int/2 rounded down)
PHASE_MUX_F = 4*odd + (f >> 1)
WF_R = 1 if 1 <= oaf <= 8     WF_F = 1 if 2 <= oaf <= 9
```

I reproduced the decoded Vivado values from these formulas by hand (the function file was read from a published copy of the 7-series header, `Learn-FPGA-Programming` `mmcm_pll_drp_func_7s_mmcm.vh`, lines 622 to 638):

| Divide | int, odd, f, oaf | HIGH, LOW | PHASE_MUX_F | WF_R, WF_F | Vivado decode |
|---|---|---|---|---|---|
| 35.75 | 35, 1, 6, 14 | 17, 17 | 4 + 3 = 7 | 0, 0 | 17, 17, 7, 0, 0 |
| 36.75 | 36, 0, 6, 6 | 17, 17 | 0 + 3 = 3 | 1, 1 | 17, 17, 3, 1, 1 |
| 35.125 | 35, 1, 1, 9 | 17 (HIGH), 16 (LOW) | 4 + 0 = 4 | 0, 1 | HIGH 17, LOW 16, 4, 0, 1 |

nextpnr-xilinx 0.8.2 (`xilinx/fasm.cc`, `write_mmcm_clkout`, lines 1584 to 1657) has no odd-integer branch: it splits the integer part as `high = floor(d/2)`, `low = floor(d) - high`, sets `edge` when they differ, and for a fractional CLKOUT0 or CLKFBOUT decrements both (`--high; --low;`, lines 1621 to 1623), writes `FRAC_WF_F` and `PHASE_MUX_F = frac >> 1` (only if non-zero), and always `FRAC_EN`, `FRAC_WF_R` and `FRAC` (lines 1651 to 1655). For 35.75 that is HIGH 16, LOW 17, EDGE, WF_R 1, WF_F 1, PHASE_MUX_F 3: the 36.75 row with HIGH one lower. For an even integer part it is the XAPP888 result (the 36.75 row), which is why even integer parts agree. Upstream `main` of openXC7/nextpnr-xilinx is the same in this respect (`xilinx/fasm.cc` lines 2901 to 2932).

**What that means for the output frequency (reasoning, not a measurement).** XAPP888 (page 2) says a HIGH or LOW count is a number of VCO cycles and the EDGE bit moves half a cycle from LOW to HIGH without changing their sum; the fractional bits (page 3) only adjust edge timing. If so, nextpnr's 35.75 encoding is Vivado's known-good 36.75 encoding with one HIGH count fewer, so its period is 36.75 - 1 = 35.75 VCO cycles: 900 / 35.75 = 25.1748 MHz, the frequency is right and only the duty cycle (and the exact edge positions) differ from Vivado's, a few percent off 50% (the reviewer's estimate was about 48.6%, or 50% if EDGE adds a half cycle as in integer mode; I did not derive it). When written this was likely but not established: the fractional counter's exact behaviour is not documented beyond the above. If the assumption were wrong the output would be 900 / 34.75 or 900 / 36.75, about 2.8% off. **The measurement ("Measured on hardware") confirms the reasoning: the period is 35.75 VCO cycles exactly, and the duty cycle is about 51.5% high**, not the 48.6% or 50% estimated above.

**What was probed** (`scripts/mmcm_encoding_probe.py` builds one tiny MMCM design per setting for the `xc7a100tcsg324-1` in both flows and decodes both bitstreams; `scripts/mmcm_encoding_compare.py` prints every differing feature; outputs in `build/fracprobe/<case>/`). The unused-output, `FILTREG1_RESERVED` and `POWER_REG` differences of the table above apply to every case, and `TABLE` differs whenever MULT is not 10, 17 or 18 (it is 0x3d4 whatever the multiplier; the table `filter_lookup_optimized` in `fasm.cc` lines 1965 to 1984 gives 0x3d4 at MULT 10, 17 and 18). Beyond those, the divider fields are:

| DIVCLK, MULT / DIVIDE_F | Divider fields |
|---|---|
| 1, 18 / 35.0 | equal |
| 1, 18 / 35.25, 35.5, 35.75 | `FRAC`, `FRAC_EN` equal; HIGH, WF_R, WF_F, PHASE_MUX_F differ. Vivado PHASE_MUX_F 5, 6, 7; open 1, 2, 3 |
| 1, 18 / 35.125 (odd, f = 1) | differ in another way: Vivado HIGH 17, LOW 16, WF_R 0, WF_F 1, PHASE_MUX_F 4; open HIGH 16, LOW 17, WF_R 1, WF_F 0, PHASE_MUX_F 0 (`frac >> 1` is 0, so nextpnr writes no WF_F or PHASE_MUX_F) |
| 1, 18 / 36.75 | equal |
| 1, 18.5 / 36 and 18.125 / 36 (fractional MULT, even integer part) | equal |
| 4, 53.875 / 26.75 (odd integer part 53 in MULT) | the same divergence on CLKFBOUT (HIGH, WF_R) |
| 4, 54.125 / 26.875 (both even integer parts) | equal, but `TABLE` 0x3d4 against Vivado's 0x184 (MULT 54) |

So the divider divergence occurs whenever a fractional divider (output or feedback) has an odd integer part.

**Alternatives, per the brief (the open flow did not reject the fractional MMCM, so this is for information).** From the solver's space:

| Class of setting | From 50 MHz | From 100 MHz |
|---|---|---|
| Solver's choice (odd-integer fractional divider) | DIVCLK 1, 18.0 / 35.75, -6.94 ppm | DIVCLK 1, 9.0 / 35.75, -6.94 ppm |
| Fractions only on even integer parts | DIVCLK 4, 54.125 / 26.875 (VCO 676.6), -23.09 ppm; divider fields equal, but **MULT 54 gets a wrong open-flow loop filter** (`TABLE` 0x3d4, Vivado 0x184) | DIVCLK 5, 58.375 / 46.375, +8.03 ppm (computed only; MULT 58 would also get a wrong table) |
| Integer MULT and DIVIDE only (MMCM) | DIVCLK 5, 63 / 25 (VCO 630), 25.200 MHz, **+993 ppm** (inside 0.1% only just) | DIVCLK 3, 34 / 45, +404.6 ppm |
| `PLLE2_ADV`, integer only (VCO 800 to 1600 MHz) | best is 25.000 MHz, -6951 ppm, outside 0.1% | DIVCLK 3, 37 / 49, -195.9 ppm |

The recommendation in the first version of this document, to restrict the solver to fractions on even integer parts, is **withdrawn**: it removes the divider difference but, for the Game Bub (MULT 18 today, MULT 54 under the rule) it replaces a difference that is probably cosmetic (duty cycle) with a definitely wrong loop filter, and it costs 16 ppm. A rule for this toolchain version would also have to keep `MULT` in {10, 17, 18} (where 0x3d4 is the right table) or wait for the upstream fix, which puts the filter value in `TABLE` and 0x8 in `FILTREG1_RESERVED` (`xilinx/fasm.cc` on `main`, lines 3344 to 3355, with the comment that they were previously swapped and that Vivado wants FILTREG1 = 0x8 and TABLE = the filter lookup). The measurement has since shown that the odd-integer encoding gives the right frequency and that the MMCM locks with the 0x3d4 table at MULT 9, so no such rule is proposed. `PLLE2_ADV` was not built: it cannot reach the frequency from 50 MHz, and from 100 MHz the MMCM options are better.

Two further upstream facts. Upstream `main` also writes `POWER_REG` 0x100 (integer counters only, as Vivado does for non-fractional configurations) where 0.8.2 writes 0xffff; Vivado's 0x9900 for a fractional configuration is what enables the fractional counters (the 0.8.2 source comment says so), so **a toolchain upgrade may break fractional mode** on the open flow, a follow-up to re-check on any upgrade (the new test would flag the change in the POWER_REG row). The 0.8.2 chip database and binary are the ones used here.

## 2. BUFGCE with a wired enable

Accepted. nextpnr-xilinx 0.8.2 reads Yosys's `BUFGCE` as its own cell type and packs it to a `BUFGCTRL` (`Constrained BUFGCTRL 'BUFGCE' to bel 'BUFGCTRL_X0Y0/BUFGCTRL'` on the Game Bub, `X0Y16` on the Arty). The FASM routes the fabric flop to the buffer's CE0 pin and does not invert it:

```
CLK_BUFG_BOT_R_X78Y100.CLK_BUFG_R_BUFGCTRL0_CE0.CLK_BUFG_IMUX20_0
CLK_BUFG_BOT_R_X78Y100.BUFGCTRL.BUFGCTRL_X0Y0.IN_USE
CLK_BUFG_BOT_R_X78Y100.BUFGCTRL.BUFGCTRL_X0Y0.ZINV_CE0
```

`ZINV_CE0` is the "not inverted" bit (the same bit is set on the two plain BUFGs, whose CE0 is tied high instead of routed, which is the difference). Vivado implements the BUFGCE the same way, as a `BUFGCTRL` whose `CE0` pin is `xilinxmultiregimpl1_reg/Q`. Test: `test_open_flow_bufgce_enable_is_routed`. This is a netlist and bitstream check; whether the enable gates the clock on silicon is not measured.

## 3. COMPENSATION="INTERNAL" with CLKFBOUT wired to CLKFBIN

Accepted by both flows without a warning or DRC about it; **in the open flow it does not work on hardware** ("Measured on hardware": nextpnr-xilinx 0.8.2 takes `INTERNAL` as leave to disconnect CLKFBIN, and the bitstream feeds that pin from the interconnect where Vivado's selects the dedicated `CLKFBOUT2IN` connection; the comparison in this section looked at the compensation bit only, which is the same). Vivado reads it back (`COMPENSATION INTERNAL`) and its DRC report (`drc.rpt`) had one warning, CFGBVS-1 (fixed in the platform since, see "Changes"; neither `CFGBVS` nor `CONFIG_VOLTAGE` is set; the platform does not set them, which a real board build should). Vivado's synthesis log has `Synth 8-7071` (unconnected MMCM outputs), `Synth 8-7023` (33 connections, 17 given) and `Netlist 29-345` ("SIM_DEVICE on instance 'BUFGCE' is 'ULTRASCALE'; it is being changed to match ... '7SERIES'"), all harmless. In the openXC7 FASM `COMP.Z_ZHOLD` is set; the Vivado bitstream decodes to the same feature, so both write the same INTERNAL compensation bit.

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
- **openXC7: not timed.** The only path nextpnr-xilinx reports for `project_free_clk` (the clock the two synchroniser flops are on) is flop to flop, `xilinxmultiregimpl00` to `$auto$ff.cc:337:slice$1197.D`; no path ends at the BUFGCTRL's CE. The source says why: in nextpnr-xilinx 0.8.2 `xilinx/arch.cc`, `Arch::getPortTimingClass` (lines 1252 to 1292) gives a `BUFGCTRL` only `TMG_COMB_INPUT` for `I0`/`I1` and `TMG_COMB_OUTPUT` for `O` (lines 1285 to 1289) and falls through to `TMG_IGNORE` (line 1291) for every other port, `CE0` and `CE1` included. So the enable path is ignored by the timing engine, by design, not by accident. The path is a single flop to a global buffer pin, so the risk is small, but it is not checked in this flow.

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

None to `clocking.py` or `build.py`. The Game Bub platform gained `CFGBVS VCCO` and `CONFIG_VOLTAGE 3.3` for the Vivado toolchain only (as `tt_harness/boards/gamebub/link_build.py` sets them in its build script), with a unit test that the generated XDC carries them, so the CFGBVS-1 DRC warning seen in the Game Bub Vivado build is gone for any build that uses the platform; re-running the Game Bub Vivado build afterwards, `drc.rpt` reports `Checks found: 0`. The platform's comment says "Task 7b must confirm the open flow actually emits the pull-up in its FASM": it does.

## Notes and loose ends

- LiteX's own `openxc7` toolchain object looks for `$CHIPDB/xc7a100tcsg324.bin` (no speed grade) while `scripts/setup_toolchains.py` builds `xc7a100tcsg324-1.bin`; `clock_build.py` drives the tools directly and uses the repository's names, so this does not matter here, but `platform.build()` without `run=False` on the openxc7 backend would not find the chip database.
- The first version of the platform set no `CFGBVS` / `CONFIG_VOLTAGE` and Vivado warned (CFGBVS-1); now added, see above.
- The `SIM_DEVICE` warning on `BUFGCE` is cosmetic.
- Not probed: odd-integer fractions on the feedback divider beyond MULT 53.875 (one case), and fractions of x.375, x.625, x.875 on the output divider. The formulas above predict Vivado's encoding for them; nextpnr's follows `write_mmcm_clkout`.
- Not done: `PLLE2_ADV` builds (not needed, see above); a second seed or a hold check in the open flow (nextpnr-xilinx does no hold analysis). The hardware measurement is the next section.

## Measured on hardware (Task 7c, 2026-11-24)

One board: the Digilent Arty A7-35T on host `pi7` of the public fpgas.online PS1 site (USB serial `210319A764F5`), room temperature, between 09:08 and 09:29 UTC. Probe: `tt_harness/boards/arty_clock_probe.py`, built by `arty_clock_probe_build.py`, run by `scripts/clock_probe_run.py`. Raw captures and per-run JSON are in `work/clock-probe/`, console logs in `tmp/run-*.log` (neither is committed).

**Result in one paragraph.** With Vivado the project clock is exact and locked in all four settings. With the open flow, **the design as specified does not work: the MMCM never asserts LOCKED, its output runs unlocked at about 39 MHz and the gated project clock never starts**, in all four settings, including the integer one and the one with the right filter table. The cause is the feedback path, not the divider encoding or the filter table: nextpnr-xilinx 0.8.2 disconnects `CLKFBIN` when `COMPENSATION="INTERNAL"`. With the feedback corrected and nothing else changed (diagnostic variants below, which are evidence and not the result), the open-flow clock has exactly the right frequency for the odd-integer fractional divider and locks with the wrong filter table.

### Method

- **Frequency.** `FreqMeter` (`tt_harness/core/freq_meter.py`): a free-running 32-bit counter in the measured clock's domain, held as Gray code, synchronised into the 100 MHz domain through two flops per bit, converted back to binary and differenced at the boundaries of a gate of 100,000,000 oscillator cycles (1 s). Nothing is reset between gates, so consecutive counts add up exactly. Resolution is +/-1 count per gate. The gate and the MMCM input are the same oscillator, so the reading is a pure ratio and the ideal count is exactly 100,000,000 x MULT / (DIVCLK x DIVIDE). `F` is the free-running BUFG output, `G` the gated BUFGCE output (`ProjectClock` enables it with `enable & locked`; enable is tied high), so G = F shows the clock was enabled for the whole gate.
- **Lock.** LOCKED through a two-flop synchroniser, reported at the end of each gate (`L`), and a saturating 8-bit count of its falling edges since configuration (`U`).
- **Duty cycle.** A level that is high from each rising edge of the free-running clock to the next falling edge (two flops and an XOR in fabric; the clock net itself cannot be routed to a data pin in the open flow) is sampled by the 100 MHz clock; `H` is the number of samples high per gate. The two clocks are coherent, so when the MMCM is locked the samples visit only 143 phases of the period for settings A and B, 49 for C and 4 for D: H / gate is then a multiple of 1/143 (0.70%), 1/49 (2.0%) or 1/4, biased by up to one step. It is reported with that resolution and is no measurement at all for D.
- **Report.** One 51-byte line per gate at 115200 baud: hexadecimal fields, an XOR checksum, and the gate's number since configuration. In every run all lines were whole, the checksums good, the gate numbers consecutive and the kernel's UART error counters unchanged. Gate 0, which contains the start-up, is kept apart; the tables are gates 1 to 10.
- **Constraints.** Both flows constrain the input clock and the MMCM output as in section 4, and both met timing in all builds. Vivado: input clock slack +4.747 ns, MMCM output +18.56 to +19.13 ns (the half-cycle path into the falling-edge flop), hold +0.074 and +0.106 ns, `check_timing` clean but for ports. nextpnr: 167.76 MHz against 100, and 204.92 and 204.00 MHz against 25. Every crossing between the domains goes through a `MultiReg` and is asynchronous by design. Vivado is told by LiteX's own constraints (a false path through the nets of flops marked `mr_ff`, and `ASYNC_REG` on both flops). nextpnr-xilinx applies no requirement between clocks and only reports the delays (about 1 ns). The Gray crossing needs the bits to arrive within one measured-clock period of each other; neither tool checks that, and at these delays it is met by a wide margin.
- **Vivado accepted all four settings** with no DRC error and no critical warning. One DRC warning in each: CFGBVS-1 (the LiteX Arty platform sets neither `CFGBVS` nor `CONFIG_VOLTAGE`). Setting B's limits: PFD 100 / 2 = 50 MHz (allowed 10 to 450), VCO 50 x 18 = 900 MHz (allowed 600 to 1200).

### The eight runs: the design as specified

Ideal counts: A and B 100e6 x 9 / 35.75 = 3,600,000,000 / 143 = 25,174,825.17; C 100e6 x 9 / 36.75 = 24,489,795.92; D 25,000,000 exactly. Counts are the minimum and maximum over gates 1 to 10. The last three columns are decoded from the bitstream that was loaded (`bit2fasm`): the loop filter `TABLE`; CLKOUT0's HIGH, LOW, EDGE, FRAC_WF_R, FRAC_WF_F, PHASE_MUX_F; and what feeds the MMCM's CLKFBIN.

| Setting (DIVCLK, MULT, DIVIDE) | Toolchain | Ideal | F min / max | G min / max | LOCKED | Lock losses | H / gate | TABLE | CLKOUT0 fields | CLKFBIN from |
|---|---|---|---|---|---|---|---|---|---|---|
| A (1, 9, 35.75) | Vivado | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 72/143 = 0.50350 | 0x394 | 17, 17, 1, 0, 0, 7 | CLKFBOUT2IN |
| A | openXC7 | 25,174,825.17 | **39,628,861 / 39,716,656** | **0 / 0** | **0** | 0 | 0.51491 to 0.51493 | 0x3d4 | 16, 17, 1, 1, 1, 3 | **CLK_IN3_INT** |
| B (2, 18, 35.75) | Vivado | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 72/143 = 0.50350 | 0x3d4 | 17, 17, 1, 0, 0, 7 | CLKFBOUT2IN |
| B | openXC7 | 25,174,825.17 | **39,602,693 / 39,651,878** | **0 / 0** | **0** | 0 | 0.51493 to 0.51496 | 0x3d4 | 16, 17, 1, 1, 1, 3 | **CLK_IN3_INT** |
| C (1, 9, 36.75) | Vivado | 24,489,795.92 | 24,489,795 / 24,489,796 | equal to F | 1 | 0 | 25/49 = 0.51020 | 0x394 | 17, 17, 0, 1, 1, 3 | CLKFBOUT2IN |
| C | openXC7 | 24,489,795.92 | **38,501,517 / 38,563,549** | **0 / 0** | **0** | 0 | 0.50095 to 0.50097 | 0x3d4 | 17, 17, 0, 1, 1, 3 | **CLK_IN3_INT** |
| D (1, 9, 36) | Vivado | 25,000,000 | 25,000,000 / 25,000,000 | equal to F | 1 | 0 | 2/4 (no resolution) | 0x394 | 18, 18, 0, 0, 0, 0 | CLKFBOUT2IN |
| D | openXC7 | 25,000,000 | **39,275,368 / 39,372,858** | **0 / 0** | **0** | 0 | 0.50043 to 0.50046 | 0x3d4 | 18, 18, 0, 0, 0, 0 | **CLK_IN3_INT** |

- Vivado: every gate is within one count of the ideal, G equals F in every gate, and the ten counts sum to within 0.25 of ten times the ideal. In gate 0 the free-running output was 231 to 237 counts short of a full gate and the gated output a further 323 to 364 (13 to 14.5 microseconds of clock).
- openXC7: LOCKED was low at the end of every gate and never fell, because it never rose; the gated output counted nothing; the free-running output wandered by 0.1 to 0.2% over the ten gates. Repeated for A over 60 gates: the same (F 39,462,334 to 39,532,272, LOCKED 0, G 0).
- The unlocked outputs keep the ratios of their dividers: multiplied by the divider they give a VCO near 1418 MHz (A), 1417 MHz (B), 1416 MHz (C) and 1415 MHz (D), above the 1200 MHz limit. That is what a loop does when its phase detector sees a reference and no feedback.

### Why the open flow does not lock

- The decoded bitstreams differ in the multiplexer that feeds the MMCM's CLKFBIN pin. Vivado: `CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_LR_LOWER_B_CLKFBOUT2IN`, the dedicated connection from the MMCM's own CLKFBOUT. openXC7: `CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_L_LOWER_B_CLK_IN3_INT`, a pin of the fabric interconnect, and no pip takes CLKFBOUT anywhere.
- The netlist given to nextpnr is right: `pc_clkfb` joins CLKFBOUT and CLKFBIN in the Yosys JSON. After routing, a post-route script shows the net `pc_clkfb` with its driver and **no users**. nextpnr-xilinx 0.8.2 takes CLKFBIN off the net when `COMPENSATION` is `INTERNAL` and ties the pin off through the interconnect, without a warning.
- **Variant `fbfix`** (evidence about the cause): the same four open-flow builds with that one multiplexer rewritten in the FASM to Vivado's `CLKFBOUT2IN` and the bitstream regenerated. The decoded bitstream differs from the stock one in exactly that one feature (compared for A and D). All four lock.
- **Variant `zhold`** (a candidate fix for a follow-up): the same build with the MMCM's `COMPENSATION` set to `ZHOLD` in the nextpnr pre-pack script and no FASM edit. nextpnr then keeps the connection and selects `CLKFBOUT2IN` itself. For D the decoded bitstream equals the `fbfix` one but for one left-over interconnect pip, and it carries the same compensation feature (`COMP.Z_ZHOLD`) as the stock open build and as Vivado's INTERNAL build.

| Setting | Variant (openXC7) | Ideal | F min / max | G | LOCKED | Lock losses | H / gate | TABLE |
|---|---|---|---|---|---|---|---|---|
| A | fbfix | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 0.51732 to 0.51739 (74/143 is 0.51748) | 0x3d4, wrong for MULT 9 |
| B | fbfix | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 0.51728 to 0.51733 | 0x3d4, right for MULT 18 |
| C | fbfix | 24,489,795.92 | 24,489,795 / 24,489,796 | equal to F | 1 | 0 | 25/49 = 0.51020 | 0x3d4, wrong |
| D | fbfix | 25,000,000 | 25,000,000 / 25,000,000 | equal to F | 1 | 0 | 2/4 | 0x3d4, wrong |
| A | zhold | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 0.51730 to 0.51737 | 0x3d4, wrong |
| D | zhold | 25,000,000 | 25,000,000 / 25,000,000 | equal to F | 1 | 0 | 2/4 | 0x3d4, wrong |
| A, 60 gates | zhold | 25,174,825.17 | 25,174,825 / 25,174,826 | equal to F | 1 | 0 | 0.51725 to 0.51736 | 0x3d4, wrong |

Over the 60 gates the counts sum to within 0.51 of sixty times the ideal.

### The open questions, answered

1. **Is the open-flow frequency right for a fractional divider with an odd integer part?** Yes, exactly, once the MMCM has feedback: 25,174,825 or 25,174,826 per gate against 25,174,825.17, the same readings as Vivado's, for nextpnr's encoding (HIGH 16, LOW 17, WF_R 1, WF_F 1, PHASE_MUX_F 3). The 2.8% error feared in section 1 would have read 24.49e6 or 25.90e6. In the design as specified the question does not arise, because nothing locks.
2. **Does the MMCM lock and stay locked with the wrong filter table?** With feedback, yes in what was observed: MULT 9 with `TABLE` 0x3d4 (Vivado 0x394), `FILTREG1_RESERVED` 0x394 and `POWER_REG` 0xffff was locked from the first gate and showed no falling edge of LOCKED in 10 s (A, C, D) or in 60 s (A). What this does not show: it is one board, at one room temperature, for at most a minute, in a small quiet design. A wrong loop filter changes bandwidth and jitter; neither was measured, LOCKED is a coarse indicator, and lock over temperature, supply noise or a busier design is not established. The table should still be corrected when the toolchain allows.
3. **Duty cycle.** Vivado: 72 of 143 phases (A, B) and 25 of 49 (C) sampled high, which is 50% within the one-step resolution. nextpnr's odd-integer encoding (A, B), locked: 74 of 143, 51.7%, two steps above Vivado's. The unlocked stock runs happen to give a finer figure, because an unlocked clock is not coherent with the sampler: 51.49% for A and B, against 50.10% (C) and 50.04% (D) for the encodings that agree with Vivado's, which also bounds the method's own asymmetry at about 0.1%. So the open flow's 35.75 is **about 51.5% high**, roughly 0.6 ns more than half of the 39.7 ns period, and not the 48.6% estimated in section 1. That is harmless for logic clocked on one edge; logic that uses both edges loses that much from one half-period.

### What must change (not changed in this task)

- **The solver needs no toolchain-specific rule.** Odd-integer fractional dividers give the right frequency in the open flow, and the hard-coded filter table did not prevent lock at MULT 9.
- **The open-flow build must not present `COMPENSATION="INTERNAL"` to nextpnr-xilinx 0.8.2.** The `zhold` variant shows one way that worked on this board: with `ZHOLD` and CLKFBOUT wired to CLKFBIN, nextpnr selects the dedicated feedback and writes the same feedback and compensation bits as Vivado. Whether to set that in `ProjectClock` per toolchain or in the pre-pack script, and what Vivado makes of `ZHOLD` with a direct connection, is for the follow-up. `clocking.py` still says `INTERNAL`, so every open-flow build of `ProjectClock` is broken until then.
- **A gate that would have caught this:** the decoded bitstream's CLKFBIN source (`arty_clock_probe_build.feedback_source`) must be `CLKFBOUT2IN`. Section 3's comparison covered the MMCM's own fields and missed the routing multiplexer beside them.
- **The Game Bub** was not measured (no board), and its bitstream was not decoded again here. Its open-flow build uses the same `ProjectClock` and the same nextpnr, so the same failure is expected.
- **Found on the way: an instantiated `FDRE` with `IS_C_INVERTED=1`.** nextpnr-xilinx 0.8.2 ignored it: the first probe build's FASM had no `CLKINV` in any slice, so the probe's falling-edge flop would have been a rising-edge one. The binary reads the inversion from a parameter named `IS_CLK_INVERTED`; setting that in the pre-pack script gives one `CLKINV` slice, and the duty readings near 50% (an uninverted flop would give 100%) show it took effect. Flops that Yosys infers from `negedge` are not affected in a small check made here (Yosys emits `FDRE_1`, and nextpnr's FASM then has `CLKINV`); that check was a build and a look at the FASM, not a run on the board.

### State the board was left in

FPGA: the `zhold` variant of setting A (openXC7, bitstream sha256 `284a17cfd316…`), which goes on sending one line a second on the UART. Board host: no `tt-harness-probe-*` directory, no process of ours, the UART unheld, disk use unchanged (3.2 MB of 458 MB); `sudo` was not used and nothing was installed. Before the session (by hand) and before every load (`board_activity`) the host was checked for other users: one other terminal, idle since the previous day, and nothing holding the UART.
