# SPDX-License-Identifier: Apache-2.0
"""The runner model against traces written by hand from docs/demo-runner.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tt_harness.demo import model, validate
from tt_harness.demo.model import Event

TRACES = sorted((Path(__file__).parent / "data" / "demo" / "traces").glob("*.json"))
SHUTTLE = "ttsky25a"


def load_entry(raw: dict) -> validate.Entry:
    text = json.dumps({"version": 1, "shuttle": SHUTTLE, "projects": [raw]})
    return validate.parse(text, SHUTTLE).projects[0]


def expected_events(trace: dict) -> list[Event]:
    """The trace's events, with [t, 'report', name, base] expanded to one write per level."""
    out: list[Event] = []
    for item in trace["events"]:
        if item[1] == "report":
            t, _, name, base = item
            out.extend(Event(t, "gamepad", base | level) for level in trace["reports"][name])
        else:
            t, kind, value = item
            out.append(Event(t, kind, value))
    return out


def test_there_are_traces():
    assert len(TRACES) >= 4


@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_model_reproduces_every_trace(path: Path):
    trace = json.loads(path.read_text())
    assert trace["comment"]
    entry = load_entry(trace["entry"])
    got = model.run(entry, trace["total_projects"], trace["until_ms"])
    assert got == expected_events(trace)


def test_events_are_ordered_by_time_and_use_known_kinds():
    entry = load_entry(
        {
            "address": 1,
            "subtile": 0,
            "duration_ms": 700,
            "clock_hz": 5,
            "ui_in": 0,
            "steps": [{"set": 1}, {"wait_ms": 7}, {"gamepad": [{"buttons": ["a"]}]}, {"wait_ms": 9}],
        }
    )
    events = model.run(entry, 3, 700)
    assert [e.t_ms for e in events] == sorted(e.t_ms for e in events)
    assert {e.kind for e in events} <= set(model.KINDS)
    assert events[-1] == Event(700, "next", 0)
    assert all(e.t_ms <= 700 for e in events)


def test_until_before_the_end_of_selection_keeps_only_time_zero_writes():
    entry = load_entry(
        {"address": 1, "subtile": 0, "duration_ms": 50, "clock_hz": 0, "ui_in": 9, "steps": []}
    )
    assert model.run(entry, 2, 49) == [
        Event(0, "ena", 1),
        Event(0, "clock_hz", 0),
        Event(0, "ui_in", 9),
    ]
    assert model.run(entry, 2, 50)[-1] == Event(50, "next", 0)


def test_gamepad_levels_start_and_end_idle_and_send_24_clocks_and_one_latch():
    for controllers in [(), ((),), (("b",),), (("start", "r"), ("left", "x")), ((), ())]:
        levels = model.gamepad_levels(validate.gamepad_bits(controllers))
        assert levels[-1] == 0
        clock_edges = sum(1 for i, v in enumerate(levels) if v & 0x20 and not (levels[i - 1] & 0x20 if i else 0))
        latch_edges = sum(1 for i, v in enumerate(levels) if v & 0x10 and not (levels[i - 1] & 0x10 if i else 0))
        assert (clock_edges, latch_edges) == (24, 1)
        assert all(v & ~0x70 == 0 for v in levels)
