# SPDX-License-Identifier: Apache-2.0
"""Tests marked `board` touch a real FPGA board over SSH and run only with `--board`.

A test session must not write into the default build tree `build/compare/`, which holds
the bitstreams the CLI and the pool script built and ran: `_default_build_tree_untouched`
fails the session if a file there was created, removed or modified while it ran. (So do
not run the tests while a build into that tree is in progress.)
"""

from __future__ import annotations

from pathlib import Path

import build_tree
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--board", action="store_true", default=False, help="run tests that use a real board"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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
