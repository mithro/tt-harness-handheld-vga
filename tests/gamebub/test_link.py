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
    GET_STATUS,
    HOST_MAILBOX,
    IRQ_BUTTON,
    IRQ_CORE_REQUEST,
    OVERLAY,
    STATUS_SETUP,
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
    def body(model, dut):
        yield from model.boot()
        idle = yield from read_sys(dut.link.irq_n)
        yield dut.link.buttons.eq(1 << BUTTONS.index("start"))
        asserted = yield from read_sys(dut.link.irq_n)
        pending = yield from model.service_interrupt()
        released = yield from read_sys(dut.link.irq_n)
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
