# SPDX-License-Identifier: Apache-2.0
"""Real project sources for tests: skip only when they cannot be had, fail on anything else.

`load_or_skip(project_id)` is `tt_harness.pool.load_sources`, skipping the test when there is
no tinytapeout-vga-videos checkout (`$TT_VIDEOS_REPO`) or when `git fetch` fails or times out
(no network with a cold `work/sources` cache). Any other error (a project missing from the
manifest, a bad checkout, a missing source file) fails the test.
"""

from __future__ import annotations

import subprocess

import pytest

from tt_harness import fetch, manifest, pool
from tt_harness.fetch import Sources


def _is_network(e: fetch.FetchError) -> bool:
    cause = e.__cause__
    if isinstance(cause, subprocess.TimeoutExpired):
        return True
    return isinstance(cause, subprocess.CalledProcessError) and cause.cmd[1:2] == ["fetch"]


def load_or_skip(project_id: str) -> Sources:
    videos = manifest.default_videos_repo()
    if not (videos / "data" / "targets.json").is_file():
        pytest.skip(f"no tinytapeout-vga-videos checkout at {videos} (set TT_VIDEOS_REPO)")
    try:
        return pool.load_sources(project_id)
    except fetch.FetchError as e:
        if _is_network(e):
            pytest.skip(f"cannot fetch {project_id} (no network?): {e}")
        raise
