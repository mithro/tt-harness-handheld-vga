# SPDX-License-Identifier: Apache-2.0
"""The `slow` marker hook in `conftest.py`: skipped unless asked for.

Tested on small fake configuration objects: the `pytester` plugin is not enabled here.
"""

from __future__ import annotations

import conftest
import pytest


class _Config:
    def __init__(self, run_slow: bool = False, m: str = "") -> None:
        self.opts = {"--run-slow": run_slow, "-m": m, "--board": False}

    def getoption(self, name: str):
        return self.opts[name]


class _Item:
    def __init__(self, slow: bool) -> None:
        self.slow = slow
        self.markers: list = []

    def get_closest_marker(self, name: str):
        return object() if (name == "slow" and self.slow) else None

    def add_marker(self, marker) -> None:
        self.markers.append(marker)


@pytest.mark.parametrize(
    ("run_slow", "expr", "selected"),
    [
        (False, "", False),
        (False, "not toolchain and not vivado", False),
        (True, "", True),
        (False, "slow", True),
        (False, "toolchain or slow", True),
    ],
)
def test_slow_tests_run_only_when_asked_for(run_slow: bool, expr: str, selected: bool) -> None:
    config = _Config(run_slow, expr)
    assert conftest.slow_selected(config) is selected
    slow, fast = _Item(True), _Item(False)
    items = [slow, fast]
    conftest.pytest_collection_modifyitems(config, items)
    assert items == [slow, fast]
    assert fast.markers == []
    assert bool(slow.markers) is (not selected)
    if slow.markers:
        assert slow.markers[0].kwargs["reason"] == "slow: run with --run-slow"
