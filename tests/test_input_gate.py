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


def test_generated_top_connects_each_project_input_to_one_named_net(tmp_path: Path) -> None:
    text = _top_verilog(tmp_path)
    assert input_gate.input_nets(text, PROJECT_INSTANCE) == NETS
    connections = input_gate.instance_connections(text, PROJECT_INSTANCE)
    assert set(connections) == {"clk", *input_gate.INPUT_PORTS, *input_gate.OUTPUT_PORTS}
    # The register output is marked keep, so both tools preserve the net under its name.
    # The constants are not (it would stop Vivado propagating them into the project); they
    # are recognised from the literals the top assigns them.
    kept = [ln.split()[-1].rstrip(";") for ln in text.splitlines() if ln.startswith("(* keep")]
    kept = [
        ln.split(" = 1'd")[0].split()[-1] for ln in text.splitlines() if ln.startswith("(* keep")
    ]
    assert kept == ["stimulus_rst_n"]
    assert input_gate.source_constants(text, NETS) == {"ena", "ui_in", "uio_in"}


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
    # Yosys folds the constants into the project and drops their nets, as Vivado does.
    assert drivers == {
        "ena": ("missing",),
        "rst_n": ("FDRE",),
        "ui_in": ("missing",),
        "uio_in": ("missing",),
    }
    folded = input_gate.fold_constants(drivers, frozenset({"ena", "ui_in", "uio_in"}))
    assert input_gate.offenders(folded) == []
    assert input_gate.summary(folded) == (
        ("ena", input_gate.FOLDED),
        ("rst_n", "FDRE"),
        ("ui_in", input_gate.FOLDED),
        ("uio_in", input_gate.FOLDED),
    )
    # A constant net that is still there (tied off) is a constant too.
    case["modules"]["arty_compare"]["netnames"]["stimulus_ui_in"] = {"bits": ["0", "1"] * 4}
    assert input_gate.yosys_drivers(case, "arty_compare", NETS)["ui_in"] == ("constant",) * 8


def test_yosys_comparator_reset_fails() -> None:
    drivers = input_gate.yosys_drivers(_yosys("comparator"), "arty_compare", NETS)
    assert drivers["rst_n"] == ("CARRY4",)
    assert _only_rst_n(_yosys("comparator")) == ["rst_n is driven by CARRY4"]


@pytest.mark.parametrize(
    "kind", ["LUT1", "LUT2", "INV", "MUXF7", "CARRY4", "LDCE", "DSP48E1", "BUFG"]
)
def test_yosys_any_driver_but_a_flip_flop_fails(kind: str) -> None:
    # A LUT used as a buffer and an inverter folded into a LUT are LUTs here.
    case = _yosys("registered")
    (cell,) = case["modules"]["arty_compare"]["cells"].values()
    cell["type"] = kind
    assert _only_rst_n(case) == [f"rst_n is driven by {kind}"]


@pytest.mark.parametrize("kind", input_gate.FLIP_FLOPS)
def test_yosys_every_flip_flop_type_passes(kind: str) -> None:
    case = _yosys("registered")
    (cell,) = case["modules"]["arty_compare"]["cells"].values()
    cell["type"] = kind
    assert _only_rst_n(case) == []


def _only_rst_n(case: dict) -> list[str]:
    drivers = input_gate.yosys_drivers(case, "arty_compare", NETS)
    folded = input_gate.fold_constants(drivers, frozenset({"ena", "ui_in", "uio_in"}))
    return input_gate.offenders(folded)


def test_yosys_missing_net_undriven_bit_and_non_q_pin_fail() -> None:
    case = _yosys("registered")
    assert _only_rst_n(case) == []
    missing = copy.deepcopy(case)
    del missing["modules"]["arty_compare"]["netnames"]["stimulus_rst_n"]
    assert _only_rst_n(missing) == ["rst_n is driven by missing"]
    undriven = copy.deepcopy(case)
    undriven["modules"]["arty_compare"]["cells"].clear()
    assert _only_rst_n(undriven) == ["rst_n is driven by undriven"]
    # One bit of a constant bus that is not a constant after all.
    one_bit = copy.deepcopy(case)
    one_bit["modules"]["arty_compare"]["netnames"]["stimulus_ui_in"] = {"bits": ["0"] * 7 + [9999]}
    assert _only_rst_n(one_bit) == ["ui_in is driven by undriven"]
    # A flip-flop, but not from its Q pin.
    not_q = copy.deepcopy(case)
    (cell,) = not_q["modules"]["arty_compare"]["cells"].values()
    cell["connections"] = {"QN": cell["connections"]["Q"]}
    cell["port_directions"]["QN"] = cell["port_directions"].pop("Q")
    assert _only_rst_n(not_q) == ["rst_n is driven by FDRE.QN"]


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


# Which clock the driving flip-flop is on -------------------------------------------------------
#
# The Yosys case is a real netlist (scripts/input_gate_clock_fixtures.py: a tiny Migen design,
# `rst_n` and `ui_in` registered in the project clock domain, `ena` in the other one). The Vivado
# report is HAND-WRITTEN in the format `vivado_tcl` emits: Vivado was not available when this
# was written, so the Tcl's clock lines are not verified against a real run.
# The check fails closed: a flip-flop driver needs a clock entry for every bit, equal to the
# expected net; anything missing, short, long or None is an offender.

CLOCK_NETS = {
    "ui_in": "pin_ui_in",
    "ena": "pin_ena",
    "rst_n": "pin_rst_n",
}
CLOCK_TOP = "clock_domains_fixture"
Trace = input_gate.ClockTrace


def _clock_case() -> dict:
    return json.loads((DATA / "yosys-clock-domains.json").read_text())


def _good(net: str = "project_clk") -> Trace:
    return Trace((net,), ())


def _ok_inputs() -> tuple[dict, dict]:
    drivers = {"ena": ("FDRE",), "ui_in": ("FDRE",) * 2, "uio_in": (input_gate.FOLDED,)}
    clocks = {"ena": (_good(),), "ui_in": (_good(),) * 2}
    return drivers, clocks


def test_clock_check_all_good_passes_and_constants_need_no_clock() -> None:
    drivers, clocks = _ok_inputs()
    assert input_gate.clock_offenders(drivers, clocks, "project_clk") == []
    drivers = drivers | {"rst_n": (input_gate.CONSTANT,)}
    assert input_gate.clock_offenders(drivers, clocks, "project_clk") == []


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda d, c: c.pop("ena"), "ena[0]: no clock data"),  # missing port
        (lambda d, c: c.__setitem__("ui_in", (_good(),)), "ui_in: 2 bits but 1 clock entries"),
        (lambda d, c: c.__setitem__("ui_in", (_good(),)), "ui_in[1]: unchecked"),
        (lambda d, c: c.__setitem__("ui_in", (_good(),)), "ui_in[0]: unchecked"),
        (lambda d, c: c.__setitem__("ui_in", (_good(),) * 3), "ui_in: 2 bits but 3 clock entries"),
        (lambda d, c: c.__setitem__("ui_in", (_good(),) * 3), "ui_in[1]: unchecked"),
        (lambda d, c: c.__setitem__("ena", (None,)), "ena[0]: no clock net"),
        (lambda d, c: c.__setitem__("ui_in", (_good(), None)), "ui_in[1]: no clock net"),
        (lambda d, c: c.__setitem__("ena", (_good("sys_clk"),)), "ena[0]: clocked by sys_clk"),
        (lambda d, c: c.clear(), "ena[0]: no clock data"),
        (lambda d, c: d.__setitem__("ena", ("LUT2",)), "ena[0]: driven by LUT2"),
        (lambda d, c: d.__setitem__("ena", (input_gate.MISSING,)), "ena[0]: driven by missing"),
    ],
)
def test_clock_check_fails_closed(mutate, expect: str) -> None:
    drivers, clocks = _ok_inputs()
    mutate(drivers, clocks)
    out = input_gate.clock_offenders(drivers, clocks, "project_clk")
    assert out, "the gate passed with missing or wrong clock data"
    assert any(expect in line for line in out), out


def test_a_clock_trace_through_a_gate_is_reported_and_can_be_pinned() -> None:
    drivers = {"ena": ("FDRE",)}
    gated = {"ena": (Trace(("project_clk",), ("BUFGCE", "BUFG")),)}
    assert input_gate.clock_offenders(drivers, gated, "project_clk") == []  # caller's choice
    out = input_gate.clock_offenders(drivers, gated, "project_clk", allowed_trace=("BUFG",))
    assert out and "BUFGCE/BUFG" in out[0]
    exact = {"ena": (Trace(("project_clk",), ("BUFG",)),)}
    assert input_gate.clock_offenders(drivers, exact, "project_clk", allowed_trace=("BUFG",)) == []


def test_yosys_clock_domains() -> None:
    case = _clock_case()
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, CLOCK_NETS)
    assert drivers["ena"] == ("FDRE",) and drivers["rst_n"] == ("FDRE",)
    assert input_gate.offenders(drivers) == []  # glitch-free drivers, but in which domain?
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, CLOCK_NETS)
    # The clock buffer's output net is renamed by the tool; the chain reaches the port name.
    assert "project_clk" in clocks["rst_n"][0].names and "project_clk" in clocks["ui_in"][0].names
    assert "sys_clk" in clocks["ena"][0].names
    assert clocks["rst_n"][0].cells and set(clocks["rst_n"][0].cells) <= set(
        input_gate.CLOCK_BUFFERS
    )
    (bad,) = input_gate.clock_offenders(drivers, clocks, "project_clk")
    assert bad.startswith("ena[0]: clocked by ") and "sys_clk" in bad
    wrong = input_gate.clock_offenders(drivers, clocks, "sys_clk")
    assert {w.split("[")[0].split(":")[0] for w in wrong} == {"rst_n", "ui_in"}
    assert len(clocks["ui_in"]) == 8


def test_yosys_missing_net_and_missing_clock_pin_are_offenders() -> None:
    case = _clock_case()
    nets = CLOCK_NETS | {"absent": "no_such_net"}
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, nets)
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, nets)
    assert drivers["absent"] == (input_gate.MISSING,) and clocks["absent"] == (None,)
    assert any(
        line.startswith("absent[0]")
        for line in input_gate.clock_offenders(drivers, clocks, "project_clk")
    )
    for cell in case["modules"][CLOCK_TOP]["cells"].values():
        if cell["type"] == "FDRE":
            cell["connections"].pop("C", None)
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, CLOCK_NETS)
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, CLOCK_NETS)
    out = input_gate.clock_offenders(drivers, clocks, "project_clk")
    assert len(out) == 1 + 1 + 8 and all("no clock net" in line for line in out)


def _lut_gated(case: dict, kind: str) -> dict:
    """The fixture with the `ena` flip-flop's clock driven through a cell of type `kind`."""
    module = case["modules"][CLOCK_TOP]
    cells = module["cells"]
    ena_bit = module["netnames"]["pin_ena"]["bits"][0]
    flop = next(
        c for c in cells.values() if c["type"] == "FDRE" and ena_bit in c["connections"]["Q"]
    )
    source = flop["connections"]["C"][0]
    gated = 99999
    flop["connections"]["C"] = [gated]
    if kind.startswith("LUT"):
        pins = {"I0": "input", "I1": "input", "O": "output"}
        conns = {"I0": [source], "I1": [source]}
    else:
        pins = {"I": "input", "CE": "input", "O": "output"}
        conns = {"I": [source], "CE": ["1"]}
    cells["gate"] = {"type": kind, "port_directions": pins, "connections": conns | {"O": [gated]}}
    return case


def test_a_clock_gated_by_a_lut_stops_at_the_lut_and_fails() -> None:
    case = _lut_gated(_clock_case(), "LUT2")
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, CLOCK_NETS)
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, CLOCK_NETS)
    assert clocks["ena"][0].stopped_at == "LUT2"
    # Even asked for the clock the gate is fed from, a gated clock is not that clock.
    out = input_gate.clock_offenders(drivers, clocks, "sys_clk")
    assert any(line.startswith("ena[0]") and "LUT2" in line for line in out)


def test_a_clock_gated_by_a_bufgce_is_reported_with_the_bufgce_in_the_trace() -> None:
    case = _lut_gated(_clock_case(), "BUFGCE")
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, CLOCK_NETS)
    trace = clocks["ena"][0]
    assert trace.cells[0] == "BUFGCE" and "sys_clk" in trace.names
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, CLOCK_NETS)
    # The caller decides: pinned to the buffer chain of the project clock, a BUFGCE fails.
    out = input_gate.clock_offenders(drivers, clocks, "sys_clk", allowed_trace=trace.cells[1:])
    assert any(line.startswith("ena[0]") and "BUFGCE" in line for line in out)


def test_gate_rejects_register_in_the_wrong_domain_vivado() -> None:
    text = (DATA / "vivado-clock-domains.rpt").read_text()
    nets = CLOCK_NETS | {"uio_in": "stimulus_uio_in"}
    drivers = input_gate.parse_vivado(text, nets)
    assert drivers["rst_n"] == ("FDCE",) and drivers["ui_in"] == ("FDRE", "FDRE")
    clocks = input_gate.parse_vivado_clocks(text, nets)
    assert clocks["rst_n"] == (Trace(("project_clk",), ()),)
    assert clocks["uio_in"] == (None,)
    folded = input_gate.fold_constants(drivers, frozenset({"uio_in"}))
    assert input_gate.offenders(folded) == []
    out = input_gate.clock_offenders(folded, clocks, "project_clk")
    assert out == ["ena[0]: clocked by sys_clk, not project_clk"]


def test_vivado_report_gaps_are_offenders() -> None:
    text = (DATA / "vivado-clock-domains.rpt").read_text()
    nets = CLOCK_NETS | {"uio_in": "stimulus_uio_in"}
    consts = frozenset({"uio_in"})

    def run(report: str) -> list[str]:
        drivers = input_gate.fold_constants(input_gate.parse_vivado(report, nets), consts)
        clocks = input_gate.parse_vivado_clocks(report, nets)
        return input_gate.clock_offenders(drivers, clocks, "project_clk")

    # a flip-flop line with no clock field
    assert any("no clock net" in x for x in run(text.replace(" clock project_clk\n", "\n", 1)))
    # a port the report does not mention at all
    lines = [ln for ln in text.splitlines() if " rst_n " not in ln]
    assert any(x.startswith("rst_n[0]") for x in run("\n".join(lines) + "\n"))
    # the old report format still parses; its flip-flop has no clock, so the check fails
    old = _vivado("registered")
    old_drivers = input_gate.fold_constants(input_gate.parse_vivado(old, NETS), CONSTANTS)
    old_clocks = input_gate.parse_vivado_clocks(old, NETS)
    assert old_clocks["rst_n"] == (None,)
    assert input_gate.clock_offenders(old_drivers, old_clocks, "project_clk") == [
        "rst_n[0]: no clock net"
    ]


def test_vivado_tcl_reports_the_clock_net_of_a_flip_flop_driver() -> None:
    tcl = input_gate.vivado_tcl(NETS, "/out/project-inputs.rpt")
    assert "REF_PIN_NAME == C" in tcl and "get_nets -quiet -of_objects $cpin" in tcl
    assert 'driver $kind$clock"' in tcl


def _chain(hops: int) -> tuple[dict, dict, dict]:
    """The clock fixture with the `ena` flip-flop's clock behind `hops` plain buffers."""
    case = _clock_case()
    module = case["modules"][CLOCK_TOP]
    cells = module["cells"]
    ena_bit = module["netnames"]["pin_ena"]["bits"][0]
    flop = next(
        c for c in cells.values() if c["type"] == "FDRE" and ena_bit in c["connections"]["Q"]
    )
    previous = flop["connections"]["C"][0]
    for hop in range(hops):
        out_bit = 90000 + hop
        cells[f"hop{hop}"] = {
            "type": "BUFG",
            "port_directions": {"I": "input", "O": "output"},
            "connections": {"I": [previous], "O": [out_bit]},
        }
        previous = out_bit
    flop["connections"]["C"] = [previous]
    drivers = input_gate.yosys_drivers(case, CLOCK_TOP, CLOCK_NETS)
    clocks = input_gate.yosys_driver_clocks(case, CLOCK_TOP, CLOCK_NETS)
    return case, drivers, clocks


def test_the_clock_hop_limit_boundary() -> None:
    limit = input_gate.MAX_CLOCK_HOPS
    base = len(_chain(0)[2]["ena"][0].cells)  # the fixture's own buffers (IBUF, BUFG)
    # exactly `limit` buffers in all is a plain buffer chain; one more is not
    _, drivers, clocks = _chain(limit - base)
    assert len(clocks["ena"][0].cells) == limit and clocks["ena"][0].stopped_at is None
    assert [
        x for x in input_gate.clock_offenders(drivers, clocks, "sys_clk") if x.startswith("ena")
    ] == []
    _, drivers, clocks = _chain(limit - base + 1)
    out = input_gate.clock_offenders(drivers, clocks, "sys_clk")
    assert any(line.startswith("ena[0]") and "more than" in line for line in out), out
    _, drivers, clocks = _chain(limit - base + 4)
    assert any(
        "more than" in line for line in input_gate.clock_offenders(drivers, clocks, "sys_clk")
    )
