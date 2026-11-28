# SPDX-License-Identifier: Apache-2.0
"""Probe: how do Vivado and the open flow encode MMCM dividers? One tiny MMCM design per setting."""

import re
import sys
from pathlib import Path

from tt_harness import clock_build as cb
from tt_harness import tools, verilate

ROOT = Path("build/fracprobe").resolve()
DB = cb.PRJXRAY_DB / "artix7"
PART = "xc7a100tcsg324-1"

# COMPENSATION("INTERNAL") is deliberate here: this probe compares how the two flows encode
# the dividers, as the design was first specified. It does not lock in the open flow
# (nextpnr-xilinx 0.8.2 ties CLKFBIN to a constant for it); the harness's own clock uses
# ZHOLD and the feedback gate (docs/results/2026-11-spike-b-project-clock.md).
V = """\
module top(input clk50, output led);
  wire fb, o, g, locked;
  MMCME2_ADV #(.BANDWIDTH("OPTIMIZED"), .COMPENSATION("INTERNAL"), .CLKIN1_PERIOD(20.0),
    .DIVCLK_DIVIDE(%s), .CLKFBOUT_MULT_F(%s), .CLKOUT0_DIVIDE_F(%s), .CLKOUT0_DUTY_CYCLE(0.5),
    .CLKOUT0_PHASE(0.0), .CLKFBOUT_PHASE(0.0)) mmcm (
    .CLKIN1(clk50), .CLKIN2(1'b0), .CLKINSEL(1'b1), .CLKFBIN(fb), .CLKFBOUT(fb), .CLKOUT0(o),
    .LOCKED(locked), .RST(1'b0), .PWRDWN(1'b0), .DCLK(1'b0), .DEN(1'b0), .DWE(1'b0),
    .DADDR(7'd0), .DI(16'd0), .PSCLK(1'b0), .PSEN(1'b0), .PSINCDEC(1'b0));
  BUFG b (.I(o), .O(g));
  reg [23:0] c = 0;
  always @(posedge g) c <= c + 1;
  assign led = c[23];
endmodule
"""
XDC = """\
set_property LOC P17 [get_ports clk50]
set_property IOSTANDARD LVCMOS33 [get_ports clk50]
set_property LOC V14 [get_ports led]
set_property IOSTANDARD LVCMOS33 [get_ports led]
create_clock -name clk50 -period 20.0 [get_ports clk50]
"""


def run(cmd, log, cwd, **kw):
    rc = verilate.run(cmd, log, cwd, 1800, **kw)
    if rc:
        raise SystemExit(f"failed {cmd[0]} rc={rc} log={log}")


def decode(bit: Path, out: Path, log: Path):
    """The harness's own decode (`clock_build.decode_bitstream`): logged, TMPDIR in the repo."""
    cb.decode_bitstream(bit, PART, out, log)


def mmcm_lines(fasm: Path):
    keep = []
    for line in fasm.read_text().splitlines():
        if "MMCME2_ADV." in line and re.search(
            r"CLKOUT0_|CLKFBOUT_|CLKOUT5_CLKOUT[12]_FRACTIONAL|DIVCLK_", line
        ):
            keep.append(line.split("MMCME2_ADV.")[1])
    return keep


def one(mult: str, div: str, flows, divclk: str = "1"):
    d = ROOT / f"c{divclk}_m{mult}_d{div}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "top.v").write_text(V % (divclk, mult, div))
    (d / "top.xdc").write_text(XDC)
    log = d / "build.log"
    log.write_text("")
    if "open" in flows:
        run(
            [
                str(cb.tool("yosys")),
                "-p",
                f"read_verilog {d / 'top.v'}; synth_xilinx -flatten -abc9 -noclkbuf -arch xc7 -top top; write_json {d / 'top.json'}",
            ],
            log,
            d,
        )
        run(
            [
                str(cb.tool("nextpnr-xilinx")),
                "--chipdb",
                str(cb.CHIPDB / f"{PART}.bin"),
                "--xdc",
                str(d / "top.xdc"),
                "--json",
                str(d / "top.json"),
                "--fasm",
                str(d / "open.fasm"),
                "--freq",
                "25",
            ],
            log,
            d,
        )
        run(
            [
                str(cb.tool("fasm2frames")),
                "--part",
                PART,
                "--db-root",
                str(DB),
                str(d / "open.fasm"),
            ],
            log,
            d,
            stdout=d / "open.frames",
        )
        run(
            [
                str(cb.tool("xc7frames2bit")),
                "--part_file",
                str(DB / PART / "part.yaml"),
                "--part_name",
                PART,
                "--frm_file",
                str(d / "open.frames"),
                "--output_file",
                str(d / "open.bit"),
            ],
            log,
            d,
        )
        decode(d / "open.bit", d / "open.decoded.fasm", log)
    if "vivado" in flows:
        (d / "viv.tcl").write_text(
            f"read_verilog {{{d / 'top.v'}}}\nread_xdc {{{d / 'top.xdc'}}}\nsynth_design -top top -part {PART}\n"
            f"opt_design\nplace_design\nroute_design\nwrite_bitstream -force {{{d / 'viv.bit'}}}\n"
        )
        shell = f'source "{tools.vivado_settings()}" && exec vivado -mode batch -nojournal -log "{d / "vivado.log"}" -source "{d / "viv.tcl"}"'
        run(["bash", "-c", shell], log, d)
        decode(d / "viv.bit", d / "viv.decoded.fasm", log)


if __name__ == "__main__":
    flows = sys.argv[1].split(",")
    cases = [
        ("18.000", "35.000"),
        ("18.000", "35.250"),
        ("18.000", "35.500"),
        ("18.000", "35.750"),
        ("18.000", "36.750"),
        ("18.500", "36.000"),
        ("18.125", "36.000"),
    ]
    if len(sys.argv) > 2:  # "divclk:mult:div" ...
        for spec in sys.argv[2:]:
            c, m, dv = spec.split(":")
            one(m, dv, flows, c)
            print("done", spec, flush=True)
        raise SystemExit(0)
    for m, dv in cases:
        one(m, dv, flows)
        print("done", m, dv, flush=True)
