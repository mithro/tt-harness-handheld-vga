# SPDX-License-Identifier: Apache-2.0
"""The demo.json validator: the board's messages, the shared vectors, and the gamepad word."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tt_harness.demo import validate
from tt_harness.demo.validate import (
    DemoError,
    DemoFile,
    Entry,
    Gamepad,
    Set,
    WaitMs,
)

DATA = Path(__file__).parent / "data" / "demo"
SHUTTLE = "ttsky25a"
VALID = sorted((DATA / "valid").glob("*.json"))
INVALID = sorted(
    p for p in (DATA / "invalid").glob("*.json") if not p.name.endswith(".errors.json")
)

ONE_ENTRY = {
    "address": 907,
    "subtile": 0,
    "duration_ms": 15000,
    "clock_hz": 25000000,
    "ui_in": 0,
    "steps": [],
}


def conf_with(**fields) -> dict:
    return {
        "version": 1,
        "shuttle": SHUTTLE,
        "projects": [{**ONE_ENTRY, **fields}],
    }


def test_the_vector_directories_are_not_empty():
    assert len(VALID) >= 9
    assert len(INVALID) >= 10


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_every_valid_vector_parses(path: Path):
    text = path.read_text()
    shuttle = json.loads(text)["shuttle"]
    assert validate.validate(json.loads(text), shuttle) == []
    demo = validate.parse(text, shuttle)
    assert isinstance(demo, DemoFile)
    assert demo.shuttle == shuttle
    assert len(demo.projects) == len(json.loads(text)["projects"])


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_every_invalid_vector_gives_exactly_the_recorded_messages(path: Path):
    expected = json.loads(path.with_suffix(".errors.json").read_text())
    assert expected, "an invalid vector records at least one message"
    assert validate.validate(json.loads(path.read_text()), SHUTTLE) == expected
    with pytest.raises(DemoError) as raised:
        validate.parse(path.read_text(), SHUTTLE)
    assert raised.value.errors == expected


def test_float_where_integer_required_is_rejected():
    # Python's json keeps 1.0 a float, and the board rejects it (the configurator cannot see it)
    assert validate.validate(conf_with(address=907.0), SHUTTLE) == [
        'project 0: "address" must be an integer >= 0, got 907.0'
    ]
    assert validate.validate({**conf_with(), "version": 1.0}, SHUTTLE) == [
        '"version" must be 1, got 1.0'
    ]
    for field in ("subtile", "duration_ms", "clock_hz", "ui_in"):
        assert len(validate.validate(conf_with(**{field: 1.0}), SHUTTLE)) == 1, field
    steps = [{"set": 1.0}, {"wait_ms": 5}]
    assert validate.validate(conf_with(steps=steps), SHUTTLE) == [
        'project 0 step 0: "set" must be an integer 0..255, got 1.0'
    ]
    steps = [{"set": 1}, {"wait_ms": 5.0}]
    assert validate.validate(conf_with(steps=steps), SHUTTLE) == [
        'project 0 step 1: "wait_ms" must be an integer >= 0, got 5.0',
        'project 0: "steps" must wait for some time in total',
    ]
    # the same through the text parser: 1.0 must survive json.loads as a float
    with pytest.raises(DemoError):
        validate.parse(json.dumps(conf_with(address=907.0)), SHUTTLE)


def test_booleans_are_not_integers():
    for field in ("address", "subtile", "duration_ms", "clock_hz", "ui_in"):
        errors = validate.validate(conf_with(**{field: True}), SHUTTLE)
        assert errors == [
            f'project 0: "{field}" must be '
            + {
                "address": "an integer >= 0",
                "subtile": "an integer >= 0",
                "duration_ms": "an integer > 0",
                "clock_hz": "an integer >= 0",
                "ui_in": "an integer 0..255",
            }[field]
            + ", got True"
        ]
    assert validate.validate({**conf_with(), "version": True}, SHUTTLE) == [
        '"version" must be 1, got True'
    ]


def test_parse_reports_text_that_is_not_json():
    with pytest.raises(DemoError) as raised:
        validate.parse("{", SHUTTLE)
    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith("not valid JSON: ")
    with pytest.raises(DemoError) as raised:
        validate.parse("", SHUTTLE)
    assert raised.value.errors[0].startswith("not valid JSON: ")


def test_parse_rejects_duplicate_keys():
    text = '{"version": 1, "version": 1, "shuttle": "ttsky25a", "projects": []}'
    with pytest.raises(DemoError) as raised:
        validate.parse(text, SHUTTLE)
    assert len(raised.value.errors) == 1
    assert raised.value.errors[0].startswith("not valid JSON: ")
    assert "version" in raised.value.errors[0]
    nested = '{"version": 1, "shuttle": "ttsky25a", "projects": [{"set": 1, "set": 2}]}'
    with pytest.raises(DemoError) as raised:
        validate.parse(nested, SHUTTLE)
    assert raised.value.errors[0].startswith("not valid JSON: ")


def test_parse_rejects_a_top_level_that_is_not_an_object():
    for text in ("[1, 2]", "3", "null", '"x"'):
        with pytest.raises(DemoError) as raised:
            validate.parse(text, SHUTTLE)
        assert raised.value.errors == ["must be a JSON object"]


def test_demo_error_message_lists_every_problem():
    err = DemoError(["one", "two"])
    assert err.errors == ["one", "two"]
    assert "one" in str(err)
    assert "two" in str(err)


def test_parse_builds_the_dataclasses():
    text = (DATA / "valid" / "steps-and-subtile.json").read_text()
    demo = validate.parse(text, SHUTTLE)
    assert demo == DemoFile(
        version=1,
        shuttle=SHUTTLE,
        projects=(
            Entry(260, 0, 30000, 25000000, 7, (Set(6), WaitMs(40), Set(7), WaitMs(250))),
            Entry(10, 1, 1, 0, 255, (WaitMs(0), WaitMs(1))),
        ),
    )


def test_parse_builds_gamepad_steps():
    text = (DATA / "valid" / "gamepad.json").read_text()
    steps = validate.parse(text, SHUTTLE).projects[0].steps
    assert steps[0] == Gamepad(((),))
    assert steps[2] == Gamepad((("start",),))
    assert steps[5] == Gamepad(
        (
            (
                "b",
                "y",
                "select",
                "start",
                "up",
                "down",
                "left",
                "right",
                "a",
                "x",
                "l",
            ),
            ("r",),
        )
    )
    assert steps[7] == Gamepad(())


def test_entry_types_are_frozen():
    entry = Entry(1, 0, 10, 0, 0, ())
    with pytest.raises(AttributeError):
        entry.address = 2  # type: ignore[misc]


# the gamepad word -------------------------------------------------------------------------


def test_button_names_are_in_wire_order():
    assert validate.GAMEPAD_BUTTONS == (
        "b",
        "y",
        "select",
        "start",
        "up",
        "down",
        "left",
        "right",
        "a",
        "x",
        "l",
        "r",
    )
    assert validate.GAMEPAD_PINS == 0x70


def test_gamepad_bits_puts_player_one_last_and_fills_empty_sockets_with_ones():
    bits = validate.gamepad_bits
    assert bits(()) == 0xFFFFFF
    assert bits(((),)) == 0x000FFF
    assert bits((("b",),)) == 0x001FFF
    assert bits((("start", "r"),)) == (((1 << 3) | (1 << 11)) << 12) | 0xFFF
    assert bits((("a",), ("left",))) == ((1 << 8) << 12) | (1 << 6)
    assert bits(((), ())) == 0


def test_gamepad_bits_second_controller_is_the_low_half():
    assert validate.gamepad_bits(((), ("y",))) == 1 << 1
    assert validate.gamepad_bits((("y",), ())) == 1 << 13
