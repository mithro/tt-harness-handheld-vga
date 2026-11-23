# SPDX-License-Identifier: Apache-2.0
"""Read the shared project manifest published by the tinytapeout-vga-videos checkout.

Inputs are files only; nothing from that project is imported:

- `data/targets.json` (key `targets`): repo, commit, top module, source files, clock, pmods.
- `data/results/<shuttle>/<macro>/result.json`, key `override`: the compiled per-project
  override (`sources`, `top_module`, `clock_hz`, `ui_in`, `uio_in`; `inputs`, `gamepad` and
  `patch` there are file names in a git-ignored directory, so they are not usable).
- `overrides/<shuttle>/<macro>.yaml`: the tracked source of that override, read as
  `ttvga/overrides.py` does. It supplies the input events and the patch file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_VIDEOS_REPO = "~/github/TinyTapeout/tinytapeout-vga-videos"
# `ttvga/overrides.py` DEFAULT_CLOCK_HZ: used when neither the manifest nor an override sets one.
DEFAULT_CLOCK_HZ = 25_175_000

_OVERRIDE_KEYS = ("sources", "top_module", "clock_hz", "ui_in", "uio_in")


@dataclass(frozen=True)
class Project:
    shuttle: str
    macro: str
    repo: str
    commit: str
    top_module: str
    source_files: tuple[str, ...]
    clock_hz: int
    pmods: tuple[str, ...]
    address: int | None
    ui_in: int
    uio_in: int
    patch: str | None

    @property
    def id(self) -> str:
        return f"{self.shuttle}/{self.macro}"


def default_videos_repo() -> Path:
    """The videos checkout: `$TT_VIDEOS_REPO`, else the conventional sibling path."""
    return Path(os.environ.get("TT_VIDEOS_REPO", DEFAULT_VIDEOS_REPO)).expanduser()


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text())
    return data or {}


def _read_result_override(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("override") or {}


def _project(target: dict, videos_repo: Path) -> Project:
    shuttle, macro = target["shuttle"], target["macro"]
    result_ov = _read_result_override(
        videos_repo / "data/results" / shuttle / macro / "result.json"
    )
    yaml_path = videos_repo / "overrides" / shuttle / f"{macro}.yaml"
    yaml_ov = _read_yaml(yaml_path)

    ov = {k: result_ov[k] for k in _OVERRIDE_KEYS if k in result_ov}
    ov.update({k: yaml_ov[k] for k in _OVERRIDE_KEYS if k in yaml_ov})

    patch = None
    if yaml_ov.get("patch"):
        patch = str(yaml_path.parent / yaml_ov["patch"])
    elif result_ov.get("patch"):
        raise ValueError(
            f"{shuttle}/{macro}: result.json names patch {result_ov['patch']!r} "
            f"but {yaml_path} does not"
        )

    events = yaml_ov.get("inputs")
    first = events[0] if isinstance(events, list) and events else {}
    ui_in = int(ov["ui_in"]) if "ui_in" in ov else int(first.get("ui_in", 0))
    uio_in = int(ov["uio_in"]) if "uio_in" in ov else int(first.get("uio_in", 0))

    clock_hz = int(ov.get("clock_hz") or target.get("clock_hz") or 0) or DEFAULT_CLOCK_HZ
    address = target.get("address")
    return Project(
        shuttle=shuttle,
        macro=macro,
        repo=target["repo"],
        commit=target["commit"],
        top_module=ov.get("top_module") or target["top_module"],
        source_files=tuple(ov.get("sources") or target["source_files"]),
        clock_hz=clock_hz,
        pmods=tuple(target.get("pmods") or ()),
        address=int(address) if address not in (None, "") else None,
        ui_in=ui_in,
        uio_in=uio_in,
        patch=patch,
    )


def load(videos_repo: Path) -> dict[str, Project]:
    """Load every project from a videos checkout, keyed by `Project.id`."""
    targets_json = Path(videos_repo) / "data" / "targets.json"
    if not targets_json.is_file():
        raise FileNotFoundError(
            f"no manifest at {targets_json}; set TT_VIDEOS_REPO to a checkout of "
            f"tinytapeout-vga-videos (default {DEFAULT_VIDEOS_REPO})"
        )
    targets = json.loads(targets_json.read_text())["targets"]
    projects = (_project(t, Path(videos_repo)) for t in targets)
    return {p.id: p for p in projects}


def get(projects: dict[str, Project], project_id: str) -> Project:
    """Look up a project by id, raising a `KeyError` that names it."""
    try:
        return projects[project_id]
    except KeyError:
        raise KeyError(f"unknown project {project_id!r}") from None


def by_address(projects: dict[str, Project], shuttle: str, address: int) -> Project:
    """Find the project at an integer address within a shuttle."""
    for p in projects.values():
        if p.shuttle == shuttle and p.address == address:
            return p
    raise KeyError(f"no project at address {address} in shuttle {shuttle!r}")
