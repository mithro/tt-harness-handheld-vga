# SPDX-License-Identifier: Apache-2.0
"""`tests/source_cache.py` skips a test only for a failed `git fetch` (no network)."""

from __future__ import annotations

import subprocess

from source_cache import _is_network

from tt_harness import fetch


def _error(cause: BaseException) -> fetch.FetchError:
    try:
        raise fetch.FetchError("x") from cause
    except fetch.FetchError as e:
        return e


def test_only_a_failed_or_timed_out_fetch_is_no_network() -> None:
    fetch_failed = subprocess.CalledProcessError(128, ["git", "fetch", "-q"], stderr="no route")
    checkout_failed = subprocess.CalledProcessError(1, ["git", "checkout", "-q"], stderr="x")
    timed_out = subprocess.TimeoutExpired(["git", "fetch"], 60)
    assert _is_network(_error(fetch_failed))
    assert _is_network(_error(timed_out))
    assert not _is_network(_error(checkout_failed))
    assert not _is_network(fetch.FetchError("source file missing from checkout"))
