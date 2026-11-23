# SPDX-License-Identifier: Apache-2.0
"""Locate external tools: the repo-local toolchains first, then PATH."""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLCHAINS = REPO / ".toolchains"
VIVADO_SETTINGS = Path("/opt/xilinx/Vivado/2025.2/settings64.sh")


def _toolchain_bin_dirs() -> list[Path]:
    return sorted(p for p in TOOLCHAINS.glob("*/bin") if p.is_dir())


def find(name: str) -> Path | None:
    """Return the path of executable `name`, or None.

    `.toolchains/<component>/bin` is searched before PATH.
    """
    for directory in _toolchain_bin_dirs():
        candidate = directory / name
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def vivado_settings() -> Path | None:
    """Return Vivado's settings64.sh if installed, else None."""
    return VIVADO_SETTINGS if VIVADO_SETTINGS.is_file() else None
