# SPDX-License-Identifier: Apache-2.0
"""Fetch a design's Verilog sources at its pinned commit into a cache directory."""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tt_harness.manifest import Project, default_videos_repo

_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_HTTPS_RE = re.compile(r"https://[^\s/][^\s]*")
_MARKER = ".tt-harness-fetched"
_SRC_DIR = "src"
_POLYFILL = "sky130_polyfill.v"
_POLYFILL_IN_VIDEOS = Path("ttvga/harness/cells") / _POLYFILL
_SSH_GITHUB_RE = re.compile(r"^(git@github\.com:|ssh://git@github\.com/)")
_GIT_TIMEOUT_S = 600


class FetchError(RuntimeError):
    """Sources for a project could not be fetched or are incomplete."""


@dataclass(frozen=True)
class Sources:
    project: Project
    root: Path
    files: tuple[Path, ...]
    needs_sv: bool


def _git(args: list[str], cwd: Path) -> str:
    """Run git in `cwd`, returning stdout; raises `subprocess.CalledProcessError` on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:file")
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return result.stdout


def _validate(project: Project) -> None:
    if not _COMMIT_RE.fullmatch(project.commit):
        raise FetchError(
            f"{project.id}: commit {project.commit!r} is not a full lower-case hex commit id"
        )
    repo = project.repo
    if not (_HTTPS_RE.fullmatch(repo) or (repo.startswith("/") and "\n" not in repo)):
        raise FetchError(f"{project.id}: repo {repo!r} is not an https:// URL or absolute path")
    _validate_source_paths(project)


def _validate_source_paths(project: Project) -> None:
    for entry in project.source_files:
        norm = posixpath.normpath(entry)
        if posixpath.isabs(entry) or norm == ".." or norm.startswith("../"):
            raise FetchError(f"{project.id}: source file {entry!r} is outside the source directory")


def _patch_id(project: Project) -> str:
    if project.patch is None:
        return "none"
    return Path(project.patch).read_text()


def _sources(project: Project, root: Path) -> Sources:
    src_dir = (root / _SRC_DIR).resolve()
    files = tuple(root / _SRC_DIR / f for f in project.source_files)
    for entry, f in zip(project.source_files, files, strict=True):
        if not f.resolve().is_relative_to(src_dir):
            raise FetchError(f"{project.id}: source file {entry!r} is outside the source directory")
        if not f.is_file():
            raise FetchError(f"{project.id}: source file {f.name} missing from checkout at {f}")
    return Sources(project, root, files, any(f.suffix == ".sv" for f in files))


def _submodules(project: Project, dest: Path) -> None:
    """Initialise submodules as videos `ttvga/harness/job.py` does, after checking URLs.

    `.gitmodules` comes from the fetched (untrusted) repository, so every URL must be https
    (GitHub SSH forms are rewritten to https, as job.py does) or an absolute local path.
    """
    gitmodules = dest / ".gitmodules"
    if not gitmodules.is_file():
        return
    try:
        listing = _git(["config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.url"], dest)
        for line in listing.splitlines():
            key, _, url = line.partition(" ")
            url = url.strip()
            https = _SSH_GITHUB_RE.sub("https://github.com/", url)
            if not (_HTTPS_RE.fullmatch(https) or https.startswith("/")):
                raise FetchError(
                    f"{project.id}: submodule URL {url!r} is not https or a local path"
                )
            if https != url:
                _git(["config", "-f", ".gitmodules", key, https], dest)
        _git(["submodule", "sync", "-q", "--recursive"], dest)
        _git(["submodule", "update", "-q", "--init", "--depth", "1", "--recursive"], dest)
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"{project.id}: git {' '.join(e.cmd[1:3])} failed: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"{project.id}: git timed out initialising submodules") from e


def _polyfill(project: Project, dest: Path, videos_repo: Path) -> None:
    """Substitute the videos checkout's polyfill when a listed one is absent (job.py:98-100)."""
    for entry in project.source_files:
        if Path(entry).name != _POLYFILL:
            continue
        target = dest / _SRC_DIR / entry
        if target.exists():
            continue
        source = videos_repo / _POLYFILL_IN_VIDEOS
        if not source.is_file():
            raise FetchError(
                f"{project.id}: {_POLYFILL} is absent from the checkout and {source} does not "
                "exist; set TT_VIDEOS_REPO to a tinytapeout-vga-videos checkout"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


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
    _submodules(project, dest)
    if project.patch is not None:
        try:
            _git(["apply", "--whitespace=nowarn", "--", str(Path(project.patch).resolve())], dest)
        except subprocess.CalledProcessError as e:
            raise FetchError(
                f"{project.id}: patch {project.patch} did not apply: {e.stderr.strip()}"
            ) from e


def fetch(project: Project, cache: Path, videos_repo: Path | None = None) -> Sources:
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
        _polyfill(project, partial, Path(videos_repo) if videos_repo else default_videos_repo())
        _sources(project, partial)  # raises FetchError if a listed file is missing
        (partial / _MARKER).write_text(patch_id)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    partial.rename(final)
    return _sources(project, final)
