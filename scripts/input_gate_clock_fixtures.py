# SPDX-License-Identifier: Apache-2.0
"""Make the saved Yosys case for the input gate's clock-domain check.

    uv run python scripts/input_gate_clock_fixtures.py

Builds a tiny Migen design with two clock domains (`project`, `sys`) and a black-box
project instance whose inputs are driven by one register per domain, runs Yosys
(`synth_xilinx`) on it and saves the whole (small) netlist as
`tests/data/input_gate/yosys-clock-domains.json`. `rst_n` is driven by the register in
`project`, `ena` by the one in `sys` (the wrong domain). The tests' nets: `pin_rst_n`,
`pin_ena`, `pin_ui_in`; clock nets `project_clk` and `sys_clk`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from migen import ClockDomain, Instance, Module, Signal
from migen.fhdl import verilog

from tt_harness import tools

OUT = Path(__file__).resolve().parent.parent / "tests" / "data" / "input_gate"
TOP = "clock_domains_fixture"
STUB = """(* blackbox *)
module tt_project(input clk, input rst_n, input ena, input [7:0] ui_in, output [7:0] uo_out);
endmodule
"""


class Fixture(Module):
    def __init__(self) -> None:
        self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
        self.clock_domains.cd_sys = ClockDomain("sys", reset_less=True)
        self.d = Signal()
        pin_rst_n = Signal(name="pin_rst_n")
        pin_ena = Signal(name="pin_ena")
        pin_ui_in = Signal(8, name="pin_ui_in")
        for s in (pin_rst_n, pin_ena, pin_ui_in):
            s.attr.add("keep")
        self.sync.project += [pin_rst_n.eq(self.d), pin_ui_in.eq(0xA5 ^ pin_ui_in)]
        self.sync.sys += pin_ena.eq(self.d)
        self.uo = Signal(8)
        self.specials += Instance(
            "tt_project",
            name="project",
            i_clk=self.cd_project.clk,
            i_rst_n=pin_rst_n,
            i_ena=pin_ena,
            i_ui_in=pin_ui_in,
            o_uo_out=self.uo,
        )


def main() -> int:
    yosys = tools.find("yosys")
    if yosys is None:
        print("yosys not found", file=sys.stderr)
        return 1
    work = tools.REPO / "tmp" / "input-gate-clock-fixture"
    work.mkdir(parents=True, exist_ok=True)
    fx = Fixture()
    ios = {fx.cd_project.clk, fx.cd_sys.clk, fx.d, fx.uo}
    verilog.convert(fx, ios=ios, name=TOP).write(str(work / "top.v"))
    (work / "stub.v").write_text(STUB)
    out = work / "netlist.json"
    script = (
        f"read_verilog -lib {work / 'stub.v'}; read_verilog {work / 'top.v'}; "
        f"synth_xilinx -top {TOP} -flatten; write_json {out}"
    )
    subprocess.run([str(yosys), "-q", "-p", script], check=True, cwd=work)
    doc = json.loads(out.read_text())
    module = doc["modules"][TOP]
    # Bits and cell types only: attributes and source locations carry local paths.
    slim = {
        "comment": f"Yosys netlist of {TOP} (scripts/input_gate_clock_fixtures.py)",
        "modules": {
            TOP: {
                "netnames": {n: {"bits": v["bits"]} for n, v in module["netnames"].items()},
                "ports": {
                    n: {"direction": v["direction"], "bits": v["bits"]}
                    for n, v in module["ports"].items()
                },
                "cells": {
                    name: {
                        "type": c["type"],
                        "port_directions": c["port_directions"],
                        "connections": c["connections"],
                    }
                    for name, c in module["cells"].items()
                },
            }
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "yosys-clock-domains.json"
    target.write_text(json.dumps(slim, indent=1, sort_keys=True) + "\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
