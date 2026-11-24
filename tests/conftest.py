# SPDX-License-Identifier: Apache-2.0
"""Tests marked `board` touch a real FPGA board over SSH and run only with `--board`."""

from __future__ import annotations

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
