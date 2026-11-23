# SPDX-License-Identifier: Apache-2.0
"""Fetch a design's Verilog sources at its pinned commit into a cache directory."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tt_harness.manifest import Project

_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HTTPS_RE = re.compile(r"https://[^\s/][^\s]*")
_MARKER = ".tt-harness-fetched"
_SRC_DIR = "src"
_GIT_TIMEOUT_S = 600


class FetchError(RuntimeError):
    """Sources for a project could not be fetched or are incomplete."""


@dataclass(frozen=True)
class Sources:
    project: Project
    root: Path
    files: tuple[Path, ...]
    needs_sv: bool


def _git(args: list[str], cwd: Path) -> None:
    """Run git in `cwd`, raising `subprocess.CalledProcessError` (with output) on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:file")
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )


def _validate(project: Project) -> None:
    if not _COMMIT_RE.fullmatch(project.commit):
        raise FetchError(
            f"{project.id}: commit {project.commit!r} is not a full lower-case hex commit id"
        )
    repo = project.repo
    if not (_HTTPS_RE.fullmatch(repo) or (repo.startswith("/") and "\n" not in repo)):
        raise FetchError(f"{project.id}: repo {repo!r} is not an https:// URL or absolute path")


def _patch_id(project: Project) -> str:
    if project.patch is None:
        return "none"
    return Path(project.patch).read_text()


def _sources(project: Project, root: Path) -> Sources:
    files = tuple(root / _SRC_DIR / f for f in project.source_files)
    for f in files:
        if not f.is_file():
            raise FetchError(f"{project.id}: source file {f.name} missing from checkout at {f}")
    return Sources(project, root, files, any(f.suffix == ".sv" for f in files))


def _checkout(project: Project, dest: Path) -> None:
    dest.mkdir(parents=True)
    try:
        for args in (
            ["init", "-q"],
            ["remote", "add", "origin", "--", project.repo],
            ["fetch", "-q", "--depth", "1", "origin", "--", project.commit],
            ["checkout", "-q", "--detach", "FETCH_HEAD"],
        ):
            _git(args, dest)
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"{project.id}: git {e.cmd[1]} failed for commit {project.commit}: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"{project.id}: git timed out fetching commit {project.commit}") from e
    if project.patch is not None:
        try:
            _git(["apply", "--whitespace=nowarn", "--", str(Path(project.patch).resolve())], dest)
        except subprocess.CalledProcessError as e:
            raise FetchError(
                f"{project.id}: patch {project.patch} did not apply: {e.stderr.strip()}"
            ) from e


def fetch(project: Project, cache: Path) -> Sources:
    """Return the sources of `project`, fetching into `cache/<shuttle>/<macro>/<commit>/`.

    A cache hit (same commit and same patch text) runs no Git command. A failed fetch leaves
    nothing at the final path.
    """
    _validate(project)
    final = Path(cache) / project.shuttle / project.macro / project.commit
    marker = final / _MARKER
    patch_id = _patch_id(project)
    if marker.is_file() and marker.read_text() == patch_id:
        return _sources(project, final)

    partial = final.with_name(final.name + ".partial")
    for stale in (partial, final):
        if stale.exists():
            shutil.rmtree(stale)
    try:
        _checkout(project, partial)
        _sources(project, partial)  # raises FetchError if a listed file is missing
        (partial / _MARKER).write_text(patch_id)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    partial.rename(final)
    return _sources(project, final)
