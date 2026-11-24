# SPDX-License-Identifier: Apache-2.0
"""Project-clock roots (`tt_harness.project_clocks`): classification and Vivado constraints.

The facts are the `tt-harness clock root:` / `tt-harness clock contributor:` lines both
flows print. PROBE is what Vivado 2025.2 printed for tests/data/projects/clocks, and FALLCLK
for tests/data/projects/fallclk, on 2026-11-24.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tt_harness import project_clocks as pc

PROBE = """\
tt-harness clock root: project/div_reg/Q
tt-harness clock contributor: project/div_reg/Q FDRE pclk rising direct no-async
tt-harness clock root: project/capg[1]_i_1/O
tt-harness clock contributor: project/capg[1]_i_1/O FDRE pclk rising direct no-async
tt-harness clock contributor: project/capg[1]_i_1/O FDRE pclk rising direct no-async
tt-harness clock root: project/div2_reg/Q
tt-harness clock contributor: project/div2_reg/Q FDRE root:project/div_reg/Q rising direct no-async
tt-harness clock root: project/r2_reg/Q
tt-harness clock contributor: project/r2_reg/Q FDRE pclk rising direct no-async
"""

FALLCLK = """\
tt-harness clock root: project/lq_reg/Q
tt-harness clock contributor: project/lq_reg/Q LDCE pclk falling direct no-async
tt-harness clock root: project/nq_reg/Q
tt-harness clock contributor: project/nq_reg/Q FDRE pclk falling direct no-async
"""


def _classify(text: str) -> dict[str, pc.Classified]:
    return pc.classify(pc.parse_roots(text))


def _one(*contributors: str) -> pc.Classified:
    text = "tt-harness clock root: r\n" + "".join(
        f"tt-harness clock contributor: r {c}\n" for c in contributors
    )
    return _classify(text)["r"]


def test_parse_roots_keeps_order_and_contributors() -> None:
    roots = pc.parse_roots("noise\n" + PROBE + "more noise\n")
    assert list(roots) == [
        "project/div_reg/Q",
        "project/capg[1]_i_1/O",
        "project/div2_reg/Q",
        "project/r2_reg/Q",
    ]
    assert roots["project/div2_reg/Q"] == (
        pc.Contributor("FDRE", "root:project/div_reg/Q", False, True, False),
    )
    assert len(roots["project/capg[1]_i_1/O"]) == 2


def test_parse_roots_refuses_a_contributor_of_an_unknown_root() -> None:
    with pytest.raises(ValueError, match="unknown root"):
        pc.parse_roots("tt-harness clock contributor: ghost FDRE pclk rising direct no-async\n")


def test_parse_roots_refuses_a_malformed_line() -> None:
    with pytest.raises(ValueError, match="cannot read"):
        pc.parse_roots("tt-harness clock root: r\ntt-harness clock contributor: r FDRE pclk\n")


def test_probe_roots_are_all_register_class() -> None:
    classes = _classify(PROBE)
    assert {name: c.kind for name, c in classes.items()} == {
        "project/div_reg/Q": "register",
        "project/capg[1]_i_1/O": "register",  # a gate of two rising-edge pclk flip-flops
        "project/div2_reg/Q": "register",  # a ripple stage on a register-class clock
        "project/r2_reg/Q": "register",
    }
    assert all(c.signed_off and c.from_pclk for c in classes.values())
    assert classes["project/capg[1]_i_1/O"].gate_of == 2
    assert classes["project/capg[1]_i_1/O"].reason == (
        "a gate of 2 flip-flops clocked on pclk rising edges drives it"
    )
    assert classes["project/div_reg/Q"].gate_of == 1
    assert classes["project/div2_reg/Q"].reason == (
        "a flip-flop clocked on pclk rising edges drives it"
    )


def test_fallclk_roots() -> None:
    classes = _classify(FALLCLK)
    nq, lq = classes["project/nq_reg/Q"], classes["project/lq_reg/Q"]
    assert nq.kind == "derived" and nq.signed_off
    assert "falling edge of pclk" in nq.reason
    assert lq.kind == "unclassified" and not lq.signed_off
    assert "LDCE" in lq.reason


@pytest.mark.parametrize(
    "contributors, kind",
    [
        (["FDRE pclk rising direct no-async"], "register"),
        (["FDSE pclk rising direct no-async", "FDRE pclk rising direct no-async"], "register"),
        (["FDCE pclk rising direct no-async"], "register"),  # async pin tied off
        (["FDRE_1 pclk falling direct no-async"], "derived"),
        (["FDRE pclk falling direct no-async"], "derived"),
        (["FDRE pclk rising direct no-async", "FDRE pclk falling direct no-async"], "derived"),
        # pclk reaches the flip-flop through logic: its sense is not known.
        (["FDRE pclk rising through-logic no-async"], "derived"),
        # pclk itself used as data in the gate.
        (["clock pclk rising through-logic no-async"], "derived"),
        (["LDCE pclk rising direct no-async"], "unclassified"),
        (["FDCE pclk rising direct async"], "unclassified"),
        (["FDRE sys rising direct no-async"], "unclassified"),
        (["FDRE none rising direct no-async"], "unclassified"),
        (["port none rising direct no-async"], "unclassified"),
        (["RAMB18E1 pclk rising direct no-async"], "unclassified"),
        (["FDRE pclk rising direct no-async", "FDRE sys rising direct no-async"], "unclassified"),
        ([], "unclassified"),
    ],
)
def test_classify_one_root(contributors: list[str], kind: str) -> None:
    c = _one(*contributors)
    assert c.kind == kind
    assert c.signed_off == (kind != "unclassified")


def test_classify_ripple_stages() -> None:
    text = (
        "tt-harness clock root: a\n"
        "tt-harness clock contributor: a FDRE pclk rising direct no-async\n"
        "tt-harness clock root: b\n"  # rising edge of a: still only pclk rising edges
        "tt-harness clock contributor: b FDRE root:a rising direct no-async\n"
        "tt-harness clock root: c\n"  # falling edge of a register-class clock: derived
        "tt-harness clock contributor: c FDRE root:a falling direct no-async\n"
        "tt-harness clock root: d\n"  # on a derived clock
        "tt-harness clock contributor: d FDRE root:c rising direct no-async\n"
        "tt-harness clock root: e\n"  # third stage
        "tt-harness clock contributor: e FDRE root:b rising direct no-async\n"
        "tt-harness clock root: f\n"  # on a root the tool did not report
        "tt-harness clock contributor: f FDRE root:ghost rising direct no-async\n"
    )
    kinds = {name: c.kind for name, c in _classify(text).items()}
    assert kinds == {
        "a": "register",
        "b": "register",
        "c": "derived",
        "d": "derived",
        "e": "register",
        "f": "unclassified",
    }


def test_classify_a_loop_of_roots_is_unclassified() -> None:
    text = (
        "tt-harness clock root: a\n"
        "tt-harness clock contributor: a FDRE root:b rising direct no-async\n"
        "tt-harness clock root: b\n"
        "tt-harness clock contributor: b FDRE root:a rising direct no-async\n"
    )
    classes = _classify(text)
    assert {c.kind for c in classes.values()} == {"unclassified"}
    assert not any(c.from_pclk for c in classes.values())


def test_classify_order_does_not_matter() -> None:
    text = (
        "tt-harness clock root: b\n"
        "tt-harness clock contributor: b FDRE root:a rising direct no-async\n"
        "tt-harness clock root: a\n"
        "tt-harness clock contributor: a FDRE pclk rising direct no-async\n"
    )
    assert {n: c.kind for n, c in _classify(text).items()} == {"b": "register", "a": "register"}


# Vivado constraints --------------------------------------------------------------------------

BUFG = "[get_pins pclk_bufg/O]"


def _tcl(text: str) -> str:
    return pc.vivado_constraints(pc.parse_roots(text), "pclk_bufg/O", 60.0)


def test_vivado_constraints_for_register_roots() -> None:
    tcl = _tcl(PROBE)
    lines = tcl.splitlines()
    # Both edges on pclk rising edges, from the pclk BUFG output; the pin objects are the
    # ones the collecting script stored, by position.
    for i in range(4):
        assert (
            f"create_generated_clock -name project_clk{i} -edges {{1 3 5}} -source {BUFG} "
            f"$tt_root({i})"
        ) in lines
    assert 'puts "tt-harness project clock: project_clk1 on $tt_root(1) kind register"' in lines
    assert "-divide_by" not in tcl
    # Two different register-made clocks: one pclk period for setup, the same pclk edge
    # for hold, on every ordered pair.
    for a in range(4):
        for b in range(4):
            pair = f"-from [get_clocks project_clk{a}] -to [get_clocks project_clk{b}]"
            assert (f"set_max_delay 60.000 {pair}" in lines) == (a != b)
            assert (f"set_min_delay 0.000 {pair}" in lines) == (a != b)
    assert "[get_clocks pclk]" not in tcl  # pclk <-> register clocks follow from the waveforms
    assert "[get_clocks sys]" not in tcl


def test_vivado_constraints_for_derived_and_unclassified_roots() -> None:
    tcl = _tcl(FALLCLK)
    lines = tcl.splitlines()
    # pclk's own waveform (either sense): an edge on each pclk edge.
    for i in range(2):
        assert (
            f"create_generated_clock -name project_clk{i} -divide_by 1 -source {BUFG} $tt_root({i})"
        ) in lines
    assert 'puts "tt-harness project clock: project_clk0 on $tt_root(0) kind unclassified"' in lines
    assert 'puts "tt-harness project clock: project_clk1 on $tt_root(1) kind derived"' in lines
    # Half a pclk period for setup and the same edge for hold, against pclk and every other
    # project clock, both ways.
    for a, b in [
        ("project_clk0", "pclk"),
        ("pclk", "project_clk0"),
        ("project_clk1", "pclk"),
        ("pclk", "project_clk1"),
        ("project_clk0", "project_clk1"),
        ("project_clk1", "project_clk0"),
    ]:
        pair = f"-from [get_clocks {a}] -to [get_clocks {b}]"
        assert f"set_max_delay 30.000 {pair}" in lines
        assert f"set_min_delay 0.000 {pair}" in lines
    assert "set_max_delay 60.000" not in tcl


def test_vivado_constraints_mixed_pairs_use_the_tighter_requirement() -> None:
    text = PROBE.split("tt-harness clock root: project/capg")[0] + FALLCLK
    lines = _tcl(text).splitlines()  # project_clk0 register, 1 unclassified, 2 derived
    for a, b in [("project_clk0", "project_clk1"), ("project_clk2", "project_clk0")]:
        pair = f"-from [get_clocks {a}] -to [get_clocks {b}]"
        assert f"set_max_delay 30.000 {pair}" in lines
        assert f"set_min_delay 0.000 {pair}" in lines
        assert f"set_max_delay 60.000 {pair}" not in lines
    # The register clock against pclk is left to the waveforms.
    assert not any("project_clk0" in ln and "[get_clocks pclk]" in ln for ln in lines)


def test_vivado_constraints_for_a_root_pclk_does_not_reach() -> None:
    lines = _tcl(
        "tt-harness clock root: r\ntt-harness clock contributor: r FDRE sys rising direct no-async\n"
    ).splitlines()
    # No path from pclk to make a generated clock on: a primary clock with pclk's waveform.
    assert (
        "create_clock -name project_clk0 -period 60.000 -waveform {0.000 30.000} $tt_root(0)"
        in (lines)
    )
    assert 'puts "tt-harness project clock: project_clk0 on $tt_root(0) kind unclassified"' in lines
    assert "set_max_delay 30.000 -from [get_clocks pclk] -to [get_clocks project_clk0]" in lines


def test_vivado_constraints_without_roots() -> None:
    tcl = _tcl("")
    assert "create_" not in tcl and "set_m" not in tcl
    assert 'puts "tt-harness: 0 project clock(s)"' in tcl.splitlines()


def test_command_line_writes_the_constraints(tmp_path: Path) -> None:
    # As Vivado's Tcl runs it: an isolated interpreter on the file itself.
    facts = tmp_path / "clock-roots.txt"
    facts.write_text(PROBE)
    out = tmp_path / "project-clocks.tcl"
    script = Path(pc.__file__)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            str(facts),
            str(out),
            "pclk_bufg/O",
            "60.0",
        ],
        check=True,
        env={},
    )
    assert out.read_text() == _tcl(PROBE)
