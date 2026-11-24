# SPDX-License-Identifier: Apache-2.0
"""MCU link against the firmware model (protocol sections 4-8), with no CPU attached."""

from __future__ import annotations

import pytest

from tt_harness.boards.gamebub.link import BUTTONS

from . import sim
from .firmware_model import (
    BUTTON_BITS,
    CORE_HALT,
    CORE_RUN,
    CTRL_CMD_HOST,
    CTRL_IRQ_ENABLE,
    DUMMY_CLOCKS,
    GET_STATUS,
    HOST_MAILBOX,
    IRQ_BUTTON,
    IRQ_CORE_REQUEST,
    OVERLAY,
    STATUS_SETUP,
    WORD_8,
    WORD_16,
    WORD_32,
    FirmwareError,
    FirmwareModel,
    FirmwareTimeout,
)

SYS_SETTLE_TICKS = 64  # > 3 sys cycles: time for a 2-flop synchroniser into sys


def run_link(body, *, reset=False, sys_body=None, **model_kwargs):
    dut = sim.make_dut()
    model = FirmwareModel(dut, **model_kwargs)
    result = {}

    def main():
        if reset:
            yield dut.link.reset.eq(1)
        result["value"] = yield from body(model, dut)

    sys_generators = [sys_body(dut)] if sys_body else []
    sim.run(dut, [main()], sys_generators)
    return model, result.get("value")


def read_sys(signal):
    """Read a sys-domain output after giving it time to cross."""
    yield from sim.wait_ticks(SYS_SETTLE_TICKS)
    return (yield signal)


@pytest.mark.parametrize(
    "rates",
    [
        pytest.param(None, id="firmware-rates-reads-10MHz"),
        pytest.param((40_000_000, 16_000_000), id="writes-40MHz-reads-16MHz"),
    ],
)
def test_boot_completes_without_cpu(rates):
    def body(model, dut):
        assert (yield from read_sys(dut.link.running)) == 0
        yield from model.boot()
        return (
            (yield from read_sys(dut.link.running)),
            (yield dut.link.focus),
        )

    kwargs = {} if rates is None else {"driver_rates_hz": rates}
    model, (running, focus) = run_link(body, **kwargs)
    assert model.running
    assert model.ignored_errors == []
    assert model.input_caps == 0
    assert model.input_state == set()
    assert running == 1
    assert focus == 1
    # Five commands (GET_STATUS twice, SETUP_COMPLETE, CORE_RUN, NOTIFY_FOCUS), each
    # finished at its first status read, so no poll ever had to sleep.
    status_reads = [t for t in model.transactions if t[:2] == ("r", CTRL_CMD_HOST)]
    assert len(status_reads) == 5
    reads = [t for t in model.transactions if t[0] == "r"]
    assert all(rate <= 16_000_000 for _, _, _, rate in reads)
    assert model.interrupts == IRQ_BUTTON | IRQ_CORE_REQUEST
    assert model.now < model.ticks(0.100)


@pytest.mark.parametrize("name", list(BUTTON_BITS))
def test_button_register_bit_order(name):
    def body(model, dut):
        yield dut.link.buttons.eq(1 << BUTTONS.index(name))
        yield from sim.wait_ticks(SYS_SETTLE_TICKS)
        return (yield from model.get_input_state())

    _, pressed = run_link(body)
    assert pressed == {name}


def test_unknown_command_errors_and_next_command_succeeds():
    def body(model, dut):
        with pytest.raises(FirmwareError) as info:
            yield from model.command([0x0300, 7, 8, 9])
        error_status = model.last_cmd_status
        valid = yield from read_sys(dut.link.cmd_valid)
        words = []
        for w in dut.link.cmd_words:
            words.append((yield w))
        (status,) = yield from model.command([GET_STATUS], 1)
        return info.value, error_status, valid, words, status

    _, (error, error_status, valid, words, status) = run_link(body)
    assert error.core_error == "CommandFailed"
    assert error.code == 0x0300
    assert error_status == 0b1011
    assert valid == 1
    assert words[:4] == [0x0300, 7, 8, 9]
    assert status == STATUS_SETUP


def test_cpu_acknowledges_forwarded_command():
    def body(model, dut):
        with pytest.raises(FirmwareError):
            yield from model.command([0x0404, 1])
        assert (yield from read_sys(dut.link.cmd_valid)) == 1
        yield dut.link.cmd_ack.eq(1)
        yield from sim.wait_ticks(8)
        yield dut.link.cmd_ack.eq(0)
        return (yield from read_sys(dut.link.cmd_valid))

    _, valid_after_ack = run_link(body)
    assert valid_after_ack == 0


def test_held_cmd_ack_does_not_clear_a_new_command_silently():
    """cmd_ack is a strobe acted on at its rising edge: a level held high across the arrival
    of a forwarded command must not capture and then clear it without a trace."""

    def body(model, dut):
        yield dut.link.cmd_ack.eq(1)  # held high from before the command arrives
        yield from sim.wait_ticks(8)
        with pytest.raises(FirmwareError):
            yield from model.command([0x0405, 11, 12])
        valid = yield from read_sys(dut.link.cmd_valid)
        words = []
        for w in dut.link.cmd_words:
            words.append((yield w))
        # A second command while the level is still held: kept first, flagged overrun.
        with pytest.raises(FirmwareError):
            yield from model.command([0x0406, 13])
        overrun = yield from read_sys(dut.link.cmd_overrun)
        still_valid = yield dut.link.cmd_valid
        # A real acknowledge is a new rising edge.
        yield dut.link.cmd_ack.eq(0)
        yield from sim.wait_ticks(8)
        yield dut.link.cmd_ack.eq(1)
        cleared = yield from read_sys(dut.link.cmd_valid)
        return valid, words, overrun, still_valid, cleared

    _, (valid, words, overrun, still_valid, cleared) = run_link(body)
    assert valid == 1
    assert words[:3] == [0x0405, 11, 12]
    assert (overrun, still_valid) == (1, 1)
    assert cleared == 0


def test_forwarded_command_overrun_keeps_first_and_flags():
    def ack(dut):
        yield dut.link.cmd_ack.eq(1)
        yield from sim.wait_ticks(8)
        yield dut.link.cmd_ack.eq(0)

    def words(dut):
        out = []
        for w in dut.link.cmd_words:
            out.append((yield w))
        return out

    def body(model, dut):
        with pytest.raises(FirmwareError):
            yield from model.command([0x0402, 1, 2])
        first = (yield from read_sys(dut.link.cmd_overrun)), (yield from words(dut))
        with pytest.raises(FirmwareError):
            yield from model.command([0x0403, 3, 4])
        second = (yield from read_sys(dut.link.cmd_overrun)), (yield from words(dut))
        yield from ack(dut)
        cleared = (yield from read_sys(dut.link.cmd_valid)), (yield dut.link.cmd_overrun)
        with pytest.raises(FirmwareError):
            yield from model.command([0x0404, 5])
        third = (yield from read_sys(dut.link.cmd_overrun)), (yield from words(dut))
        return first, second, cleared, third

    _, (first, second, cleared, third) = run_link(body)
    assert first[0] == 0 and first[1][:3] == [0x0402, 1, 2]
    assert second[0] == 1 and second[1][:3] == [0x0402, 1, 2]  # the first is not lost
    assert cleared == (0, 0)
    assert third[0] == 0 and third[1][:2] == [0x0404, 5]


# Section 2.1: the command bytes the firmware sends, from the documented bit fields.
DOCUMENTED_COMMAND_BYTES = {
    (False, WORD_8): 0x58,
    (True, WORD_8): 0x59,
    (False, WORD_16): 0x5A,
    (True, WORD_16): 0x5B,
    (False, WORD_32): 0x5C,
    (True, WORD_32): 0x5D,
}


@pytest.mark.parametrize(("read", "word_size"), list(DOCUMENTED_COMMAND_BYTES))
def test_command_byte_values_match_document(read, word_size):
    fields = int(read) | (word_size << 1) | (1 << 3) | (1 << 4) | (0b10 << 5)
    assert fields == DOCUMENTED_COMMAND_BYTES[(read, word_size)]
    assert FirmwareModel.command_byte(read, word_size, True, True) == fields


def test_boot_sends_only_documented_command_bytes():
    def body(model, dut):
        yield from model.boot()

    model, _ = run_link(body)
    assert set(model.command_bytes) == {0x5A, 0x5C, 0x5D}  # overlay; register W/R


CUT_POINTS = {
    "mid-command": ("w", 4),
    "mid-address": ("w", 8 + 5),
    "mid-data-nibble": ("w", 8 + 8 + 1),  # high nibble of CTRL_CMD_HOST byte 0 only
    "mid-read": ("r", 8 + 8 + DUMMY_CLOCKS + 3),
}


@pytest.mark.parametrize("cut", list(CUT_POINTS))
def test_early_cs_release_then_boot(cut):
    kind, clocks = CUT_POINTS[cut]

    def body(model, dut):
        yield from model.write_u32(HOST_MAILBOX, 0x1234)  # an unknown command, waiting
        read = kind == "r"
        cmd = model.command_byte(read, WORD_32, True, True)
        data = None if read else (0x8).to_bytes(4, "little")
        yield from model.raw_transaction(
            40_000_000 if not read else 10_000_000,
            cmd,
            CTRL_CMD_HOST,
            data=data,
            read_len=4,
            stop_after_clocks=clocks,
        )
        valid = yield from read_sys(dut.link.cmd_valid)
        yield from model.boot()
        return valid

    model, valid = run_link(body)
    assert valid == 0  # the cut write never started the unknown command
    assert model.running
    assert model.ignored_errors == []


def test_stray_falling_edge_before_first_rising_edge():
    def body(model, dut):
        yield dut.cs_n.eq(0)
        yield from sim.wait_ticks(4)
        yield dut.spi_fall.eq(1)
        yield
        yield dut.spi_fall.eq(0)
        yield from sim.wait_ticks(4)
        # Chip select stays low into a normal transaction.
        (status,) = yield from model.command([GET_STATUS], 1)
        yield from model.boot()
        return status

    model, status = run_link(body)
    assert status == STATUS_SETUP
    assert model.running


@pytest.mark.parametrize("addr", [0xF100_0F00, 0xF100_100C + 0x100, 0xF1FF_FFFC, 0xF000_0800])
def test_undefined_register_write_ignored_read_zero(addr):
    def body(model, dut):
        yield from model.write_u32(CTRL_IRQ_ENABLE, 0x12)
        yield from model.write_u32(addr, 0xDEAD_BEEF)
        return (
            (yield from model.read_u32(addr)),
            (yield from model.read_u32(CTRL_IRQ_ENABLE)),
        )

    _, (value, enable) = run_link(body)
    assert value == 0
    assert enable == 0x12


def test_mailbox_and_overlay():
    def body(model, dut):
        yield from model.write(HOST_MAILBOX + 8, bytes(range(1, 13)), 2)
        mailbox = yield from model.read(HOST_MAILBOX + 8, 12, 2)
        yield from model.write_overlay(0x100, b"\x55\xaa" * 4)
        overlay = yield from model.read_u32(OVERLAY | 0x100)
        return mailbox, overlay

    _, (mailbox, overlay) = run_link(body)
    assert mailbox == bytes(range(1, 13))
    assert overlay == 0  # overlay writes are accepted and ignored


def test_boot_against_link_in_reset_times_out():
    def body(model, dut):
        with pytest.raises(FirmwareTimeout) as info:
            yield from model.boot()
        return info.value

    model, error = run_link(body, reset=True)
    # A GET_STATUS timeout during the status poll is reported as a failed command.
    assert error.core_error == "CommandFailed"
    assert error.code == GET_STATUS
    assert model.ticks(0.100) < model.now < model.ticks(0.150)
    # The abort path dropped CoreRequest from the enable mask (never set here).
    assert model.interrupts == IRQ_BUTTON


def test_button_interrupt_after_boot():
    """Checked where the firmware sees it: the MCU's line, after the board's n-FET."""

    def body(model, dut):
        yield from model.boot()
        yield from sim.wait_ticks(SYS_SETTLE_TICKS)
        idle = yield from model.mcu_int_n()
        yield dut.link.buttons.eq(1 << BUTTONS.index("start"))
        yield from sim.wait_ticks(SYS_SETTLE_TICKS)
        asserted = yield from model.mcu_int_n()
        pending = yield from model.service_interrupt()
        yield from sim.wait_ticks(SYS_SETTLE_TICKS)
        released = yield from model.mcu_int_n()
        return idle, asserted, pending, released, model.input_state

    _, (idle, asserted, pending, released, state) = run_link(body)
    assert (idle, asserted, released) == (1, 0, 1)
    assert pending == IRQ_BUTTON
    assert state == {"start"}


def test_reset_core_halts_and_runs():
    def body(model, dut):
        yield from model.boot()
        yield from model.command([CORE_HALT])
        halted = yield from read_sys(dut.link.running)
        yield from model.command([CORE_RUN])
        running = yield from read_sys(dut.link.running)
        return halted, running

    model, (halted, running) = run_link(body)
    assert (halted, running) == (0, 1)
    assert model.ignored_errors == []
