# SPDX-License-Identifier: Apache-2.0
"""Who is using a board, and the ssh command line: against a fake board host."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
from board_fakes import BOARD, IDLE, NOW, UART, FakeHost, probe, terminal

from tt_harness.host import run


def _activity(probe: dict) -> run.Activity:
    return run.board_activity(BOARD, runner=FakeHost(probe=probe))


def test_a_login_alone_is_not_use():
    activity = _activity(IDLE)
    assert activity.busy is False
    assert activity.reasons == ()
    assert any("/dev/pts/2" in n and "idle" in n for n in activity.notes)


def test_no_sessions_at_all_is_not_use():
    assert _activity(probe()).busy is False


@pytest.mark.parametrize(("idle_s", "busy"), [(30, True), (599, True), (601, False)])
def test_terminal_activity_within_ten_minutes_is_use(idle_s, busy):
    activity = _activity(probe(terminals=[terminal(idle_s)]))
    assert activity.busy is busy
    assert bool(activity.reasons) is busy


def test_a_keystroke_counts_as_activity_as_much_as_output():
    terminal = {"name": "/dev/pts/3", "user": "pi", "read": NOW - 20, "written": NOW - 9000}
    assert _activity(probe(terminals=[terminal])).busy is True


@pytest.mark.parametrize("tool", ["openFPGALoader", "picocom", "tio", "minicom"])
def test_a_serial_or_loader_tool_running_is_use(tool):
    process = {"pid": 4242, "user": "pi", "args": f"{tool} -b 115200", "why": [f"runs {tool}"]}
    activity = _activity(probe(terminals=[terminal(70_000)], processes=[process]))
    assert activity.busy is True
    assert tool in activity.reasons[0] and "4242" in activity.reasons[0]


def test_a_process_holding_the_uart_is_use():
    process = {
        "pid": 77,
        "user": "pi",
        "args": "python3 logger.py",
        "why": ["has /dev/ttyUSB1 open"],
    }
    activity = _activity(probe(processes=[process]))
    assert activity.busy is True
    assert "/dev/ttyUSB1" in activity.reasons[0]


def test_a_missing_uart_is_refused():
    activity = _activity(probe(uart_exists=False))
    assert activity.busy is True
    assert UART in activity.reasons[0]


def test_a_clock_that_runs_backwards_is_treated_as_activity():
    # A terminal "used" in the future cannot be judged idle.
    assert _activity(probe(terminals=[terminal(-50)])).busy is True


@pytest.mark.parametrize(
    "output", [b"", b"ship7ohT\n", b"TTHR probe {not json\n", b"TTHR probe []\n"]
)
def test_an_unreadable_probe_is_busy_not_idle(output):
    class Broken(FakeHost):
        def run(self, argv, *, input=None, timeout):
            return run.Result(0, output, b"")

    activity = run.board_activity(BOARD, runner=Broken())
    assert activity.busy is True
    assert "check" in activity.reasons[0]


def test_a_failed_probe_command_is_busy_and_says_why():
    class Down(FakeHost):
        def run(self, argv, *, input=None, timeout):
            return run.Result(255, b"", b"ssh: connect to host ps1.fpgas.online: timed out\n")

    activity = run.board_activity(BOARD, runner=Down())
    assert activity.busy is True
    assert "timed out" in activity.reasons[0]


def test_what_could_not_be_inspected_is_said():
    assert any("40" in n for n in _activity(IDLE).notes)


# SshRunner: the ssh command line ------------------------------------------------------


def test_ssh_command_is_batch_mode_with_one_quoted_remote_command():
    runner = run.SshRunner(BOARD)
    argv = ["dd", "of=/home/pi/x y", "status=none; reboot", "$(id)"]
    cmd = runner.command(argv)
    assert cmd[0] == "ssh"
    assert "BatchMode=yes" in cmd
    assert any(c.startswith("ConnectTimeout=") for c in cmd)
    assert cmd[cmd.index("-p") + 1] == "10722"
    assert cmd[-2] == "pi@ps1.fpgas.online" and cmd[-3] == "--"
    assert shlex.split(cmd[-1]) == argv  # the remote shell sees exactly these words


def test_ssh_command_leaves_host_key_checking_to_the_users_configuration():
    text = " ".join(run.SshRunner(BOARD).command(["true"]))
    assert "StrictHostKeyChecking" not in text
    assert "UserKnownHostsFile" not in text


def test_ssh_command_with_a_known_hosts_file_only_makes_checking_stricter(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("")
    cmd = run.SshRunner(BOARD, known_hosts=known).command(["true"])
    assert f"UserKnownHostsFile={known}" in cmd
    assert "StrictHostKeyChecking=yes" in cmd


# a real board (deselected unless --board is given) -------------------------------------


@pytest.mark.board
def test_real_board_answers_the_activityprobe():
    """Read-only: asks the board host in $TT_BOARD (host:port:uart) who is using it."""
    host, port, uart = os.environ["TT_BOARD"].split(":", 2)
    known = os.environ.get("TT_BOARD_KNOWN_HOSTS")
    board = run.Board(host=host, port=int(port), uart=uart)
    runner = run.SshRunner(board, known_hosts=Path(known) if known else None)
    activity = run.board_activity(board, runner=runner)
    assert isinstance(activity.busy, bool)
    assert activity.busy == bool(activity.reasons)
