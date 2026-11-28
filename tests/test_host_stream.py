# SPDX-License-Identifier: Apache-2.0
"""A remote command left running, tried with local commands in place of ssh."""

from __future__ import annotations

import sys

import pytest

from tt_harness.host import run

CHATTY = r"""
import sys
sys.stdout.buffer.write(b'fake-login-banner\n\x00\x00TTHR listening {"baud": 9600}\n')
sys.stdout.flush()
print('TTHR received {"bytes": 3}')
sys.exit(3)
"""


def _stream(script: str) -> run._SshStream:
    return run._SshStream([sys.executable, "-c", script])


def test_stream_finds_its_lines_after_a_banner_and_nul_bytes():
    stream = _stream(CHATTY)
    try:
        assert stream.expect("listening", 20) == {"baud": 9600}
        assert stream.expect("received", 20) == {"bytes": 3}
        assert stream.wait(20) == 3
    finally:
        stream.kill()


def test_stream_that_ends_without_the_line_is_an_error_with_what_it_said():
    stream = _stream("print('Traceback: no module named serial')")
    try:
        with pytest.raises(run.RunError, match="no module named serial"):
            stream.expect("listening", 20)
    finally:
        stream.kill()


def test_stream_with_an_unreadable_line_is_an_error():
    stream = _stream("print('TTHR listening {oops')")
    try:
        with pytest.raises(run.RunError, match="unreadable"):
            stream.expect("listening", 20)
    finally:
        stream.kill()


def test_stream_that_stays_silent_times_out_and_can_be_killed():
    stream = _stream("import time; time.sleep(600)")
    try:
        with pytest.raises(run.RunError, match="within"):
            stream.expect("listening", 0.3)
        with pytest.raises(run.RunError, match="did not end"):
            stream.wait(0.1)
    finally:
        stream.kill()
    assert stream.wait(1) != 0
