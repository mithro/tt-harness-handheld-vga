# SPDX-License-Identifier: Apache-2.0
"""Local Verilator reference dump (`tt_harness.reference`)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tt_harness import cli, fetch, manifest, reference, tools
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed"),
]

DATA = Path(__file__).resolve().parent / "data" / "projects"
COUNTER_UI_IN = 0xA5

AGREEMENT_ID = "tt08/tt_um_tinytapeout_logo_screensaver"
AGREEMENT_CLOCKS = 1_000_000


def make_sources(root: Path, top: str, files: tuple[str, ...], ui_in: int = 0) -> Sources:
    project = Project(
        shuttle="tt00",
        macro=top,
        repo="/nonexistent",
        commit="0" * 40,
        top_module=top,
        source_files=files,
        clock_hz=25_175_000,
        clock_declared=25_175_000,
        pmods=(),
        address=None,
        ui_in=ui_in,
        uio_in=0,
        patch=None,
    )
    paths = tuple(root / "src" / f for f in files)
    return Sources(
        project=project,
        root=root,
        files=paths,
        needs_sv=any(p.suffix == ".sv" for p in paths),
    )


@pytest.fixture
def counter() -> Sources:
    return make_sources(
        DATA / "counter", "tt_um_test_counter", ("tt_um_test_counter.v",), COUNTER_UI_IN
    )


def test_counter_dump_has_reset_value_then_counts(counter, tmp_path):
    clocks = 600
    dump = reference.simulate(counter, clocks, tmp_path / "ref")
    assert dump == tmp_path / "ref" / "uo_out.bin"
    data = dump.read_bytes()
    assert len(data) == clocks
    # Byte i is uo_out after the rising edge of clock i; rst_n is low for clocks 0 to 9.
    assert data[:10] == bytes([COUNTER_UI_IN]) * 10
    expected = bytes((COUNTER_UI_IN + i - 9) & 0xFF for i in range(10, clocks))
    assert data[10:] == expected


def test_reset_clocks_is_honoured(counter, tmp_path):
    data = reference.simulate(counter, 40, tmp_path / "ref", reset_clocks=25).read_bytes()
    assert data[:25] == bytes([COUNTER_UI_IN]) * 25
    assert data[25] == (COUNTER_UI_IN + 1) & 0xFF


def test_dump_samples_after_rising_edge_before_falling_edge(tmp_path):
    # uo_out = {fall[3:0], rise[3:0]}: `rise` counts on rising edges once out of reset, and
    # `fall` copies `rise` on each falling edge. Sampled after rising edge i and before the
    # falling edge that follows it, `fall` still holds the count from rising edge i - 1.
    sources = make_sources(DATA / "edges", "tt_um_test_edges", ("tt_um_test_edges.v",))
    data = reference.simulate(sources, 16, tmp_path / "ref").read_bytes()
    # Byte 0 is skipped: `fall` has seen no falling edge yet, so it holds its initial value.
    # Clocks 1 to 9 are in reset; rising edges 10 to 15 count rise to 1..6.
    assert data[1:] == bytes(9) + bytes([0x01, 0x12, 0x23, 0x34, 0x45, 0x56])
    # Sampling after the falling edge instead would give 0x11, 0x22, ... from clock 10.


def test_two_runs_are_byte_identical(counter, tmp_path):
    a = reference.simulate(counter, 5000, tmp_path / "a").read_bytes()
    b = reference.simulate(counter, 5000, tmp_path / "b").read_bytes()
    assert a == b


def test_syntax_error_raises_with_log_naming_the_file(tmp_path):
    root = tmp_path / "bad"
    (root / "src").mkdir(parents=True)
    (root / "src" / "tt_um_bad.v").write_text(
        "module tt_um_bad(input wire clk, output wire [7:0] uo_out)\n  assign uo_out = ;\n"
    )
    sources = make_sources(root, "tt_um_bad", ("tt_um_bad.v",))
    with pytest.raises(reference.ReferenceError) as info:
        reference.simulate(sources, 100, tmp_path / "ref")
    log = info.value.log
    assert log.is_file()
    # The log echoes the command line, which names the file anyway; check the error itself.
    errors = [line for line in log.read_text().splitlines() if line.startswith("%Error")]
    assert any("tt_um_bad.v:2" in line for line in errors)
    assert "tt_um_bad.v:2" in str(info.value)
    assert str(log) in str(info.value)
    assert not (tmp_path / "ref" / "uo_out.bin").exists()


def test_run_timeout_kills_the_process_group(tmp_path):
    log = tmp_path / "run.log"
    pid_file = tmp_path / "child.pid"
    # The shell starts a background child, as Verilator starts make; both must die.
    script = f"sleep 30 & echo $! > {pid_file}; wait"
    assert reference._run(["sh", "-c", script], log, tmp_path, timeout=1) == -1
    assert "timed out after 1s" in log.read_text()
    child = int(pid_file.read_text())
    # The orphaned child is reaped by init shortly after it is killed.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"background child {child} survived the timeout")


def _sibling_dirs() -> tuple[Path, Path]:
    ref_dir = Path(
        os.environ.get(
            "TT_REFERENCE_DIR", "~/github/TinyTapeout/tt-harness-fpga-vga/work/reference"
        )
    ).expanduser()
    return ref_dir, manifest.default_videos_repo()


@pytest.mark.skipif(
    not all(d.is_dir() for d in _sibling_dirs()),
    reason="TT_REFERENCE_DIR or TT_VIDEOS_REPO checkout absent",
)
@pytest.mark.xdist_group("work-sources")  # fetches into the shared work/sources cache
def test_agrees_with_sibling_reference(tmp_path):
    ref_dir, videos_repo = _sibling_dirs()
    sibling = ref_dir / AGREEMENT_ID / "ideal" / "uo_out.bin.gz"
    if not sibling.is_file():
        pytest.skip(f"no sibling reference at {sibling}")
    project = manifest.get(manifest.load(videos_repo), AGREEMENT_ID)
    meta = json.loads((sibling.parent / "reference.json").read_text())
    assert (meta["reset_clocks"], meta["ui_in"], meta["uio_in"]) == (
        10,
        project.ui_in,
        project.uio_in,
    )
    try:
        sources = fetch.fetch(project, tools.REPO / "work" / "sources")
    except fetch.FetchError as e:
        # A cold source cache with no network access, for example.
        pytest.skip(f"cannot fetch {AGREEMENT_ID} sources (empty cache and no network?): {e}")
    ours = reference.simulate(sources, AGREEMENT_CLOCKS, tmp_path / "ref").read_bytes()
    with gzip.open(sibling, "rb") as f:
        theirs = f.read(AGREEMENT_CLOCKS)
    assert len(theirs) == AGREEMENT_CLOCKS
    assert len(ours) == AGREEMENT_CLOCKS
    if ours != theirs:
        first = next(i for i, (a, b) in enumerate(zip(ours, theirs, strict=True)) if a != b)
        pytest.fail(
            f"first difference at clock {first}: ours {ours[first]:#04x}, "
            f"sibling {theirs[first]:#04x}"
        )


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)
    return r.stdout.strip()


@pytest.fixture
def counter_videos_repo(tmp_path) -> Path:
    """A videos checkout whose manifest lists the counter, held in a local Git repository."""
    repo = tmp_path / "counter-repo"
    shutil.copytree(DATA / "counter", repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "counter")
    target = {
        "id": "tt00/tt_um_test_counter",
        "shuttle": "tt00",
        "macro": "tt_um_test_counter",
        "address": "1",
        "repo": str(repo),
        "commit": _git(repo, "rev-parse", "HEAD"),
        "top_module": "tt_um_test_counter",
        "source_files": ["tt_um_test_counter.v"],
        "clock_hz": 25175000,
        "pmods": [],
    }
    videos = tmp_path / "videos"
    (videos / "data").mkdir(parents=True)
    (videos / "data/targets.json").write_text(json.dumps({"targets": [target]}))
    return videos


def _cli_reference(project_id: str, videos: Path, tmp_path: Path) -> int:
    return cli.main(
        [
            "reference",
            project_id,
            "--clocks",
            "30",
            "--videos-repo",
            str(videos),
            "--cache",
            str(tmp_path / "cache"),
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )


def test_cli_reference_writes_dump(counter_videos_repo, tmp_path, capsys):
    assert _cli_reference("tt00/tt_um_test_counter", counter_videos_repo, tmp_path) == 0
    dump = tmp_path / "out" / "uo_out.bin"
    assert capsys.readouterr().out.strip() == str(dump)
    assert dump.read_bytes() == bytes(10) + bytes(range(1, 21))


def test_cli_reference_unknown_project_fails(counter_videos_repo, tmp_path, capsys):
    assert _cli_reference("tt99/nope", counter_videos_repo, tmp_path) == 1
    assert "tt99/nope" in capsys.readouterr().err


def test_cli_reference_build_failure_names_project_and_log(counter_videos_repo, tmp_path, capsys):
    repo = tmp_path / "counter-repo"
    (repo / "src" / "tt_um_test_counter.v").write_text("module tt_um_test_counter(\n")
    _git(repo, "commit", "-q", "-am", "break")
    targets = counter_videos_repo / "data/targets.json"
    data = json.loads(targets.read_text())
    data["targets"][0]["commit"] = _git(repo, "rev-parse", "HEAD")
    targets.write_text(json.dumps(data))
    assert _cli_reference("tt00/tt_um_test_counter", counter_videos_repo, tmp_path) == 1
    err = capsys.readouterr().err
    assert "tt00/tt_um_test_counter" in err
    assert str(tmp_path / "out") in err
    assert not (tmp_path / "out" / "uo_out.bin").exists()
