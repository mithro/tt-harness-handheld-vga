# SPDX-License-Identifier: Apache-2.0
"""The demo.json format: a validator with the board's own messages, and the gamepad word.

`validate()` is `validateDemo()` of tt-demo-configurator's `src/lib/demoFile.ts` (Apache-2.0)
without a chip lookup, ported to Python, where the values in messages are printed with the real
`str()` the board's `validate()` uses. The structure follows `demoFile.ts` (checked against the
configurator at commit 8b7c392), not the board's LGPL-2.1 `demo.py`, whose behaviour is only
described, in `docs/demo-runner.md`. Where the board's validator and the configurator's differ,
this follows the board: see "Differences" in `docs/demo-runner.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

VERSION = 1
GAMEPAD_BUTTONS: tuple[str, ...] = (
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
GAMEPAD_PINS = 0x70
_TOP_FIELDS = ("version", "shuttle", "projects")
_ENTRY_FIELDS = ("address", "subtile", "duration_ms", "clock_hz", "ui_in", "steps")


class DemoError(Exception):
    """A demo.json that cannot be used; `errors` has every problem found, as the board logs them."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True)
class Set:
    value: int


@dataclass(frozen=True)
class WaitMs:
    ms: int


@dataclass(frozen=True)
class Gamepad:
    """The controllers plugged in (0 to 2, player 1 first), each as its held buttons in wire order."""

    controllers: tuple[tuple[str, ...], ...]


Step = Set | WaitMs | Gamepad


@dataclass(frozen=True)
class Entry:
    address: int
    subtile: int
    duration_ms: int
    clock_hz: int
    ui_in: int
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class DemoFile:
    version: int
    shuttle: str
    projects: tuple[Entry, ...]


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _uses_gamepad(steps: object) -> bool:
    if not isinstance(steps, list):
        return False
    return any(isinstance(s, dict) and len(s) == 1 and "gamepad" in s for s in steps)


def _check_gamepad(here: str, v: object, errors: list[str]) -> None:
    if not isinstance(v, list) or len(v) > 2:
        errors.append(f'{here}: "gamepad" must be a list of 0 to 2 controllers, got {v}')
        return
    for j, c in enumerate(v):
        what = f"{here}: gamepad controller {j + 1}"
        if not isinstance(c, dict) or len(c) != 1 or "buttons" not in c:
            errors.append(f'{what} must be an object with exactly one key, "buttons": {c}')
            continue
        buttons = c["buttons"]
        if not isinstance(buttons, list):
            errors.append(f'{what}: "buttons" must be a list, got {buttons}')
            continue
        unknown = [b for b in buttons if b not in GAMEPAD_BUTTONS]
        for b in unknown:
            errors.append(f"{what}: unknown button {b}")
        if unknown:
            continue
        order = [GAMEPAD_BUTTONS.index(b) for b in buttons]
        if any(order[k] <= order[k - 1] for k in range(1, len(order))):
            errors.append(
                f'{what}: "buttons" must name each button once, in the order '
                f"{', '.join(GAMEPAD_BUTTONS)}, got {buttons}"
            )
        elif len(buttons) == len(GAMEPAD_BUTTONS):
            errors.append(f"{what}: all 12 buttons at once reads as no controller")


def _check_steps(where: str, steps: object, errors: list[str]) -> None:
    if not isinstance(steps, list):
        errors.append(f'{where}: "steps" must be a list')
        return
    pad = _uses_gamepad(steps)
    total_wait = 0
    for i, step in enumerate(steps):
        here = f"{where} step {i}"
        if not isinstance(step, dict) or len(step) != 1:
            errors.append(f"{here}: must be an object with exactly one key: {step}")
            continue
        if "set" in step:
            v = step["set"]
            if not (_is_int(v) and 0 <= v <= 0xFF):
                errors.append(f'{here}: "set" must be an integer 0..255, got {v}')
            elif pad and v & GAMEPAD_PINS:
                errors.append(f'{here}: "set" must keep bits 4..6 (the gamepad) clear, got {v}')
        elif "gamepad" in step:
            _check_gamepad(here, step["gamepad"], errors)
        elif "wait_ms" in step:
            v = step["wait_ms"]
            if not (_is_int(v) and v >= 0):
                errors.append(f'{here}: "wait_ms" must be an integer >= 0, got {v}')
            else:
                total_wait += v
        else:
            errors.append(f"{here}: unknown step {step}")
    if steps and total_wait <= 0:
        errors.append(f'{where}: "steps" must wait for some time in total')


def _check_entry(where: str, e: object, errors: list[str]) -> bool:
    if not isinstance(e, dict):
        errors.append(f"{where}: must be an object")
        return False
    ok = True
    for f in e:
        if f not in _ENTRY_FIELDS:
            errors.append(f'{where}: unknown field "{f}"')
            ok = False
    for f in _ENTRY_FIELDS:
        if f not in e:
            errors.append(f'{where}: missing "{f}"')
            ok = False
    if not ok:
        return False
    checks = (
        ("address", _is_int(e["address"]) and e["address"] >= 0, "an integer >= 0"),
        ("subtile", _is_int(e["subtile"]) and e["subtile"] >= 0, "an integer >= 0"),
        ("duration_ms", _is_int(e["duration_ms"]) and e["duration_ms"] > 0, "an integer > 0"),
        ("clock_hz", _is_int(e["clock_hz"]) and e["clock_hz"] >= 0, "an integer >= 0"),
        ("ui_in", _is_int(e["ui_in"]) and 0 <= e["ui_in"] <= 0xFF, "an integer 0..255"),
    )
    for field, good, what in checks:
        if not good:
            errors.append(f'{where}: "{field}" must be {what}, got {e[field]}')
            ok = False
    n = len(errors)
    _check_steps(where, e["steps"], errors)
    if _uses_gamepad(e["steps"]):
        if _is_int(e["ui_in"]) and e["ui_in"] & GAMEPAD_PINS:
            errors.append(
                f'{where}: "ui_in" must keep bits 4..6 (the gamepad) clear, got {e["ui_in"]}'
            )
        if _is_int(e["clock_hz"]) and e["clock_hz"] == 0:
            errors.append(
                f'{where}: "clock_hz" must be > 0 with "gamepad" steps: '
                "the design reads the gamepad on its clock"
            )
    return ok and len(errors) == n


def validate(conf: object, shuttle: str) -> list[str]:
    """Every problem with a parsed demo.json for this chip (`shuttle`); empty means valid.

    The messages are those of the board's validator and of the configurator's `validateDemo`
    without a lookup, so nothing is checked against the projects on the chip.
    """
    errors: list[str] = []
    if not isinstance(conf, dict):
        return ["must be a JSON object"]
    errors.extend(f'unknown field "{f}"' for f in conf if f not in _TOP_FIELDS)
    missing = [f for f in _TOP_FIELDS if f not in conf]
    errors.extend(f'missing "{f}"' for f in missing)
    if missing:
        return errors
    if conf["version"] != VERSION or not _is_int(conf["version"]):
        errors.append(f'"version" must be {VERSION}, got {conf["version"]}')
    if conf["shuttle"] != shuttle:
        errors.append(f'"shuttle" is "{conf["shuttle"]}" but this chip is "{shuttle}"')
    projects = conf["projects"]
    if not isinstance(projects, list) or not projects:
        errors.append('"projects" must be a non-empty list')
        return errors
    for i, e in enumerate(projects):
        _check_entry(f"project {i}", e, errors)
    return errors


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def _step(raw: dict) -> Step:
    if "set" in raw:
        return Set(raw["set"])
    if "wait_ms" in raw:
        return WaitMs(raw["wait_ms"])
    return Gamepad(tuple(tuple(c["buttons"]) for c in raw["gamepad"]))


def parse(text: str, shuttle: str) -> DemoFile:
    """Parse and validate the text of a demo.json; raises `DemoError` with every problem.

    Text that is not JSON, and an object that repeats a key (which the board would accept, taking
    the last value), give a single `not valid JSON: ...` message, as `parseDemoText` does for bad
    JSON; the detail after the colon is Python's and differs from JavaScript's. A top-level value
    that is not an object gives `must be a JSON object`, as `validate` does.
    """
    try:
        conf = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as e:
        raise DemoError([f"not valid JSON: {e}"]) from e
    errors = validate(conf, shuttle)
    if errors:
        raise DemoError(errors)
    projects = tuple(
        Entry(
            p["address"],
            p["subtile"],
            p["duration_ms"],
            p["clock_hz"],
            p["ui_in"],
            tuple(_step(s) for s in p["steps"]),
        )
        for p in conf["projects"]
    )
    return DemoFile(conf["version"], conf["shuttle"], projects)


def gamepad_bits(controllers: tuple[tuple[str, ...], ...]) -> int:
    """The 24-bit value a "gamepad" step sends; wire position `n` is bit `n`, sent `n`-th.

    Written from the frame description in `docs/demo-runner.md`: wire positions 0..11 belong to
    the second controller and 12..23 to the first; within a half, position `k` is button
    `GAMEPAD_BUTTONS[k]`. A position is 1 when that controller holds the button, and also 1 for
    every position of a controller that is not listed (an empty socket reads as all ones, which
    is why a validated file never names all twelve buttons).
    """
    if len(controllers) > 2:
        raise ValueError(f"at most 2 controllers, got {len(controllers)}")
    word = 0
    for position in range(2 * len(GAMEPAD_BUTTONS)):
        slot, k = divmod(position, len(GAMEPAD_BUTTONS))
        player = 1 - slot  # slot 0 is sent first and is the second controller
        if player >= len(controllers) or GAMEPAD_BUTTONS[k] in controllers[player]:
            word |= 1 << position
    return word
