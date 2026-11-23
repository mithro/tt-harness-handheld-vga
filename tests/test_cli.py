# SPDX-License-Identifier: Apache-2.0
"""CLI skeleton and tool-lookup helper tests."""

from __future__ import annotations

import subprocess
import sys

import tt_harness
from tt_harness import tools


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tt_harness.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_prints_package_version():
    result = run_cli("--version")
    assert result.returncode == 0
    assert tt_harness.__version__ in result.stdout


def test_no_arguments_exits_2_with_usage():
    result = run_cli()
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_find_unknown_tool_is_none():
    assert tools.find("definitely-not-a-tool") is None


def test_find_locates_tool_on_path():
    found = tools.find("python3")
    assert found is not None
    assert found.name.startswith("python")
