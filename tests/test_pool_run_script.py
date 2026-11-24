# SPDX-License-Identifier: Apache-2.0
"""`scripts/pool_run.py` prints a terminal line however it ends."""

from __future__ import annotations

import importlib.util
import os
import signal
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pool_run.py"


@pytest.fixture
def pool_run():
    spec = importlib.util.spec_from_file_location("pool_run_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _last(capsys) -> str:
    return capsys.readouterr().out.strip().splitlines()[-1]


def test_terminal_line_on_normal_end(pool_run, monkeypatch, capsys) -> None:
    monkeypatch.setattr(pool_run, "cmd_build", lambda args: 1)
    assert pool_run.main(["build"]) == 1
    assert _last(capsys).endswith("pool_run build finished rc=1")


def test_terminal_line_on_uncaught_exception(pool_run, monkeypatch, capsys) -> None:
    def boom(args):
        raise ZeroDivisionError

    monkeypatch.setattr(pool_run, "cmd_build", boom)
    with pytest.raises(ZeroDivisionError):
        pool_run.main(["build"])
    assert _last(capsys).endswith("pool_run build finished rc=exception")


def test_terminal_line_on_usage_error(pool_run, capsys) -> None:
    with pytest.raises(SystemExit):
        pool_run.main(["hw"])  # the board arguments are missing
    assert _last(capsys).endswith("pool_run ? finished rc=exit 2")


def test_terminal_line_on_sigterm_and_handler_restored(pool_run, monkeypatch, capsys) -> None:
    def killed(args):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(10)  # the handler raises before this returns
        return 0

    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(pool_run, "cmd_build", killed)
    with pytest.raises(SystemExit) as raised:
        pool_run.main(["build"])
    assert raised.value.code == 128 + signal.SIGTERM
    assert _last(capsys).endswith(f"pool_run build finished rc=exit {128 + signal.SIGTERM}")
    assert signal.getsignal(signal.SIGTERM) is before
