# SPDX-License-Identifier: Apache-2.0
import dataclasses
import json
import os
import subprocess

import pytest

from tt_harness import cli, fetch
from tt_harness.manifest import Project

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd, *args):
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return r.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A local repository with two commits; returns (path, first_commit, second_commit)."""
    repo = tmp_path / "remote"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "src/top.v").write_text("module top;\nendmodule\n")
    (repo / "src/old.v").write_text("// old\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    first = git(repo, "rev-parse", "HEAD")
    (repo / "src/top.v").write_text("module top2;\nendmodule\n")
    (repo / "src/new.sv").write_text("// new\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two")
    return repo, first, git(repo, "rev-parse", "HEAD")


def make_project(repo, commit, files=("top.v",), patch=None):
    return Project(
        shuttle="tt00",
        macro="tt_um_x",
        repo=str(repo),
        commit=commit,
        top_module="top",
        source_files=tuple(files),
        clock_hz=25_175_000,
        pmods=(),
        address=None,
        ui_in=0,
        uio_in=0,
        patch=patch,
    )


def test_checks_out_exactly_the_pinned_commit(remote, tmp_path):
    repo, first, _second = remote
    src = fetch.fetch(make_project(repo, first, ("top.v", "old.v")), tmp_path / "cache")
    assert src.root == tmp_path / "cache/tt00/tt_um_x" / first
    assert src.files == (src.root / "src/top.v", src.root / "src/old.v")
    assert src.files[0].read_text() == "module top;\nendmodule\n"
    assert not (src.root / "src/new.sv").exists()
    assert src.needs_sv is False


def test_needs_sv(remote, tmp_path):
    repo, _first, second = remote
    src = fetch.fetch(make_project(repo, second, ("top.v", "new.sv")), tmp_path / "cache")
    assert src.needs_sv is True


def test_second_fetch_is_cache_hit_without_git(remote, tmp_path, monkeypatch):
    repo, first, _ = remote
    project = make_project(repo, first)
    cache = tmp_path / "cache"
    fetch.fetch(project, cache)

    def boom(*a, **k):
        raise AssertionError("git must not run on a cache hit")

    monkeypatch.setattr(fetch, "_git", boom)
    src = fetch.fetch(project, cache)
    assert src.files[0].read_text() == "module top;\nendmodule\n"


def test_patch_is_applied(remote, tmp_path):
    repo, first, _ = remote
    patch = tmp_path / "fix.diff"
    patch.write_text(
        "--- a/src/top.v\n+++ b/src/top.v\n@@ -1,2 +1,2 @@\n-module top;\n+module patched;\n"
        " endmodule\n"
    )
    src = fetch.fetch(make_project(repo, first, patch=str(patch)), tmp_path / "cache")
    assert src.files[0].read_text() == "module patched;\nendmodule\n"


def test_bad_patch_fails_and_leaves_no_cache(remote, tmp_path):
    repo, first, _ = remote
    patch = tmp_path / "bad.diff"
    patch.write_text("--- a/src/nope.v\n+++ b/src/nope.v\n@@ -1 +1 @@\n-x\n+y\n")
    project = make_project(repo, first, patch=str(patch))
    with pytest.raises(fetch.FetchError, match="patch"):
        fetch.fetch(project, tmp_path / "cache")
    assert not (tmp_path / "cache/tt00/tt_um_x" / first).exists()


def test_unknown_commit_names_project_and_commit(remote, tmp_path):
    repo, _, _ = remote
    bad = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(fetch.FetchError) as e:
        fetch.fetch(make_project(repo, bad), tmp_path / "cache")
    assert "tt00/tt_um_x" in str(e.value) and bad in str(e.value)


def test_missing_source_file_names_file(remote, tmp_path):
    repo, first, _ = remote
    with pytest.raises(fetch.FetchError, match="missing.v"):
        fetch.fetch(make_project(repo, first, ("top.v", "missing.v")), tmp_path / "cache")


@pytest.mark.parametrize("commit", ["main", "abc", "--upload-pack=x", "g" * 40, "A" * 40])
def test_commit_validated(remote, tmp_path, commit):
    repo, _, _ = remote
    with pytest.raises(fetch.FetchError, match="commit"):
        fetch.fetch(make_project(repo, commit), tmp_path / "cache")


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=touch x",
        "ext::sh -c id",
        "http://example.com/r",
        "git@github.com:a/b",
        "file:///etc",
        "relative/path",
        "",
    ],
)
def test_repo_url_validated(tmp_path, url):
    project = make_project(url, "a" * 40)
    with pytest.raises(fetch.FetchError, match="repo"):
        fetch.fetch(project, tmp_path / "cache")


def test_git_receives_url_and_commit_after_double_dash(remote, tmp_path, monkeypatch):
    repo, first, _ = remote
    calls = []
    real = fetch._git

    def spy(args, cwd):
        calls.append(list(args))
        return real(args, cwd)

    monkeypatch.setattr(fetch, "_git", spy)
    fetch.fetch(make_project(repo, first), tmp_path / "cache")
    carrying = [a for a in calls if str(repo) in a or first in a]
    assert len(carrying) >= 2  # remote add and fetch
    for args in carrying:
        i = args.index("--")
        assert str(repo) not in args[:i] and first not in args[:i]


def _videos_repo(tmp_path, repo, commit):
    videos = tmp_path / "videos"
    (videos / "data").mkdir(parents=True)
    target = {
        "id": "tt00/tt_um_x",
        "shuttle": "tt00",
        "macro": "tt_um_x",
        "address": "1",
        "repo": str(repo),
        "commit": commit,
        "top_module": "top",
        "source_files": ["top.v"],
        "clock_hz": 25175000,
        "pmods": [],
    }
    (videos / "data/targets.json").write_text(json.dumps({"targets": [target]}))
    return videos


def test_cli_fetch_prints_source_root(remote, tmp_path, capsys):
    repo, first, _ = remote
    videos = _videos_repo(tmp_path, repo, first)
    cache = tmp_path / "cache"
    rc = cli.main(["fetch", "tt00/tt_um_x", "--videos-repo", str(videos), "--cache", str(cache)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(cache / "tt00/tt_um_x" / first)


def test_cli_fetch_unknown_project_fails(remote, tmp_path, capsys):
    repo, first, _ = remote
    videos = _videos_repo(tmp_path, repo, first)
    rc = cli.main(["fetch", "tt99/nope", "--videos-repo", str(videos), "--cache", str(tmp_path)])
    assert rc == 1
    assert "tt99/nope" in capsys.readouterr().err


def test_project_is_frozen(remote):
    repo, first, _ = remote
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_project(repo, first).commit = "x"  # type: ignore[misc]
