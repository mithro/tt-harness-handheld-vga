# SPDX-License-Identifier: Apache-2.0
"""QSPI slave against the firmware model's bit-level transactions (protocol section 2).

Writes run at 40 MHz and reads at the firmware's 16 MHz cap, the fastest the firmware
allows for each.
"""

from __future__ import annotations

import random

import pytest

from . import sim
from .firmware_model import (
    DUMMY_CLOCKS,
    WORD_8,
    WORD_16,
    WORD_32,
    FirmwareModel,
)

WRITE_HZ = 40_000_000
READ_HZ = 16_000_000
BASE = 0x0001_2340


def run_with_memory(body, mem=None, **model_kwargs):
    """Run ``body(model)`` against the slave with a byte memory on its bus port."""
    dut = sim.make_dut(with_link=False)
    model = FirmwareModel(dut, **model_kwargs)
    mem = {} if mem is None else mem
    writes: list[tuple[int, int]] = []
    reads: list[int] = []
    result = {}

    def main():
        result["value"] = yield from body(model)

    sim.run(dut, [main(), sim.bus_memory(dut, mem, writes, reads)])
    return result.get("value"), mem, writes, reads


@pytest.mark.parametrize("word_size", [WORD_8, WORD_16, WORD_32])
def test_single_write_each_word_size(word_size):
    data = bytes([0xA1, 0xB2, 0xC3, 0xD4][: 1 << word_size])

    def body(model):
        yield from model.write(BASE, data, word_size)

    _, mem, writes, _ = run_with_memory(body)
    assert writes == [(BASE + i, b) for i, b in enumerate(data)]


@pytest.mark.parametrize("word_size", [WORD_8, WORD_16, WORD_32])
def test_single_read_each_word_size(word_size):
    n = 1 << word_size
    mem = {BASE + i: 0x5A ^ (17 * i) for i in range(n)}

    def body(model):
        return (yield from model.read(BASE, n, word_size, max_clock_hz=READ_HZ))

    value, *_ = run_with_memory(body, mem, driver_rates_hz=(WRITE_HZ, READ_HZ))
    assert value == bytes(mem[BASE + i] for i in range(n))


def test_auto_increment_burst_16k():
    rng = random.Random(1234)
    data = bytes(rng.randrange(256) for _ in range(16 * 1024))

    def body(model):
        yield from model.write(BASE, data, WORD_32)
        return (yield from model.read(BASE + len(data) - 32, 32, WORD_32, max_clock_hz=READ_HZ))

    value, mem, writes, _ = run_with_memory(body, driver_rates_hz=(WRITE_HZ, READ_HZ))
    assert len(writes) == len(data)
    assert bytes(mem[BASE + i] for i in range(len(data))) == data
    assert value == data[-32:]


def test_no_auto_increment_rewrites_one_word():
    def body(model):
        yield from model.write(BASE, bytes(range(1, 9)), WORD_32, auto_increment=False)

    _, mem, writes, _ = run_with_memory(body)
    assert {a for a, _ in writes} == {BASE, BASE + 1, BASE + 2, BASE + 3}
    assert [mem[BASE + i] for i in range(4)] == [5, 6, 7, 8]


@pytest.mark.parametrize("word_size", [WORD_16, WORD_32])
def test_byte_swap_bit(word_size):
    n = 1 << word_size
    data = bytes(range(0x10, 0x10 + 2 * n))
    # Without byte swap each word is sent highest address first (section 2.2).
    expected = b"".join(data[i : i + n][::-1] for i in range(0, len(data), n))

    def body(model):
        yield from model.write(BASE, data, word_size, byte_swap=False)
        swapped_back = yield from model.read(
            BASE, len(data), word_size, max_clock_hz=READ_HZ, byte_swap=False
        )
        plain = yield from model.read(BASE, len(data), word_size, max_clock_hz=READ_HZ)
        return swapped_back, plain

    (swapped_back, plain), mem, _, _ = run_with_memory(body, driver_rates_hz=(WRITE_HZ, READ_HZ))
    assert bytes(mem[BASE + i] for i in range(len(data))) == expected
    assert swapped_back == data
    assert plain == expected


def test_read_data_after_exactly_the_dummy_cycles():
    mem = {BASE: 0x12, BASE + 1: 0x34}
    samples: list[int] = []

    def body(model):
        cmd = model.command_byte(True, WORD_8, True, True)
        return (
            yield from model.raw_transaction(READ_HZ, cmd, BASE, read_len=2, samples=samples)
        )

    value, *_ = run_with_memory(body, mem, undriven=0xF)
    first = 8 + 8 + DUMMY_CLOCKS
    assert value == b"\x12\x34"
    assert samples[first : first + 4] == [0x1, 0x2, 0x3, 0x4]
    # The device does not drive during command and address (BusContention otherwise),
    # and nothing it puts out before clock 33 is data.
    assert len(samples) == first + 4


def test_cs_released_mid_address_leaves_slave_ready():
    def body(model):
        cmd = model.command_byte(False, WORD_32, True, True)
        yield from model.raw_transaction(
            WRITE_HZ, cmd, 0x0BAD_0000, data=b"\xee" * 4, stop_after_clocks=12
        )
        yield from model.write(BASE, b"\x01\x02\x03\x04", WORD_32)

    _, _, writes, _ = run_with_memory(body)
    assert writes == [(BASE, 1), (BASE + 1, 2), (BASE + 2, 3), (BASE + 3, 4)]


def test_cs_released_mid_read_then_write():
    mem = {BASE: 0x77}

    def body(model):
        cmd = model.command_byte(True, WORD_32, True, True)
        yield from model.raw_transaction(
            READ_HZ, cmd, BASE, read_len=4, stop_after_clocks=8 + 8 + DUMMY_CLOCKS + 3
        )
        yield from model.write(BASE + 4, b"\x09", WORD_8)
        return (yield from model.read(BASE, 1, WORD_8, max_clock_hz=READ_HZ))

    value, _, writes, _ = run_with_memory(body, mem, driver_rates_hz=(WRITE_HZ, READ_HZ))
    assert writes == [(BASE + 4, 9)]
    assert value == b"\x77"


def test_back_to_back_transactions_one_clock_cs_high():
    def body(model):
        yield from model.write(BASE, b"\xaa\xbb", WORD_16)
        yield from model.write(BASE + 2, b"\xcc\xdd", WORD_16)
        first = yield from model.read(BASE, 4, WORD_32, max_clock_hz=READ_HZ)
        second = yield from model.read(BASE + 1, 1, WORD_8, max_clock_hz=READ_HZ)
        yield from model.write(BASE + 3, b"\xee", WORD_8)
        return first, second

    (first, second), mem, _, _ = run_with_memory(
        body, driver_rates_hz=(WRITE_HZ, READ_HZ), cs_high_periods=1
    )
    assert first == b"\xaa\xbb\xcc\xdd"
    assert second == b"\xbb"
    assert mem[BASE + 3] == 0xEE


def test_command_with_other_line_width_is_ignored():
    def body(model):
        single_line_write = model.command_byte(False, WORD_8, True, True) & ~(0b11 << 5)
        yield from model.raw_transaction(WRITE_HZ, single_line_write, BASE, data=b"\x42")
        yield from model.write(BASE + 1, b"\x43", WORD_8)

    _, _, writes, _ = run_with_memory(body)
    assert writes == [(BASE + 1, 0x43)]
