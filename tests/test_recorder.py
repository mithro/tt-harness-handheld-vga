# SPDX-License-Identifier: Apache-2.0
"""StepRecorder tests (Migen simulator).

The behavioural "project" here is system-domain logic that acts on a detected rising edge of
`pclk`. Migen's simulator only drives clock domains from its own clock generators, so a domain
clocked by the `pclk` register cannot be simulated directly. Edge detection makes the project's
registers update one system cycle after `pclk` rises, which stands in for the clock-buffer and
clock-to-output delay of the real design; the recorder's `settle` wait has to cover it, so the
test also checks that the sample is not taken too early.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import pytest
from migen import If, Module, Signal
from migen.sim import run_simulation

from tt_harness.compare import recorder
from tt_harness.compare.recorder import StepRecorder
from tt_harness.core.stimulus import ReferenceStimulus


class _CounterProject(Module):
    """A Tiny Tapeout-like project: free-running 8-bit counter on `uo`, synchronous `rst_n`."""

    def __init__(self, pclk: Signal, rst_n: Signal) -> None:
        self.uo = Signal(8)
        pclk_prev = Signal()
        self.sync += [
            pclk_prev.eq(pclk),
            If(
                pclk & ~pclk_prev,
                If(~rst_n, self.uo.eq(0)).Else(self.uo.eq(self.uo + 1)),
            ),
        ]


class _Bench(Module):
    def __init__(self, sys_clk_freq: int, baud: int, clocks: int, settle: int) -> None:
        self.submodules.rec = StepRecorder(sys_clk_freq, baud, clocks, settle)
        self.submodules.stim = ReferenceStimulus(ui_in=0, uio_in=0)
        self.submodules.proj = _CounterProject(self.rec.pclk, self.stim.rst_n)
        self.comb += [
            self.stim.clock_index.eq(self.rec.clock_index),
            self.rec.uo.eq(self.proj.uo),
        ]


@dataclass
class _Run:
    bit_cycles: int
    tx: list[int]
    pclk: list[int]
    done: list[int]
    strobe: list[int]
    clock_index: list[int]
    rst_n: list[int]


def _simulate(sys_clk_freq: int, baud: int, clocks: int, settle: int = 2) -> _Run:
    bench = _Bench(sys_clk_freq, baud, clocks, settle)
    bit_cycles = sys_clk_freq // baud
    frame_bits = 10 * recorder.frame_length(clocks)
    step_cycles = 2 * (settle + 1) + 1
    limit = (frame_bits + 10) * (bit_cycles + 1) + (clocks + 20) * step_cycles + 1000
    run = _Run(bit_cycles, [], [], [], [], [], [])
    extra = 4 * 10 * bit_cycles  # keep watching the line after done

    def gen():
        after_done = None
        for _ in range(limit):
            run.tx.append((yield bench.rec.tx))
            run.pclk.append((yield bench.rec.pclk))
            run.done.append((yield bench.rec.done))
            run.strobe.append((yield bench.rec.sample_strobe))
            run.clock_index.append((yield bench.rec.clock_index))
            run.rst_n.append((yield bench.stim.rst_n))
            if run.done[-1] and after_done is None:
                after_done = extra
            if after_done is not None:
                after_done -= 1
                if after_done == 0:
                    return
            yield
        raise AssertionError("recorder did not finish")

    run_simulation(bench, gen())
    return run


def _uart_decode(tx: list[int], bit_cycles: int) -> tuple[bytes, list[int]]:
    """Decode 8N1 bytes from a per-cycle line trace; also return each stop bit's end cycle."""
    out = bytearray()
    stop_ends = []
    c = 1
    while c < len(tx):
        if tx[c - 1] == 1 and tx[c] == 0:  # start bit's falling edge
            mid = c + bit_cycles // 2
            assert tx[mid] == 0, f"glitch at cycle {c}"
            value = 0
            for bit in range(8):
                value |= tx[mid + (bit + 1) * bit_cycles] << bit
            stop_mid = mid + 9 * bit_cycles
            assert tx[stop_mid] == 1, f"framing error in byte starting at cycle {c}"
            out.append(value)
            stop_ends.append(c + 10 * bit_cycles)
            c = stop_mid
        else:
            c += 1
    return bytes(out), stop_ends


def _expected_payload(clocks: int, reset_clocks: int = 10) -> bytes:
    return bytes(max(0, i - reset_clocks + 1) & 0xFF for i in range(clocks))


def _expected_frame(clocks: int) -> bytes:
    payload = _expected_payload(clocks)
    return (
        b"TTHC"
        + bytes([1])
        + struct.pack("<I", clocks)
        + payload
        + struct.pack("<I", zlib.crc32(payload))
    )


def _phases(pclk: list[int]) -> list[tuple[int, int]]:
    """(level, length) of each complete constant run of pclk between two transitions."""
    edges = [c for c in range(1, len(pclk)) if pclk[c] != pclk[c - 1]]
    return [(pclk[a], b - a) for a, b in zip(edges, edges[1:], strict=False)]


def _rises(pclk: list[int]) -> list[int]:
    return [c for c in range(1, len(pclk)) if pclk[c] and not pclk[c - 1]]


def test_frame_length() -> None:
    assert recorder.frame_length(0) == 13
    assert recorder.frame_length(300) == 313
    assert len(_expected_frame(300)) == recorder.frame_length(300)


def test_expected_payload_shape() -> None:
    assert _expected_payload(13) == bytes([0] * 10 + [1, 2, 3])


@pytest.fixture(scope="module")
def short_run() -> _Run:
    return _simulate(sys_clk_freq=20, baud=10, clocks=40, settle=2)


@pytest.fixture(scope="module")
def long_run() -> _Run:
    # Eight system cycles per bit: one byte takes 80 cycles, a project step only 6, so the
    # UART is the bottleneck and the recorder must stall on every byte.
    return _simulate(sys_clk_freq=80, baud=10, clocks=300, settle=3)


def test_frame_is_exact(short_run: _Run) -> None:
    data, _ = _uart_decode(short_run.tx, short_run.bit_cycles)
    assert data == _expected_frame(40)


def test_payload_is_reset_zeros_then_counting(short_run: _Run) -> None:
    data, _ = _uart_decode(short_run.tx, short_run.bit_cycles)
    payload = data[9:-4]
    assert payload[:10] == bytes(10)
    assert list(payload[10:]) == list(range(1, 31))
    assert data[-4:] == struct.pack("<I", zlib.crc32(payload))


@pytest.mark.parametrize("run_name, settle", [("short_run", 2), ("long_run", 3)])
def test_pclk_phases_not_shorter_than_settle(
    request: pytest.FixtureRequest, run_name: str, settle: int
) -> None:
    run: _Run = request.getfixturevalue(run_name)
    phases = _phases(run.pclk)
    assert phases, "pclk never toggled"
    for level, length in phases:
        assert length >= settle, f"pclk {'high' if level else 'low'} for {length} cycles"
    high = [n for level, n in phases if level]
    low = [n for level, n in phases if not level]
    assert min(low) >= max(high)


@pytest.mark.parametrize("run_name, clocks", [("short_run", 40), ("long_run", 300)])
def test_pclk_rises_exactly_clocks_times(
    request: pytest.FixtureRequest, run_name: str, clocks: int
) -> None:
    run: _Run = request.getfixturevalue(run_name)
    assert len(_rises(run.pclk)) == clocks
    assert sum(run.strobe) == clocks
    assert run.pclk[-1] == 0


@pytest.mark.parametrize("run_name", ["short_run", "long_run"])
def test_clock_index_and_reset_at_each_rising_edge(
    request: pytest.FixtureRequest, run_name: str
) -> None:
    """Rising edge k is project clock k: clock_index == k and rst_n == (k >= 10) around it."""
    run: _Run = request.getfixturevalue(run_name)
    for k, c in enumerate(_rises(run.pclk)):
        # The cycle before the edge (setup) and the cycle after it (hold) both see clock k.
        for cycle in (c - 1, c):
            assert run.clock_index[cycle] == k, f"rise {k} at cycle {c}"
            assert run.rst_n[cycle] == (1 if k >= 10 else 0), f"rise {k} at cycle {c}"


@pytest.mark.parametrize("run_name", ["short_run", "long_run"])
def test_clock_index_changes_only_while_pclk_low(
    request: pytest.FixtureRequest, run_name: str
) -> None:
    run: _Run = request.getfixturevalue(run_name)
    for c in range(1, len(run.pclk)):
        if run.clock_index[c] != run.clock_index[c - 1]:
            assert run.pclk[c - 1] == 0 and run.pclk[c] == 0, f"cycle {c}"


def test_uart_bottleneck_loses_and_duplicates_nothing(long_run: _Run) -> None:
    data, _ = _uart_decode(long_run.tx, long_run.bit_cycles)
    assert data == _expected_frame(300)
    # 300 clocks wrap the 8-bit counter: 291 counts from 1 reach 255 then 0..35.
    assert data[9 + 10 + 254] == 255
    assert data[9 + 10 + 255] == 0


@pytest.mark.parametrize("run_name", ["short_run", "long_run"])
def test_done_rises_after_last_stop_bit(request: pytest.FixtureRequest, run_name: str) -> None:
    run: _Run = request.getfixturevalue(run_name)
    _, stop_ends = _uart_decode(run.tx, run.bit_cycles)
    first_done = run.done.index(1)
    assert first_done >= stop_ends[-1]
    assert first_done <= stop_ends[-1] + 2 * run.bit_cycles
    assert all(run.done[first_done:])
    assert all(run.tx[stop_ends[-1] - run.bit_cycles :]), "line not idle high after the frame"


def test_zero_clocks_sends_header_and_empty_crc() -> None:
    run = _simulate(sys_clk_freq=20, baud=10, clocks=0, settle=2)
    data, _ = _uart_decode(run.tx, run.bit_cycles)
    assert data == _expected_frame(0)
    assert not _rises(run.pclk)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sys_clk_freq": 10, "baud": 20, "clocks": 1},  # baud above the system clock
        {"sys_clk_freq": 25, "baud": 10, "clocks": 1},  # divider 2 or 3: baud error > 2%
        {"sys_clk_freq": 20, "baud": 10, "clocks": -1},
        {"sys_clk_freq": 20, "baud": 10, "clocks": 2**32},
        {"sys_clk_freq": 20, "baud": 10, "clocks": 1, "settle": 0},
    ],
)
def test_rejects_bad_parameters(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        StepRecorder(**kwargs)
