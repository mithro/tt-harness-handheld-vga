# SPDX-License-Identifier: Apache-2.0
"""The crossings mark their own registers (`tt_harness.core.cdc.mark`) in the Verilog."""

from __future__ import annotations

import re

import pytest
from migen import ClockDomain, Module, Signal
from migen.fhdl import verilog
from migen.genlib.cdc import MultiReg

from tt_harness.core import cdc


class _Top(Module):
    def __init__(self) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_project = ClockDomain("project")
        self.submodules.x = x = cdc.MultiBitCrossing(8, idomain="sys", odomain="project")
        self.value = Signal(8)
        self.comb += self.value.eq(x.dst)  # an alias of `dst`, as a CSR reads it
        self.i = Signal()
        self.o = Signal()
        self.specials += MultiReg(self.i, self.o, "project")
        self.ios = {x.src, self.value, self.i, self.o}


def _verilog() -> str:
    top = _Top()
    return str(verilog.convert(top, top.ios, special_overrides=cdc.special_overrides()))


def _marked(text: str) -> dict[str, str]:
    """Register name -> role, from `(* ... tt_cdc = "<role>" ... *) reg ... name`."""
    out = {}
    for attrs, name in re.findall(r"\(\*([^*]*)\*\)\s*reg\s+(?:\[[^\]]*\]\s*)?(\w+)", text):
        m = re.search(rf'{cdc.CDC_ATTR} = "(\w+)"', attrs)
        if m:
            assert 'keep = "true"' in attrs, f"{name} is marked without keep"
            out[name] = m.group(1)
    return out


def test_every_crossing_register_is_marked_with_its_role_and_keep() -> None:
    marked = _marked(_verilog())
    assert marked == {
        "hold": cdc.CDC_SOURCE,
        "req": cdc.CDC_SOURCE,
        "ack_o": cdc.CDC_SOURCE,
        "dst": cdc.CDC_SYNC,
        # The first flop of each MultiReg (named by LiteX's lowering): the handshake's two
        # (req into project, ack back into sys) and the bench's own.
        "xilinxmultiregimpl00": cdc.CDC_SYNC,
        "xilinxmultiregimpl10": cdc.CDC_SYNC,
        "xilinxmultiregimpl20": cdc.CDC_SYNC,
    }


def test_mark_refuses_an_unknown_role_and_role_of_reads_it_back() -> None:
    s = cdc.mark(Signal(), cdc.CDC_SYNC)
    assert cdc.role_of(s.attr) == cdc.CDC_SYNC
    assert cdc.KEEP_ATTR in s.attr
    assert cdc.role_of(Signal().attr) is None
    with pytest.raises(ValueError, match="unknown crossing role"):
        cdc.mark(Signal(), "maybe")
    s.attr.add((cdc.CDC_ATTR, cdc.CDC_SOURCE))
    with pytest.raises(ValueError, match="more than one crossing role"):
        cdc.role_of(s.attr)
