# SPDX-License-Identifier: Apache-2.0
"""Every signal the harness drives into the project is a constant or a register output.

The compare top's fragment is walked (`tt_harness.compare.inputs`); the cases below it are
small modules that show what the walk accepts and what it refuses.

Limits. This reads the source description only: it cannot see logic that synthesis inserts
or duplicates (the build gate looks at the netlist for that, and in the netlist it finds only
`rst_n`; the constant inputs are accepted from the literal in the generated top). Neither
this test nor the build gate checks which clock domain the register driving `rst_n` is in: a
register clocked by another domain would be accepted.
"""

from __future__ import annotations

import dataclasses

import pytest
from migen import Cat, If, Instance, Module, Signal
from test_build import _counter

from tt_harness.compare import inputs
from tt_harness.compare.top import PROJECT_INSTANCE, CompareTop

PROJECT_INPUTS = {"clk", "rst_n", "ena", "ui_in", "uio_in"}


@pytest.mark.parametrize("settle", [2, 3])
@pytest.mark.parametrize("ui_in", [0x00, 0xA5])
def test_compare_top_drives_the_project_from_constants_and_registers(
    settle: int, ui_in: int
) -> None:
    project = dataclasses.replace(_counter().project, ui_in=ui_in, uio_in=0x3C)
    top = CompareTop(project, 100_000_000, 1_000_000, 1000, settle=settle)
    found = inputs.project_inputs(top, PROJECT_INSTANCE)
    # A new project input must be added here, and then pass the rule below.
    assert set(found) == PROJECT_INPUTS
    for port, source in found.items():
        if port == inputs.CLOCK_PORT:
            continue
        assert source.glitch_free, f"project input {port}: {source.kind}: {source.detail}"
    assert found["rst_n"].kind == "register"
    assert found["ena"].kind == found["ui_in"].kind == found["uio_in"].kind == "constant"


def test_compare_top_without_reset_clocks_still_registers_rst_n() -> None:
    top = CompareTop(_counter().project, 100_000_000, 1_000_000, 100, reset_clocks=0)
    found = inputs.project_inputs(top, PROJECT_INSTANCE)
    assert found["rst_n"].glitch_free


# What the walk accepts and refuses -----------------------------------------------------------


class _Case(Module):
    """One instance `project` with input `x` driven by what `wire(self)` returns."""

    def __init__(self, wire) -> None:
        self.a = Signal()
        self.b = Signal()
        self.port = Signal()  # nothing assigns it: a port of this module
        self.sync += [self.a.eq(~self.a), self.b.eq(self.a)]
        self.specials += Instance("thing", name="project", i_x=wire(self), i_clk=self.a)


def _kind(wire) -> str:
    return inputs.project_inputs(_Case(wire), "project")["x"].kind


def _through_wires(m: _Case) -> Signal:
    w1, w2 = Signal(), Signal()
    m.comb += [w1.eq(m.a), w2.eq(w1)]
    return w2


def _constant_through_a_wire(m: _Case) -> Signal:
    w = Signal(8)
    m.comb += w.eq(0xA5)
    return w


def _gate(m: _Case) -> Signal:
    w = Signal()
    m.comb += w.eq(m.a & m.b)
    return w


def _comparator(m: _Case) -> Signal:
    # The form `rst_n` had before Task 9c.
    count = Signal(32)
    w = Signal()
    m.sync += count.eq(count + 1)
    m.comb += w.eq(count >= 10)
    return w


def _conditional(m: _Case) -> Signal:
    w = Signal()
    m.comb += If(m.a, w.eq(m.b))
    return w


def _assigned_twice(m: _Case) -> Signal:
    w = Signal()
    m.comb += [w.eq(m.a), w.eq(m.b)]
    return w


def _inverted(m: _Case) -> Signal:
    w = Signal()
    m.comb += w.eq(~m.a)
    return w


def _comb_and_sync(m: _Case) -> Signal:
    w = Signal()
    m.comb += w.eq(m.a)
    m.sync += w.eq(m.b)
    return w


@pytest.mark.parametrize(
    "wire, kind",
    [
        (lambda m: m.a, "register"),
        (_through_wires, "register"),
        (lambda m: 1, "constant"),
        (_constant_through_a_wire, "constant"),
        (_gate, "combinational"),
        (_comparator, "combinational"),
        (_conditional, "combinational"),
        (_assigned_twice, "combinational"),
        (_inverted, "combinational"),
        (_comb_and_sync, "combinational"),
        (lambda m: m.a & m.b, "combinational"),  # an expression right on the port
        (lambda m: Cat(m.a, m.b), "combinational"),
        (lambda m: m.b[0], "combinational"),  # even a slice: unknown forms fail by default
        (lambda m: m.port, "undriven"),
    ],
)
def test_classification(wire, kind: str) -> None:
    assert _kind(wire) == kind
    assert (kind in inputs.GLITCH_FREE) == (kind in ("register", "constant"))


def test_needs_exactly_one_instance_of_that_name() -> None:
    with pytest.raises(ValueError, match="expected one instance named 'absent'"):
        inputs.project_inputs(_Case(lambda m: m.a), "absent")


# The clock domain, and the asynchronous-clear flip-flop -------------------------------------


def test_a_register_reports_its_clock_domain() -> None:
    class M(Module):
        def __init__(self) -> None:
            a, b = Signal(), Signal()
            self.sync.project += a.eq(~a)
            self.sync += b.eq(~b)
            self.specials += Instance("thing", name="project", i_x=a, i_y=b)

    found = inputs.project_inputs(M(), "project")
    assert (found["x"].kind, found["x"].domain) == ("register", "project")
    assert (found["y"].kind, found["y"].domain) == ("register", "sys")
    assert inputs.project_inputs(_Case(lambda m: 1), "project")["x"].domain is None


def test_a_register_assigned_in_two_domains_is_refused() -> None:
    class M(Module):
        def __init__(self) -> None:
            a = Signal()
            self.sync.project += a.eq(1)
            self.sync += a.eq(0)
            self.specials += Instance("thing", name="project", i_x=a)

    found = inputs.project_inputs(M(), "project")["x"]
    assert found.kind == "combinational" and "clock domains" in found.detail


def test_only_the_output_of_an_async_clear_flop_is_accepted_as_a_special() -> None:
    from migen.genlib.cdc import MultiReg

    from tt_harness.core.cdc import AsyncClearFlop

    class M(Module):
        def __init__(self) -> None:
            clear, d, q, synced, written = (Signal() for _ in range(5))
            self.sync += clear.eq(~clear)
            self.specials += AsyncClearFlop("project", clear, d, q)
            self.specials += MultiReg(d, synced, odomain="project")
            self.comb += written.eq(clear & d)
            self.specials += AsyncClearFlop("project", clear, d, written)
            self.specials += Instance("thing", name="project", i_q=q, i_s=synced, i_w=written)

    found = inputs.project_inputs(M(), "project")
    assert (found["q"].kind, found["q"].domain) == ("register", "project")
    assert found["s"].kind == "undriven"  # a synchroniser output is not a project register
    assert found["w"].kind == "combinational"  # also assigned by logic: not accepted


def _clear_case(make_clear):
    from tt_harness.core.cdc import AsyncClearFlop

    class M(Module):
        def __init__(self) -> None:
            self.a, self.b, self.port, pin = (Signal() for _ in range(4))
            self.sync += [self.a.eq(~self.a), self.b.eq(self.a)]
            self.specials += AsyncClearFlop("project", make_clear(self), 1, q=pin)
            self.specials += Instance("thing", name="project", i_rst_n=pin)

    return inputs.project_inputs(M(), "project")["rst_n"]


def _wired_register(m) -> Signal:
    w = Signal()
    m.comb += w.eq(m.a)
    return w


def _synchronised(m) -> Signal:
    from migen.genlib.cdc import MultiReg

    o = Signal()
    m.specials += MultiReg(m.a, o, odomain="project")
    return o


@pytest.mark.parametrize(
    ("make_clear", "kind"),
    [
        (lambda m: m.a, "register"),
        (_wired_register, "register"),
        (lambda m: 0, "register"),
        (lambda m: m.a & ~m.b, "combinational"),  # the plan-1 `rst_n` glitch, on the clear
        (lambda m: ~m.a, "combinational"),
        (lambda m: m.port, "combinational"),  # nothing drives it: not shown glitch-free
        (_synchronised, "combinational"),
    ],
)
def test_the_clear_of_an_async_clear_flop_must_be_a_register_or_a_constant(
    make_clear, kind: str
) -> None:
    """`q` follows `clear` asynchronously, so a clear made by logic glitches the project input."""
    found = _clear_case(make_clear)
    assert found.kind == kind, found
    if kind == "register":
        assert found.domain == "project" and found.glitch_free
    else:
        assert "asynchronous clear" in found.detail and not found.glitch_free


def test_an_async_clear_flop_cleared_by_itself_is_refused() -> None:
    from tt_harness.core.cdc import AsyncClearFlop

    class M(Module):
        def __init__(self) -> None:
            pin = Signal()
            self.specials += AsyncClearFlop("project", pin, 1, q=pin)
            self.specials += Instance("thing", name="project", i_rst_n=pin)

    found = inputs.project_inputs(M(), "project")["rst_n"]
    assert found.kind == "combinational" and "loop" in found.detail
