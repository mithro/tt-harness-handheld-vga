# SPDX-License-Identifier: Apache-2.0
"""The gate on the implemented netlist: every net into the project is a constant or comes
straight from a flip-flop (`tt_harness.input_gate`, used by both build flows).

Saved cases under tests/data/input_gate/, all from real builds of the test counter design:
`*-registered*` with the compare top as it is, `*-comparator*` with `rst_n` made as before
Task 9c (`clock_index >= reset_clocks`, combinational). The Yosys ones are excerpts
(`scripts/input_gate_fixtures.py`); the Vivado ones are the reports the flow wrote.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest
from test_build import NEXTPNR_PASS, PLAN, VIVADO_PASS, _counter, _fake_flow

from tt_harness import build, input_gate
from tt_harness.boards import arty
from tt_harness.compare.top import PROJECT_INSTANCE

DATA = Path(__file__).resolve().parent / "data" / "input_gate"
NETS = {
    "ui_in": "stimulus_ui_in",
    "uio_in": "stimulus_uio_in",
    "ena": "stimulus_ena",
    "rst_n": "stimulus_rst_n",
}
GOOD = {
    "ena": ("constant",),
    "rst_n": ("FDRE",),
    "ui_in": ("constant",) * 8,
    "uio_in": ("constant",) * 8,
}


def _yosys(name: str) -> dict:
    return json.loads((DATA / f"yosys-counter-{name}.json").read_text())


def _vivado(name: str) -> str:
    return (DATA / f"vivado-counter-{name}.rpt").read_text()


# The generated top ---------------------------------------------------------------------------


def _top_verilog(tmp_path: Path) -> str:
    s = _counter()
    return arty.to_verilog(arty.Top(s.project, 1_000_000, 100), tmp_path / "t.v").read_text()


def test_generated_top_connects_each_project_input_to_one_kept_net(tmp_path: Path) -> None:
    text = _top_verilog(tmp_path)
    assert input_gate.input_nets(text, PROJECT_INSTANCE) == NETS
    for net in NETS.values():
        assert any(
            line.startswith('(* keep = "true" *)') and line.rstrip(";").split()[-1] == net
            for line in (ln.split(" = 1'd")[0] for ln in text.splitlines())
        ), f"{net} is not marked keep"
    connections = input_gate.instance_connections(text, PROJECT_INSTANCE)
    assert set(connections) == {"clk", *input_gate.INPUT_PORTS, *input_gate.OUTPUT_PORTS}


def test_an_unknown_project_port_is_refused(tmp_path: Path) -> None:
    text = _top_verilog(tmp_path).replace(".ena(stimulus_ena),", ".ena(stimulus_ena),\n\t.pad(x),")
    with pytest.raises(ValueError, match=r"unknown port\(s\) \['pad'\]"):
        input_gate.input_nets(text, PROJECT_INSTANCE)


def test_a_missing_or_expression_connection_is_refused(tmp_path: Path) -> None:
    text = _top_verilog(tmp_path)
    with pytest.raises(ValueError, match=r"unconnected input\(s\) \['rst_n'\]"):
        input_gate.input_nets(text.replace("\t.rst_n(stimulus_rst_n),\n", ""), PROJECT_INSTANCE)
    with pytest.raises(ValueError, match="cannot read the connection"):
        input_gate.input_nets(
            text.replace(".rst_n(stimulus_rst_n)", ".rst_n(a >= b)"), PROJECT_INSTANCE
        )
    with pytest.raises(ValueError, match="expected one instance"):
        input_gate.input_nets(text, "absent")


# openXC7: the netlist Yosys wrote ------------------------------------------------------------


def test_yosys_registered_reset_passes() -> None:
    case = _yosys("registered")
    assert case["nets"] == NETS
    drivers = input_gate.yosys_drivers(case, "arty_compare", NETS)
    assert drivers == GOOD
    assert input_gate.offenders(drivers) == []
    assert input_gate.summary(drivers) == (
        ("ena", "constant"),
        ("rst_n", "FDRE"),
        ("ui_in", "constant"),
        ("uio_in", "constant"),
    )


def test_yosys_comparator_reset_fails() -> None:
    drivers = input_gate.yosys_drivers(_yosys("comparator"), "arty_compare", NETS)
    assert drivers["rst_n"] == ("CARRY4",)
    assert input_gate.offenders(drivers) == ["rst_n is driven by CARRY4"]


@pytest.mark.parametrize(
    "kind", ["LUT1", "LUT2", "INV", "MUXF7", "CARRY4", "LDCE", "DSP48E1", "BUFG"]
)
def test_yosys_any_driver_but_a_flip_flop_fails(kind: str) -> None:
    # A LUT used as a buffer and an inverter folded into a LUT are LUTs here.
    case = _yosys("registered")
    (cell,) = case["modules"]["arty_compare"]["cells"].values()
    cell["type"] = kind
    drivers = input_gate.yosys_drivers(case, "arty_compare", NETS)
    assert input_gate.offenders(drivers) == [f"rst_n is driven by {kind}"]


@pytest.mark.parametrize("kind", input_gate.FLIP_FLOPS)
def test_yosys_every_flip_flop_type_passes(kind: str) -> None:
    case = _yosys("registered")
    (cell,) = case["modules"]["arty_compare"]["cells"].values()
    cell["type"] = kind
    assert input_gate.offenders(input_gate.yosys_drivers(case, "arty_compare", NETS)) == []


def test_yosys_missing_net_undriven_bit_and_non_q_pin_fail() -> None:
    case = _yosys("registered")
    module = case["modules"]["arty_compare"]
    missing = copy.deepcopy(case)
    del missing["modules"]["arty_compare"]["netnames"]["stimulus_rst_n"]
    assert input_gate.offenders(input_gate.yosys_drivers(missing, "arty_compare", NETS)) == [
        "rst_n is driven by missing"
    ]
    undriven = copy.deepcopy(case)
    undriven["modules"]["arty_compare"]["cells"].clear()
    assert input_gate.offenders(input_gate.yosys_drivers(undriven, "arty_compare", NETS)) == [
        "rst_n is driven by undriven"
    ]
    one_bit = copy.deepcopy(case)
    (bit,) = module["netnames"]["stimulus_rst_n"]["bits"]
    one_bit["modules"]["arty_compare"]["netnames"]["stimulus_ui_in"]["bits"][3] = bit + 1000
    assert input_gate.offenders(input_gate.yosys_drivers(one_bit, "arty_compare", NETS)) == [
        "ui_in is driven by undriven"
    ]


# Vivado: the report from the routed design ---------------------------------------------------


CONSTANTS = frozenset({"ena", "ui_in", "uio_in"})


def test_vivado_registered_reset_passes() -> None:
    drivers = input_gate.parse_vivado(_vivado("registered"), NETS)
    assert drivers["rst_n"] == ("FDRE",)
    # Vivado removes a net tied to a constant when it propagates the constant, `keep` or
    # not: the three constant inputs are not in the routed design.
    assert drivers["ena"] == drivers["ui_in"] == drivers["uio_in"] == ("missing",)
    # That is accepted only for a net the generated top assigns a literal ...
    folded = input_gate.fold_constants(drivers, CONSTANTS)
    assert folded["ena"] == (input_gate.FOLDED,) and folded["rst_n"] == ("FDRE",)
    assert input_gate.offenders(folded) == []
    # ... and for no other: a missing net that is not a source constant fails.
    assert input_gate.offenders(drivers) == [
        "ena is driven by missing",
        "ui_in is driven by missing",
        "uio_in is driven by missing",
    ]
    gone = input_gate.fold_constants(drivers | {"rst_n": ("missing",)}, CONSTANTS)
    assert input_gate.offenders(gone) == ["rst_n is driven by missing"]


def test_source_constants_are_the_nets_assigned_a_literal(tmp_path: Path) -> None:
    text = _top_verilog(tmp_path)
    assert input_gate.source_constants(text, NETS) == CONSTANTS
    # The register is not one, and neither is a net assigned an expression or twice.
    expr = text.replace("assign stimulus_ena = 1'd1;", "assign stimulus_ena = (a & b);")
    assert input_gate.source_constants(expr, NETS) == CONSTANTS - {"ena"}
    twice = text.replace(
        "assign stimulus_ena = 1'd1;", "assign stimulus_ena = 1'd1;\nassign stimulus_ena = 1'd0;"
    )
    assert input_gate.source_constants(twice, NETS) == CONSTANTS - {"ena"}


def test_a_source_constant_with_a_real_driver_still_fails() -> None:
    drivers = input_gate.fold_constants({"ena": ("LUT1",), "rst_n": ("FDRE",)}, CONSTANTS)
    assert input_gate.offenders(drivers) == ["ena is driven by LUT1"]


def test_vivado_comparator_reset_fails() -> None:
    drivers = input_gate.fold_constants(
        input_gate.parse_vivado(_vivado("comparator"), NETS), CONSTANTS
    )
    assert drivers["rst_n"] == ("LUT6.O",)
    assert input_gate.offenders(drivers) == ["rst_n is driven by LUT6.O"]


def test_vivado_incomplete_or_foreign_report_is_refused() -> None:
    text = _vivado("registered")
    lines = text.splitlines()
    assert lines[-1] == "tt-harness project inputs: end"
    with pytest.raises(ValueError, match="incomplete"):
        input_gate.parse_vivado("\n".join(lines[:-1]) + "\n", NETS)
    with pytest.raises(ValueError, match="incomplete"):
        input_gate.parse_vivado("", NETS)
    with pytest.raises(ValueError, match="unknown port pad"):
        input_gate.parse_vivado("tt-harness project input: pad net x driver FDRE\n" + text, NETS)


def test_vivado_port_without_a_line_is_missing_and_fails() -> None:
    text = "\n".join(ln for ln in _vivado("registered").splitlines() if " rst_n " not in ln)
    drivers = input_gate.fold_constants(input_gate.parse_vivado(text + "\n", NETS), CONSTANTS)
    assert drivers["rst_n"] == ("missing",)
    assert input_gate.offenders(drivers) == ["rst_n is driven by missing"]


@pytest.mark.parametrize("driver", ["LUT2", "LUT1", "CARRY4", "FDRE.QN", "undriven", "missing"])
def test_vivado_any_driver_but_a_flip_flop_q_fails(driver: str) -> None:
    text = _vivado("registered").replace(
        "rst_n net stimulus_rst_n driver FDRE", f"rst_n net stimulus_rst_n driver {driver}"
    )
    drivers = input_gate.fold_constants(input_gate.parse_vivado(text, NETS), CONSTANTS)
    assert input_gate.offenders(drivers) == [f"rst_n is driven by {driver}"]


def test_vivado_tcl_asks_about_every_net_and_ends_the_report() -> None:
    tcl = input_gate.vivado_tcl(NETS, "/out/project-inputs.rpt")
    assert "set tt_inputs [open {/out/project-inputs.rpt} w]" in tcl
    for port, net in NETS.items():
        assert f"tt_input {port} {net}\n" in tcl
    assert tcl.index("tt_input rst_n") < tcl.index(
        'puts $tt_inputs "tt-harness project inputs: end"'
    )
    assert tcl.rstrip().endswith("close $tt_inputs")
    assert "-leaf" in tcl and "REF_NAME" in tcl


# In the build --------------------------------------------------------------------------------


def _job(tmp_path: Path, toolchain: str) -> build._Job:
    s = _counter()
    top_v = arty.to_verilog(arty.Top(s.project, 1_000_000, 100), tmp_path / "arty_compare.v")
    return build._Job(
        sources=s,
        board=arty,
        toolchain=toolchain,
        part=arty.PARTS[toolchain],
        out_dir=tmp_path,
        top_v=top_v,
        pins_xdc=tmp_path / "pins.xdc",
        plan=PLAN,
        jobs=1,
        log=tmp_path / "build.log",
        language=build.VERILOG,
    )


def test_gate_records_the_drivers_and_passes(tmp_path: Path) -> None:
    job = _job(tmp_path, "openxc7")
    job.log.write_text("")
    build._gate_inputs(job, GOOD)
    assert job.input_drivers == [
        ("ena", "constant"),
        ("rst_n", "FDRE"),
        ("ui_in", "constant"),
        ("uio_in", "constant"),
    ]
    assert "tt-harness project input: rst_n driven by FDRE\n" in job.log.read_text()


@pytest.mark.parametrize("toolchain, tool", [("openxc7", "openXC7"), ("vivado", "Vivado")])
def test_gate_fails_the_build_on_a_combinational_driver(
    tmp_path: Path, toolchain: str, tool: str
) -> None:
    job = _job(tmp_path, toolchain)
    job.log.write_text("")
    with pytest.raises(build.InputDriverError) as info:
        build._gate_inputs(job, GOOD | {"rst_n": ("CARRY4",)})
    text = str(info.value)
    assert f"project input not glitch-free in the {tool} netlist: rst_n is driven by CARRY4" in text
    assert isinstance(info.value, build.BuildError)
    # What was found is in the log and on the job even though the build fails.
    assert "tt-harness project input: rst_n driven by CARRY4\n" in job.log.read_text()


def test_vivado_script_writes_the_report_after_routing(tmp_path: Path) -> None:
    job = _job(tmp_path, "vivado")
    text = build._vivado_script(job, list(job.sources.files), []).read_text()
    assert text.index("route_design") < text.index("tt_input rst_n stimulus_rst_n")
    assert text.index("tt_input rst_n stimulus_rst_n") < text.index("write_bitstream")
    assert f"open {{{tmp_path / input_gate.REPORT_NAME}}} w" in text


def test_a_build_without_the_report_or_with_a_bad_one_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The Vivado run is replaced by one that "succeeds" and leaves the given report.
    def run_with(report: str | None):
        out = tmp_path / ("none" if report is None else str(abs(hash(report))))
        out.mkdir()
        job = dataclasses.replace(_job(out, "vivado"), out_dir=out, log=out / "build.log")
        job.log.write_text("")

        def fake_run(cmd, log, cwd, timeout, **kwargs):
            (out / "timing.rpt").write_text(VIVADO_PASS)
            if report is not None:
                (out / input_gate.REPORT_NAME).write_text(report)
            return 0

        monkeypatch.setattr(build.verilate, "run", fake_run)
        monkeypatch.setattr(build.tools, "vivado_settings", lambda: Path("/opt/settings64.sh"))
        return build._vivado_implement(job), job

    _, job = run_with(_vivado("registered"))
    assert ("rst_n", "FDRE") in job.input_drivers
    assert ("ena", input_gate.FOLDED) in job.input_drivers
    with pytest.raises(
        build.InputDriverError, match="cannot tell what drives the project's inputs"
    ):
        run_with(None)
    with pytest.raises(build.InputDriverError, match="incomplete"):
        run_with("tt-harness project input: rst_n net stimulus_rst_n driver FDRE\n")
    with pytest.raises(build.InputDriverError, match="not glitch-free in the Vivado netlist"):
        run_with(_vivado("comparator"))


def test_build_result_carries_the_input_drivers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With the flow replaced nothing is checked, and the result says so by being empty.
    _fake_flow(monkeypatch, "openxc7", NEXTPNR_PASS)
    s = _counter()
    result = build.build_compare(s.project, s, "arty", "openxc7", tmp_path, clocks=100)
    assert result.input_drivers == ()
