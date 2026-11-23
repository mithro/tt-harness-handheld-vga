# SPDX-License-Identifier: Apache-2.0
"""The core and compare gateware converts to plain Verilog with no vendor primitives."""

from __future__ import annotations

import re

import pytest
from migen.fhdl import verilog

from tt_harness.compare.recorder import StepRecorder
from tt_harness.core.sampler import UoSampler
from tt_harness.core.stimulus import ReferenceStimulus

# Xilinx, Lattice and Gowin clocking/IO primitives that must not appear in portable modules.
VENDOR_PRIMITIVE = re.compile(
    r"\b(BUFG\w*|BUFH\w*|BUFR|BUFIO|MMCM\w*|PLLE\w*|IBUF\w*|OBUF\w*|IOBUF\w*|IDDR\w*|ODDR\w*"
    r"|DCCA|EHXPLL\w*|rPLL|PLLVR|CLKDIV\w*|SB_\w+|TRELLIS_\w+)\b"
)


def _sampler():
    m = UoSampler()
    return m, {m.uo, m.strobe, m.data, m.valid, m.count}


def _stimulus():
    m = ReferenceStimulus(ui_in=0x12, uio_in=0x34)
    return m, {m.clock_index, m.ena, m.rst_n, m.ui_in, m.uio_in}


def _recorder():
    m = StepRecorder(sys_clk_freq=50_000_000, baud=1_000_000, clocks=1000)
    return m, {m.uo, m.pclk, m.clock_index, m.sample_strobe, m.tx, m.done}


@pytest.mark.parametrize("build", [_sampler, _stimulus, _recorder])
def test_converts_without_vendor_primitives(build) -> None:
    module, ios = build()
    text = str(verilog.convert(module, ios=ios))
    assert "module top" in text
    found = VENDOR_PRIMITIVE.findall(text)
    assert not found, f"vendor primitives in generated Verilog: {sorted(set(found))}"
