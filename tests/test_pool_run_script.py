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


def test_build_document_keeps_the_fetch_warnings(pool_run, monkeypatch, tmp_path) -> None:
    manifest_dir = Path(__file__).resolve().parent / "data" / "manifest"
    monkeypatch.setenv("TT_VIDEOS_REPO", str(manifest_dir))
    seen = []

    def fake_fetch(project, cache, videos_repo=None):
        seen.append(videos_repo)
        return pool_run.fetch.Sources(project, tmp_path, (), False, warnings=("odd submodule",))

    def stop(*args, **kwargs):
        raise OSError("stopped here")

    monkeypatch.setattr(pool_run.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(pool_run.prescreen, "check", stop)
    monkeypatch.setattr(pool_run, "_build_dir", lambda project, toolchain: tmp_path / "b")
    monkeypatch.setattr(pool_run, "USE", {"openxc7": lambda out_dir: {}})
    doc = pool_run.build_one("tt07/tt_um_tinytapeout_dvd_screensaver", "openxc7", 100, 1)
    assert seen == [manifest_dir]
    assert doc["warnings"] == ["odd submodule"] and doc["ok"] is False


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
