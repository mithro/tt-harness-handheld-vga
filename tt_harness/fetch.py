# SPDX-License-Identifier: Apache-2.0
"""Fetch a design's Verilog sources at its pinned commit into a cache directory."""

from __future__ import annotations

import hashlib
import json
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
    warnings: tuple[str, ...] = ()


def _protocols_for(project: Project) -> tuple[str, ...]:
    """Transports git may use: `file` only when the top-level repo is itself a local path."""
    return ("https",) if project.repo.startswith("https://") else ("https", "file")


def _git(args: list[str], cwd: Path, protocols: tuple[str, ...]) -> str:
    """Run git in `cwd`, returning stdout; raises `subprocess.CalledProcessError` on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL=":".join(protocols))
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


def _sources(project: Project, root: Path, warnings: tuple[str, ...] = ()) -> Sources:
    src = root / _SRC_DIR
    src_dir = root.resolve() / _SRC_DIR
    if src.is_symlink() or not src.resolve().is_relative_to(root.resolve()):
        raise FetchError(
            f"{project.id}: source directory {src} is a symlink or escapes the checkout"
        )
    files = tuple(root / _SRC_DIR / f for f in project.source_files)
    for entry, f in zip(project.source_files, files, strict=True):
        if not f.resolve().is_relative_to(src_dir):
            raise FetchError(f"{project.id}: source file {entry!r} is outside the source directory")
        if not f.is_file():
            raise FetchError(f"{project.id}: source file {f.name} missing from checkout at {f}")
    return Sources(project, root, files, any(f.suffix == ".sv" for f in files), warnings)


def _submodules(project: Project, dest: Path, protocols: tuple[str, ...]) -> list[str]:
    """Initialise submodules as videos `ttvga/harness/job.py` does; returns warnings.

    A failure is not fatal (job.py ignores it, and an unused submodule may be unreachable); a
    needed file that is missing is caught later. `.gitmodules` comes from the fetched,
    untrusted repository, so transports are limited only by GIT_ALLOW_PROTOCOL in `_git`
    (`protocols`, decided once per fetch from the top-level repo). Non-https transports, and
    `file` unless the top-level repo is a local path, are deliberately refused by git itself
    and surface here as warnings, never as a clone.
    """
    if not (dest / ".gitmodules").is_file():
        return []
    try:
        listing = _git(
            ["config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.url"], dest, protocols
        )
        for line in listing.splitlines():
            key, _, url = line.partition(" ")
            url = url.strip()
            https = _SSH_GITHUB_RE.sub("https://github.com/", url)
            if https != url:
                _git(["config", "-f", ".gitmodules", key, https], dest, protocols)
        _git(["submodule", "sync", "-q", "--recursive"], dest, protocols)
        _git(
            ["submodule", "update", "-q", "--init", "--depth", "1", "--recursive"], dest, protocols
        )
    except subprocess.CalledProcessError as e:
        lines = e.stderr.strip().splitlines()
        return [f"{project.id}: submodule init failed: {lines[-1] if lines else e}"]
    except subprocess.TimeoutExpired:
        return [f"{project.id}: submodule init failed: git timed out"]
    return []


def _polyfill_source(videos_repo: Path) -> Path:
    return videos_repo / _POLYFILL_IN_VIDEOS


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _polyfill(project: Project, dest: Path, videos_repo: Path) -> str | None:
    """Substitute the videos checkout's polyfill when a listed one is absent (job.py:98-100).

    Returns the SHA-256 of the substituted file, or None if nothing was substituted.
    """
    digest = None
    for entry in project.source_files:
        if Path(entry).name != _POLYFILL:
            continue
        target = dest / _SRC_DIR / entry
        if target.exists() or target.is_symlink():
            continue
        source = _polyfill_source(videos_repo)
        if not source.is_file():
            raise FetchError(
                f"{project.id}: {_POLYFILL} is absent from the checkout and {source} does not "
                "exist; set TT_VIDEOS_REPO to a tinytapeout-vga-videos checkout"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digest = _sha256(source)
    return digest


def _checkout(
    project: Project,
    dest: Path,
    protocols: tuple[str, ...],
    submodule_protocols: tuple[str, ...] | None = None,
) -> list[str]:
    """Fetch and check out `project` into `dest`; the submodule phase may use fewer transports."""
    dest.mkdir(parents=True)
    try:
        for args in (
            ["init", "-q"],
            ["remote", "add", "origin", "--", project.repo],
            ["fetch", "-q", "--depth", "1", "origin", "--", project.commit],
            ["checkout", "-q", "--detach", "FETCH_HEAD"],
        ):
            _git(args, dest, protocols)
    except subprocess.CalledProcessError as e:
        raise FetchError(
            f"{project.id}: git {e.cmd[1]} failed for commit {project.commit}: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"{project.id}: git timed out fetching commit {project.commit}") from e
    warnings = _submodules(project, dest, submodule_protocols or protocols)
    if project.patch is not None:
        try:
            _git(
                ["apply", "--whitespace=nowarn", "--", str(Path(project.patch).resolve())],
                dest,
                protocols,
            )
        except subprocess.CalledProcessError as e:
            raise FetchError(
                f"{project.id}: patch {project.patch} did not apply: {e.stderr.strip()}"
            ) from e
    return warnings


def _read_marker(marker: Path) -> dict | None:
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fetch(project: Project, cache: Path, videos_repo: Path | None = None) -> Sources:
    """Return the sources of `project`, fetching into `cache/<shuttle>/<macro>/<commit>/`.

    A cache hit (same commit, patch text and substituted-polyfill hash) runs no Git
    command and returns the warnings recorded when it was made. A failed fetch leaves
    nothing at the final path.
    """
    _validate(project)
    final = Path(cache) / project.shuttle / project.macro / project.commit
    marker = final / _MARKER
    patch_id = _patch_id(project)
    videos = Path(videos_repo) if videos_repo else default_videos_repo()

    recorded = _read_marker(marker)
    if recorded is not None and recorded.get("patch") == patch_id:
        digest = recorded.get("polyfill")
        poly = _polyfill_source(videos)
        if digest is None or (poly.is_file() and _sha256(poly) == digest):
            return _sources(project, final, tuple(recorded.get("warnings", ())))

    partial = final.with_name(final.name + ".partial")
    for stale in (partial, final):
        if stale.exists():
            shutil.rmtree(stale)
    try:
        protocols = _protocols_for(project)
        warnings = _checkout(project, partial, protocols)
        digest = _polyfill(project, partial, videos)
        _sources(project, partial)  # raises FetchError if a listed file is missing
        record = {"patch": patch_id, "polyfill": digest, "warnings": warnings}
        (partial / _MARKER).write_text(json.dumps(record))
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    partial.rename(final)
    return _sources(project, final, tuple(warnings))
