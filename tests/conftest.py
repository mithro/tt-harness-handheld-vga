# SPDX-License-Identifier: Apache-2.0
"""Tests marked `board` touch a real FPGA board over SSH and run only with `--board`.

Tests marked `slow` (exhaustive simulation sweeps) are skipped, with the reason
"slow: run with --run-slow", unless `--run-slow` is given or the `-m` expression names `slow`.

A test session must not write into the default build tree `build/compare/`, which holds
the bitstreams the CLI and the pool script built and ran: `_default_build_tree_untouched`
fails the session if a file there was created, removed or modified while it ran. (So do
not run the tests while a build into that tree is in progress.)
"""

from __future__ import annotations

from pathlib import Path

import build_tree
import pytest

SLOW_REASON = "slow: run with --run-slow"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--board", action="store_true", default=False, help="run tests that use a real board"
    )
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="also run tests marked slow (exhaustive simulation sweeps)",
    )


def slow_selected(config: pytest.Config) -> bool:
    """`slow` tests run when asked for: `--run-slow`, or a `-m` expression naming `slow`."""
    return bool(config.getoption("--run-slow")) or "slow" in (config.getoption("-m") or "")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not slow_selected(config):
        skip_slow = pytest.mark.skip(reason=SLOW_REASON)
        for item in items:
            if item.get_closest_marker("slow"):
                item.add_marker(skip_slow)
    if config.getoption("--board"):
        return
    chosen, dropped = [], []
    for item in items:
        (dropped if item.get_closest_marker("board") else chosen).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = chosen


@pytest.fixture(scope="session", autouse=True)
def _default_build_tree_untouched():
    before = build_tree.state(build_tree.DEFAULT_COMPARE_TREE)
    yield
    changed = build_tree.changes(before, build_tree.state(build_tree.DEFAULT_COMPARE_TREE))
    assert not changed, (
        f"the test session wrote into {build_tree.DEFAULT_COMPARE_TREE}, the tree of the CLI "
        f"and the pool script ({len(changed)} files): " + "; ".join(changed[:10])
    )


@pytest.fixture(scope="session")
def build_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where real-build tests build: under pytest's temporary directory."""
    return tmp_path_factory.mktemp("compare-builds")
