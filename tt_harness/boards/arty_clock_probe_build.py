# SPDX-License-Identifier: Apache-2.0
"""Builds of the Arty clock probe (`arty_clock_probe`), and what their bitstreams program.

`build` makes one probe with either toolchain through `tt_harness.clock_build` and its timing
gate. `programmed_mmcm` and `feedback_source` decode a bitstream with `bit2fasm`. The
diagnostic variants at the end exist to find out why an open-flow MMCM does not lock; they
are never the measurement itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from tt_harness import clock_build as cb
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
    cb.decode_bitstream(bitstream, cb.ARTY.parts["openxc7"], out)
    return cb.mmcm_values(out.read_text())


def feedback_source(bitstream: Path) -> str:
    """What the bitstream connects to the MMCM's CLKFBIN (the source of the mux's set pip).

    `CMT_LR_LOWER_B_CLKFBOUT2IN` is the MMCM's own CLKFBOUT, the dedicated feedback;
    `CMT_L_LOWER_B_CLK_IN3_INT` is a pin of the fabric interconnect, which nextpnr-xilinx
    0.8.2 ties to VCC (the MMCM then never locks). Needs `programmed_mmcm`
    to have decoded the bitstream.
    """
    fasm = bitstream.with_suffix(".decoded.fasm").read_text()
    found = re.findall(r"^CMT_\S+\.CMT_LR_LOWER_B_MMCM_CLKFBIN\.(\S+)$", fasm, re.MULTILINE)
    return ",".join(found) or "none"


# Diagnostic variants of the open-flow build (never the measurement itself) ----------------------

FEEDBACK_PIP = re.compile(
    r"^(CMT_\S+)\.CMT_LR_LOWER_B_MMCM_CLKFBIN\.CMT_L_LOWER_B_CLK_IN3_INT$", re.MULTILINE
)
PREPACK_ZHOLD = """
for name, cell in ctx.cells:
    if cell.type == "MMCME2_ADV":
        cell.setParam("COMPENSATION", "ZHOLD")
        print(f"tt-harness compensation: {name} ZHOLD")
"""


def rewrite_feedback(job: cb.Build) -> None:
    """Rewrite an open-flow build's FASM so CLKFBIN takes CLKFBOUT, and remake the bitstream.

    nextpnr-xilinx 0.8.2 takes CLKFBIN of an MMCM whose COMPENSATION is "INTERNAL" (or
    absent) off its net and ties it to VCC through the fabric interconnect
    (`xilinx/pack_clocking_xc7.cc`, lines 98 to 104), where Vivado selects the dedicated
    CLKFBOUT-to-CLKFBIN connection. This edits that one multiplexer in the FASM (and drops
    the interconnect pip that fed it) and runs `fasm2frames` and `xc7frames2bit` again.
    """
    fasm_path = job.out_dir / f"{cb.TOP}.fasm"
    fasm, count = FEEDBACK_PIP.subn(
        r"\1.CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_LR_LOWER_B_CLKFBOUT2IN", fasm_path.read_text()
    )
    if count != 1:
        raise cb.BuildError(
            f"expected one CLKFBIN pip from the interconnect, found {count}", job.log
        )
    fasm = re.sub(r"^CMT_\S+\.CMT_L_LOWER_B_CLK_IN3_INT\.\S+\n", "", fasm, flags=re.MULTILINE)
    fasm_path.write_text(fasm)
    part = job.board.parts["openxc7"]
    db = cb.PRJXRAY_DB / "artix7"
    frames = job.out_dir / f"{cb.TOP}.frames"
    cb._run(
        job,
        "fasm2frames",
        [str(cb.tool("fasm2frames")), "--part", part, "--db-root", str(db), str(fasm_path)],
        job.out_dir,
        stdout=frames,
    )
    cb._run(
        job,
        "xc7frames2bit",
        [
            str(cb.tool("xc7frames2bit")),
            "--part_file",
            str(db / part / "part.yaml"),
            "--part_name",
            part,
            "--frm_file",
            str(frames),
            "--output_file",
            str(job.bitstream),
        ],
        job.out_dir,
    )
