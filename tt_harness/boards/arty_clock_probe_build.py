# SPDX-License-Identifier: Apache-2.0
"""Builds of the Arty clock probe (`arty_clock_probe`), and what their bitstreams program.

`build` makes one probe with either toolchain through `tt_harness.clock_build` and its timing
gate and feedback gate. `programmed_mmcm` and `feedback_source` say what a bitstream
programs. (Task 7c's diagnostic variants, a FASM rewrite and a pre-pack ZHOLD, were removed
in Task 7d, when `ProjectClock` itself got COMPENSATION="ZHOLD".)
"""

from __future__ import annotations

from pathlib import Path

from tt_harness import clock_build as cb
from tt_harness import mmcm_fasm
from tt_harness.boards import arty_clock_probe as probe

# Builds -----------------------------------------------------------------------------------------

# nextpnr-xilinx 0.8.2 takes a flip-flop's clock inversion from a parameter IS_CLK_INVERTED,
# which no 7-series flip-flop has, and ignores the FDRE's own IS_C_INVERTED: without this the
# falling-edge flop of the waveform replica is built as a rising-edge one (no CLKINV in the
# FASM) and the replica is a constant 1. Vivado reads IS_C_INVERTED from the Verilog.
PREPACK_CLKINV = f"""
for name, cell in ctx.cells:
    if name == {probe.REPLICA_FALL!r}:
        cell.setParam("IS_CLK_INVERTED", "1")
        print(f"tt-harness clkinv: {{name}} type {{cell.type}}")
"""


def build(
    name: str,
    toolchain: str,
    out_dir: Path,
    gate_cycles: int = probe.GATE_CYCLES,
    prepack_extra: str = "",
) -> cb.Build:
    """Build the probe for the setting `name` with `toolchain` and pass the timing gate.

    Returns the `clock_build.Build`; `facts["domains"]` holds the per-domain timing. Raises
    `BuildError` or `TimingError` as `clock_build` does. `prepack_extra` (open flow only) is
    more Python for the nextpnr pre-pack script, for diagnostic variants.
    """
    s = probe.setting(name)

    def top(platform, board):
        return probe.ClockProbe(platform, s, gate_cycles)

    if toolchain == "openxc7":
        job = cb.openxc7_build(
            cb.ARTY, out_dir, top=top, prepack_extra=PREPACK_CLKINV + prepack_extra
        )
        if f"tt-harness clkinv: {probe.REPLICA_FALL} " not in job.facts["nextpnr"]:
            raise cb.BuildError(f"the pre-pack script did not find {probe.REPLICA_FALL}", job.log)
        job.facts["domains"] = cb.parse_nextpnr(job.facts["nextpnr"], cb.ARTY, s, job.log)
    elif toolchain == "vivado":
        job = cb.vivado_build(cb.ARTY, out_dir, top=top)
        job.facts["domains"] = cb.parse_vivado(job.facts["vivado"], cb.ARTY, s, job.log)
    else:
        raise ValueError(f"toolchain must be one of {probe.TOOLCHAINS}, not {toolchain!r}")
    missed = {k: d for k, d in job.facts["domains"].items() if not d.met}
    if missed:
        raise cb.TimingError(f"probe {name} {toolchain}: timing not met in {missed}", job.log)
    return job


# The MMCM fields that tie a measurement to what was programmed, in the order reported.
MMCM_FIELDS = (
    "DIVCLK_DIVCLK_HIGH_TIME",
    "DIVCLK_DIVCLK_LOW_TIME",
    "DIVCLK_DIVCLK_NO_COUNT",
    "CLKFBOUT_CLKOUT1_HIGH_TIME",
    "CLKFBOUT_CLKOUT1_LOW_TIME",
    "CLKOUT0_CLKOUT1_HIGH_TIME",
    "CLKOUT0_CLKOUT1_LOW_TIME",
    "CLKOUT0_CLKOUT2_EDGE",
    "CLKOUT0_CLKOUT2_FRAC_EN",
    "CLKOUT0_CLKOUT2_FRAC",
    "CLKOUT0_CLKOUT2_FRAC_WF_R",
    "CLKOUT5_CLKOUT2_FRACTIONAL_FRAC_WF_F",
    "CLKOUT5_CLKOUT2_FRACTIONAL_PHASE_MUX_F",
    "TABLE",
    "FILTREG1_RESERVED",
    "LKTABLE",
    "POWER_REG_POWER_REG_POWER_REG",
)


def programmed_mmcm(bitstream: Path) -> dict[str, int]:
    """Every MMCME2_ADV feature the bitstream programs, decoded with `bit2fasm`, as integers."""
    out = bitstream.with_suffix(".decoded.fasm")
    # The Vivado part (-1L) is not in the prjxray database; it is the same die as the open part.
    cb.decode_bitstream(bitstream, cb.ARTY.parts["openxc7"], out, out.with_suffix(".log"))
    return cb.mmcm_values(out.read_text())


def feedback_source(bitstream: Path) -> str:
    """The source of the MMCM's CLKFBIN in a bitstream `programmed_mmcm` has decoded.

    `CMT_LR_LOWER_B_CLKFBOUT2IN` is the MMCM's own CLKFBOUT, the dedicated feedback
    (`tt_harness.mmcm_fasm`); every build made here has passed `clock_build.feedback_gate`,
    so this is for reporting and for bitstreams built elsewhere.
    """
    return mmcm_fasm.feedback_source(bitstream.with_suffix(".decoded.fasm").read_text())[1]
