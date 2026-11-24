# SPDX-License-Identifier: Apache-2.0
"""What is in a directory tree, to tell whether a test run wrote into it.

The default build tree `build/compare/` belongs to the CLI and the pool script: a bitstream
there is the one a board was loaded with. Tests build under pytest's temporary directory;
`conftest.py` uses this module to fail a test session that changed the default tree.
"""

from __future__ import annotations

from pathlib import Path

from tt_harness.tools import REPO

DEFAULT_COMPARE_TREE = REPO / "build" / "compare"


def state(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under `root` (relative path) with its modification time and size."""
    if not root.is_dir():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def changes(before: dict, after: dict) -> list[str]:
    """One line for every file that appeared, went or changed between two states."""
    lines = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before:
            lines.append(f"created {name}")
        elif name not in after:
            lines.append(f"removed {name}")
        elif before[name] != after[name]:
            lines.append(f"modified {name}")
    return lines
