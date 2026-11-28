# SPDX-License-Identifier: Apache-2.0
"""Structure of every clock-domain crossing in `ProjectSlot` + `AtSpeedCheck` together.

The simulator cannot see a register that samples a bus of the other domain (there the first
flop is just a flop), so these tests read the Migen fragment (`tests/cdc_structure.py`): clock
domains are propagated through combinational logic and through the modules' ports, and the
ONLY things allowed to pass between `sys` and `project` are

- the four held-bus reads of the `MultiBitCrossing` handshakes (`ctl_x`, `buttons_x`, `cmd_x`,
  `rep_x`): the one statement `dst <= hold`, nothing else reading `hold`;
- the inputs of the two-flop `MultiReg` synchronisers, each a plain register (plus the MMCM's
  asynchronous LOCKED), each exactly 2 stages;
- the clear of the two `AsyncClearFlop`s of the reset chain: the plain `sys` register `rst_req`.

Anything else (a `sys` port sampled by a `project` register, a result taken from `hold`, a
synchroniser of another length, a combinational clear) fails. The analyser fails closed: a
signal it cannot give a domain is an error.
"""

from __future__ import annotations

import cdc_structure
import pytest
from migen import ClockDomain, If, Instance, Module, Signal
from migen.fhdl import verilog
from migen.genlib.cdc import MultiReg

from tt_harness.core.atspeed import AtSpeedCheck
from tt_harness.core.cdc import AsyncClearFlop
from tt_harness.core.slot import ProjectSlot

# The clocking primitives: their outputs are clocks or asynchronous, never data of a domain.
PRIMITIVES = {"MMCME2_ADV": "mmcm", "BUFG": "clock", "BUFGCE": "clock"}


class _Top(Module):
    def __init__(self) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clk_in = Signal()
        self.submodules.slot = ProjectSlot("tt_um_example", self.clk_in, 100_000_000, 25_175_000)
        self.submodules.check = AtSpeedCheck(ui_in=0x5A)
        self.slot.attach(self.check)

    def ports(self) -> dict:
        s, c = self.slot, self.check
        sys_ports = (s.ena, s.rst_n, s.ui_in, s.gamepad_enable, s.gamepad_buttons, s.clock_enable)
        out = dict.fromkeys((*sys_ports, c.start, c.abort, c.clocks), "sys")
        out[self.clk_in] = "clock"
        out[s.clock.reset] = "sys"  # the MMCM's reset: nothing drives it yet
        return out


@pytest.fixture(scope="module")
def top_and_analysis():
    top = _Top()
    return top, cdc_structure.analyse(top, top.ports(), {"project": "project", **PRIMITIVES})


def test_the_analysis_classifies_everything(top_and_analysis) -> None:
    _, a = top_and_analysis
    assert a.errors == []
    assert [cdc_structure.name(s) for s in a.mixed] == []


def test_the_only_cross_domain_reads_are_the_four_held_buses(top_and_analysis) -> None:
    top, a = top_and_analysis
    crossings = {
        "ctl_x": (top.slot.ctl_x, "project"),
        "buttons_x": (top.slot.buttons_x, "project"),
        "cmd_x": (top.check.cmd_x, "project"),
        "rep_x": (top.check.rep_x, "sys"),
    }
    expected = {(domain, id(x.dst), id(x.hold)) for x, domain in crossings.values()}
    found = set()
    for domain, targets, source in a.reads:
        described = f"{domain} registers {sorted(map(cdc_structure.name, targets))} read "
        described += f"{cdc_structure.name(source)} of {sorted(a.domains.get(source, []))}"
        assert len(targets) == 1, f"not a handshake capture: {described}"
        key = (domain, id(next(iter(targets))), id(source))
        assert key in expected, f"a crossing that is not a held-bus read: {described}"
        found.add(key)
    assert found == expected
    assert len(a.reads) == 4, "a held bus is read by more than its one capture statement"


def test_every_synchroniser_is_two_flops_fed_by_a_plain_register(top_and_analysis) -> None:
    top, a = top_and_analysis
    slot, check = top.slot, top.check
    sys_sources = [slot.ctl_x.req, slot.buttons_x.req, check.cmd_x.req, check.rep_x.ack_o]
    project_sources = [slot.ctl_x.ack_o, slot.buttons_x.ack_o, check.cmd_x.ack_o, check.rep_x.req]
    expected = {id(s): ("sys", "project") for s in sys_sources}
    expected |= {id(s): ("project", "sys") for s in project_sources}
    seen = []
    locked = gate_enable = alive = 0
    for m in a.multiregs:
        what = f"MultiReg from {cdc_structure.name(m.source)} into {m.odomain}"
        assert m.stages == 2, f"{what}: {m.stages} stage(s), not 2"
        if m.source is slot.clock.locked:  # the MMCM's LOCKED: asynchronous, straight in
            assert m.odomain == "sys" and m.source_domains == {"mmcm"}
            locked += 1
        elif m.odomain == "project_free":
            # `ProjectClock`'s clock-enable synchroniser, fed by `enable & locked`: an
            # expression (recorded for the build task; the clocking block is not changed here).
            assert m.source_domains == {"sys", "mmcm"} and not m.source_is_plain_register
            gate_enable += 1
        elif id(m.source) in expected:
            source_domain, odomain = expected[id(m.source)]
            assert m.source_is_plain_register, f"{what}: the source is not a plain register"
            assert m.source_domains == {source_domain} and m.odomain == odomain, what
            seen.append(id(m.source))
        else:  # the alive toggle: the one single-bit level, project -> sys
            assert m.source_is_plain_register and m.source_domains == {"project"}, what
            assert m.odomain == "sys", what
            alive += 1
    assert sorted(seen) == sorted(expected), "a handshake wire has no synchroniser (or two)"
    assert (locked, gate_enable, alive) == (1, 1, 1)
    assert len(a.multiregs) == 11


def test_the_reset_flops_are_cleared_by_one_plain_sys_register(top_and_analysis) -> None:
    top, a = top_and_analysis
    assert len(a.flops) == 2
    for f in a.flops:
        assert f.domain == "project"
        assert f.clear is top.slot.rst_req and f.clear_is_plain_register
        assert f.clear_domains == {"sys"}
        assert f.d_domains == {"project"}
    assert sum(f.special.q is top.slot.pin_rst_n for f in a.flops) == 1


def test_every_project_input_depends_on_the_project_domain_only(top_and_analysis) -> None:
    _, a = top_and_analysis
    assert a.instance_inputs["project"] == {
        "clk": {"clock"},
        "rst_n": {"project"},
        "ena": {"project"},
        "ui_in": {"project"},
        "uio_in": set(),
    }


def test_multireg_two_stages_is_two_flip_flops_in_the_emitted_verilog() -> None:
    """`n=2` is what Migen emits as two registers in the destination domain."""

    class M(Module):
        def __init__(self) -> None:
            self.clock_domains.cd_sys = ClockDomain("sys")
            self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
            self.a, self.o = Signal(), Signal()
            self.sync += self.a.eq(~self.a)
            self.specials += MultiReg(self.a, self.o, odomain="project", n=2)

    m = M()
    text = str(verilog.convert(m, ios={m.o}))
    block = text[text.index("always @(posedge project_clk)") :]
    block = block[: block.index("end")]
    assert block.count("<=") == 2, block


# The analyser itself: what it must refuse ------------------------------------------------------


class _Bad(Module):
    def __init__(self, kind: str) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
        self.port = Signal(8)  # a `sys` port of the module
        self.a = Signal(8)
        self.b = Signal(8)
        self.q = Signal(8)
        self.sync += self.a.eq(self.a + 1)
        self.declared = {self.port: "sys"}
        sp = self.sync.project
        if kind == "port_sampled":
            sp += self.q.eq(self.port)
        elif kind == "through_comb":
            w1, w2 = Signal(8), Signal(8)
            self.comb += [w1.eq(self.port), w2.eq(w1 ^ 0x55)]
            sp += self.q.eq(w2)
        elif kind == "condition":
            sp += If(self.a[0], self.q.eq(0))
        elif kind == "mixed_comb":
            sp += self.b.eq(self.b + 1)
            w = Signal(8)
            self.comb += w.eq(self.a & self.b)
            sp += self.q.eq(w)
        elif kind == "undeclared_port":
            other = Signal(8)
            sp += self.q.eq(other)
        elif kind == "undeclared_instance":
            out = Signal(8)
            self.specials += Instance("thing", o_x=out)
            sp += self.q.eq(out)
        elif kind == "two_domains":
            self.sync += self.q.eq(1)
            sp += self.q.eq(0)
        elif kind == "clean":
            sp += self.q.eq(self.q + 1)
        else:
            raise ValueError(kind)


@pytest.mark.parametrize(
    ("kind", "reads", "errors"),
    [
        ("clean", 0, 0),
        ("port_sampled", 1, 0),
        ("through_comb", 1, 0),
        ("condition", 1, 0),
        ("mixed_comb", 1, 0),
        ("undeclared_port", 0, 1),
        ("undeclared_instance", 0, 1),
        ("two_domains", 0, 1),
    ],
)
def test_the_analyser_sees_each_kind_of_crossing_and_fails_closed(
    kind: str, reads: int, errors: int
) -> None:
    m = _Bad(kind)
    a = cdc_structure.analyse(m, m.declared, {})
    assert len(a.reads) == reads, [cdc_structure.name(r[2]) for r in a.reads]
    assert len(a.errors) >= errors and bool(a.errors) == bool(errors), a.errors
    if kind == "mixed_comb":
        assert len(a.mixed) == 1


def test_the_analyser_reports_synchroniser_and_flop_sources() -> None:
    class M(Module):
        def __init__(self) -> None:
            self.clock_domains.cd_sys = ClockDomain("sys")
            self.clock_domains.cd_project = ClockDomain("project", reset_less=True)
            a, b, o1, o2 = Signal(), Signal(), Signal(), Signal()
            self.sync += [a.eq(~a), b.eq(a)]
            self.specials += [
                MultiReg(a, o1, odomain="project", n=1),
                MultiReg(a & b, o2, odomain="project", n=2),
                AsyncClearFlop("project", a & ~b, 1),
                AsyncClearFlop("project", a, o1),
            ]

    a = cdc_structure.analyse(M(), {}, {})
    assert a.errors == []
    found = sorted((m.stages, m.source_is_plain_register) for m in a.multiregs)
    assert found == [(1, True), (2, False)]
    assert sorted(f.clear_is_plain_register for f in a.flops) == [False, True]
    assert [f.clear_domains for f in a.flops] == [{"sys"}, {"sys"}]
