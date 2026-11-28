# SPDX-License-Identifier: Apache-2.0
"""The firmware's demo.json parser and runner, built for this machine with the system C
compiler (`cc`) and run behind the test `hal` of `tests/firmware/host_main.c`.

The parser (`firmware/demo_parse.c`) must give the messages of `tt_harness.demo.validate`
byte for byte, and the runner (`firmware/runner.c`) must make the writes of
`tt_harness.demo.model.run`, event for event. The host program prints, for `parse`, either
`ok` and the parsed demo or `errors` and every message (each as its length and bytes), and for
`run`, every write the runner makes through the `hal` (`T kind value`, `T` the runner's
nominal time in ms) and every console line it logs (`log ...`).

A `gamepad` write is one 24-bit word at the `hal` (the emulator in the gateware sends the
levels); the tests expand it with `model.gamepad_levels` over the `ui_in` base, the
form `model.run` and the traces use.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from test_demo_model import expected_events

from tt_harness.core import vga
from tt_harness.demo import model, validate
from tt_harness.demo.model import Event
from tt_harness.soc import core, firmware, limits

REPO = Path(__file__).resolve().parent.parent
FIRMWARE = REPO / "firmware"
HOST_MAIN = Path(__file__).resolve().parent / "firmware" / "host_main.c"
DATA = Path(__file__).resolve().parent / "data" / "demo"
SHUTTLE = "ttsky25a"
VALID = sorted((DATA / "valid").glob("*.json"))
INVALID = sorted(
    p for p in (DATA / "invalid").glob("*.json") if not p.name.endswith(".errors.json")
)
TRACES = sorted((DATA / "traces").glob("*.json"))
SOURCES = ("console_fmt.c", "demo_parse.c", "pyrepr.c", "runner.c", "text.c", "vga_mode.c")
CC = shutil.which("cc")
CFLAGS = (
    "-std=c11",
    "-O1",
    "-g",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-fsanitize=address,undefined",
    "-fno-sanitize-recover=all",
)
ONE = {
    "address": 907,
    "subtile": 0,
    "duration_ms": 15000,
    "clock_hz": 25000000,
    "ui_in": 0,
    "steps": [],
}


@pytest.fixture(scope="session")
def host(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The host build of the parser and runner (`tests/firmware/host_main.c`)."""
    if CC is None:
        pytest.fail("no system C compiler (cc) on PATH")
    out = tmp_path_factory.mktemp("firmware-host")
    binary = out / "host"
    log = out / "cc.log"
    generated = out / "generated"
    firmware.write_portable_headers(generated)
    cmd = [
        CC,
        *CFLAGS,
        f"-I{FIRMWARE}",
        f"-I{generated}",
        f"-I{REPO / 'tt_harness' / 'core'}",
        "-o",
        str(binary),
        str(HOST_MAIN),
        *(str(FIRMWARE / s) for s in SOURCES),
    ]
    with log.open("w") as f:
        done = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, check=False)
    assert done.returncode == 0, log.read_text()
    return binary


def parse(host: Path, text: bytes, shuttle: str = SHUTTLE) -> tuple[str, list[str]]:
    """("ok", lines of the parsed demo) or ("errors", the messages)."""
    done = subprocess.run(
        [str(host), "parse", shuttle], input=text, capture_output=True, timeout=60, check=False
    )
    assert done.stderr == b"", done.stderr.decode(errors="replace")
    head, _, rest = done.stdout.partition(b"\n")
    if head == b"ok":
        assert done.returncode == 0
        return "ok", rest.decode().splitlines()
    assert head == b"errors" and done.returncode == 1, done.stdout[:2000]
    messages = []
    while rest:
        size, _, rest = rest.partition(b"\n")
        n = int(size)
        assert rest[n : n + 1] == b"\n"
        messages.append(rest[:n].decode("utf-8", errors="surrogatepass"))
        rest = rest[n + 1 :]
    return "errors", messages


def run(host: Path, demo: dict, until_ms: int, tick_ms: int = 1) -> tuple[list, list[str]]:
    """The runner on `demo` until `until_ms`: ([(t, kind, value)], [logged lines])."""
    text = json.dumps(demo).encode()
    done = subprocess.run(
        [str(host), "run", demo["shuttle"], str(until_ms), str(tick_ms)],
        input=text,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0 and done.stderr == b"", (done.stdout[-2000:], done.stderr)
    writes, logs = [], []
    for line in done.stdout.decode().splitlines():
        if line.startswith("log "):
            logs.append(line[4:])
        else:
            t, kind, value = line.split()
            writes.append((int(t), kind, int(value, 0)))
    return writes, logs


def as_events(writes: list, logs: list[str]) -> list[Event]:
    """The writes as `model.run` events of the first entry: a gamepad word becomes its levels
    over the last `ui_in`, and the selection of the next entry is `next` (and the end)."""
    selections = [ln for ln in logs if ln.startswith("state ") and " entry=" in ln]
    next_ms = None
    for line in selections[1:2]:
        fields = dict(f.split("=", 1) for f in line.split()[1:])
        next_ms = int(fields["t_ms"])
    events: list[Event] = []
    base = None
    seen_next = False
    for t, kind, value in writes:
        if next_ms is not None and (t > next_ms or (t == next_ms and kind == "ena" and events)):
            seen_next = True
            break
        if kind == "ui_in":
            base = value
        if kind == "gamepad":
            events.extend(Event(t, "gamepad", base | lv) for lv in model.gamepad_levels(value))
        else:
            events.append(Event(t, kind, value))
    if next_ms is not None:
        assert seen_next or not writes or writes[-1][0] <= next_ms
        events.append(Event(next_ms, "next", 0))
    return events


def demo_of(entry: dict, total_projects: int = 1, shuttle: str = SHUTTLE) -> dict:
    other = {**ONE, "address": 1, "duration_ms": 1000, "steps": []}
    return {"version": 1, "shuttle": shuttle, "projects": [entry] + [other] * (total_projects - 1)}


def parsed_lines(demo: validate.DemoFile) -> list[str]:
    """What the host prints for a parsed demo: one line a project, one a step."""
    out = [f"shuttle {demo.shuttle}", f"projects {len(demo.projects)}"]
    for p in demo.projects:
        out.append(f"project {p.address} {p.subtile} {p.duration_ms} {p.clock_hz} {p.ui_in}")
        for s in p.steps:
            if isinstance(s, validate.Set):
                out.append(f"set {s.value}")
            elif isinstance(s, validate.WaitMs):
                out.append(f"wait_ms {s.ms}")
            else:
                out.append(f"gamepad 0x{validate.gamepad_bits(s.controllers):06x}")
    return out


# The five tests of the brief ----------------------------------------------------------------


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_firmware_rejects_every_invalid_vector(host: Path, path: Path) -> None:
    expected = json.loads(path.with_suffix(".errors.json").read_text())
    assert parse(host, path.read_bytes()) == ("errors", expected)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_firmware_accepts_every_valid_vector(host: Path, path: Path) -> None:
    text = path.read_text()
    shuttle = json.loads(text)["shuttle"]
    assert parse(host, path.read_bytes(), shuttle) == (
        "ok",
        parsed_lines(validate.parse(text, shuttle)),
    )


@pytest.mark.parametrize("tick_ms", [1, 7])
@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_firmware_reproduces_every_trace(host: Path, path: Path, tick_ms: int) -> None:
    trace = json.loads(path.read_text())
    entry_raw = trace["entry"]
    total, until = trace["total_projects"], trace["until_ms"]
    entry = validate.parse(json.dumps(demo_of(entry_raw)), SHUTTLE).projects[0]
    writes, logs = run(host, demo_of(entry_raw, total), until, tick_ms)
    got = as_events(writes, logs)
    assert got == model.run(entry, total, until)
    assert got == expected_events(trace)
    # Every write is logged once, as a `state` line with the same time and value.
    states = [ln for ln in logs if ln.startswith("state ") and " entry=" not in ln]
    assert [ln.split()[1:] for ln in states] == [
        [f"t_ms={t}", f"{k}={f'0x{v:06x}' if k == 'gamepad' else v}"] for t, k, v in writes
    ]


def test_runner_never_resets_with_empty_steps(host: Path) -> None:
    for total in (1, 2):
        writes, _ = run(host, demo_of({**ONE, "duration_ms": 50}, total), 10_000)
        assert writes, "the selection writes"
        assert not [w for w in writes if w[1] == "rst_n"]
        # and a pass of the other entry's steps (none) never resets either
        assert all(w[1] in ("ena", "clock_hz", "ui_in") for w in writes)


def test_duration_timer_not_armed_for_one_project(host: Path) -> None:
    entry = {**ONE, "duration_ms": 100, "steps": [{"set": 3}, {"wait_ms": 40}]}
    writes, logs = run(host, demo_of(entry, 1), 5_000)
    selections = [ln for ln in logs if ln.startswith("state ") and " entry=" in ln]
    assert len(selections) == 1, selections
    assert [w for w in writes if w[1] == "ena"] == [(0, "ena", 1)]
    resets = [w for w in writes if w[1] == "rst_n"]
    assert len(resets) == 2 * (5_000 // 42) and resets[-1][0] > 4_900  # still looping
    # the same entry in a list of two is replaced after its duration
    writes, logs = run(host, demo_of(entry, 2), 5_000)
    selections = [ln for ln in logs if ln.startswith("state ") and " entry=" in ln]
    assert selections[1].split()[1] == "t_ms=100"


# Beyond the brief's five ----------------------------------------------------------------------


def test_the_selection_order_is_enable_clock_ui_in(host: Path) -> None:
    writes, _ = run(host, demo_of({**ONE, "ui_in": 9, "clock_hz": 0}), 0)
    assert writes == [(0, "ena", 1), (0, "clock_hz", 0), (0, "ui_in", 9)]


def test_a_reset_does_not_reapply_ui_in(host: Path) -> None:
    entry = {**ONE, "ui_in": 5, "steps": [{"wait_ms": 10}]}
    writes, _ = run(host, demo_of(entry), 25)
    assert writes == [
        (0, "ena", 1),
        (0, "clock_hz", 25000000),
        (0, "ui_in", 5),
        (10, "rst_n", 0),
        (12, "rst_n", 1),
        (22, "rst_n", 0),
        (24, "rst_n", 1),
    ]


def test_the_entries_wrap_from_last_to_first(host: Path) -> None:
    demo = {
        "version": 1,
        "shuttle": SHUTTLE,
        "projects": [
            {**ONE, "address": 1, "duration_ms": 30, "ui_in": 1},
            {**ONE, "address": 2, "duration_ms": 20, "ui_in": 2},
        ],
    }
    _, logs = run(host, demo, 100)
    sel = [ln.split()[1:3] for ln in logs if ln.startswith("state ") and " entry=" in ln]
    assert sel == [
        ["t_ms=0", "entry=0"],
        ["t_ms=30", "entry=1"],
        ["t_ms=50", "entry=0"],
        ["t_ms=80", "entry=1"],
        ["t_ms=100", "entry=0"],
    ]


NOT_JSON = (
    b"",
    b" ",
    b"{",
    b"}",
    b"[1,]",
    b"[,1]",
    b"[1,,2]",
    b'{"a" "b"}',
    b'{"a":1,}',
    b'{"a":}',
    b'{"a"}',
    b"01",
    b"-01",
    b"{} {}",
    b"1 2",
    b"tru",
    b"truex",
    b"nul",
    b'"\x01"',
    b"nan",
    b"+1",
    b"{a:1}",
    b"[1:2]",
    b"1.",
    b".5",
    b"-",
    b"1e",
    b"1e+",
    b'"\\x"',
    b'"\\u12"',
    b"\x00",
    b'{"a":1}\x00',
    b"\xef\xbb\xbf{}",
    b"[1}",
    b'{"a":1]',
    b"{1:2}",
    b'{"a":1 "b":2}',
    b"Infinityx",
    b"--1",
    b"1.5e3.2",
)


@pytest.mark.parametrize("text", NOT_JSON, ids=lambda t: repr(t)[2:-1] or "empty")
def test_text_that_is_not_json(host: Path, text: bytes) -> None:
    with pytest.raises(validate.DemoError) as raised:
        validate.parse(text.decode(), SHUTTLE)
    assert raised.value.errors[0].startswith("not valid JSON: "), raised.value.errors
    kind, messages = parse(host, text)
    assert kind == "errors" and len(messages) == 1, (text, messages)
    assert messages[0].startswith("not valid JSON: "), (text, messages)


@pytest.mark.parametrize(
    "text", [b"5", b"5\n", b" []", b'"x"', b"null", b"1.5", b"NaN", b"-Infinity", b"true"]
)
def test_json_that_is_not_an_object(host: Path, text: bytes) -> None:
    assert validate.validate(json.loads(text), SHUTTLE) == ["must be a JSON object"]
    assert parse(host, text) == ("errors", ["must be a JSON object"]), text


def test_nan_and_infinity_are_values_as_in_python(host: Path) -> None:
    text = json.dumps(demo_of({**ONE, "address": float("nan"), "ui_in": float("-inf")}))
    assert parse(host, text.encode()) == ("errors", validate.validate(json.loads(text), SHUTTLE))


UTF8 = (
    b"\xff",
    b"{\x80}",
    b'"\xc3"',
    b'"\xe2\x28\xa1"',
    b'"\xe2\x82"',
    b'"\xed\xa0\x80"',
    b'"\xf0\x9f\x98"',
    b'"\xc0\xaf"',
    b'"\xf4\x90\x80\x80"',
    b'"ok" \xe2',
)


@pytest.mark.parametrize("text", UTF8, ids=lambda t: t.hex())
def test_text_that_is_not_utf8_gives_pythons_message(host: Path, text: bytes) -> None:
    with pytest.raises(UnicodeDecodeError) as raised:
        text.decode("utf-8")
    assert parse(host, text) == ("errors", [f"not UTF-8: {raised.value}"])


def test_a_repeated_key_is_not_valid_json(host: Path) -> None:
    text = b'{"version": 1, "version": 1, "shuttle": "ttsky25a", "projects": []}'
    with pytest.raises(validate.DemoError) as raised:
        validate.parse(text.decode(), SHUTTLE)
    assert parse(host, text) == ("errors", raised.value.errors)


def test_a_firmware_limit_is_a_loud_error(host: Path) -> None:
    steps = [{"set": 1}, {"wait_ms": 1}] * 300  # 600 steps: valid, but more than the firmware holds
    demo = demo_of({**ONE, "steps": steps})
    assert validate.validate(demo, SHUTTLE) == []
    assert parse(host, json.dumps(demo).encode()) == (
        "errors",
        ["firmware limit: more than 512 steps in all projects (DEMO_MAX_STEPS)"],
    )
    pad = {"gamepad": [{"buttons": list(validate.GAMEPAD_BUTTONS[:11])}]}
    demo = demo_of({**ONE, "steps": [pad] * 125 + [{"wait_ms": 1}]})  # 2,100-odd tokens
    assert validate.validate(demo, SHUTTLE) == []
    assert parse(host, json.dumps(demo).encode()) == (
        "errors",
        ["firmware limit: more than 2048 JSON tokens (DEMO_MAX_TOKENS)"],
    )
    many = demo_of({**ONE, "steps": [1.5] * 200})  # more messages than the error buffer holds
    kind, messages = parse(host, json.dumps(many).encode())
    want = validate.validate(many, SHUTTLE)
    assert (kind, messages) == ("errors", limits.stored(want))
    assert len(messages) < len(want)
    left_out = len(want) - (len(messages) - 1)
    assert messages[-1] == f"{limits.NOT_SHOWN}{left_out}"


def test_a_message_too_long_to_keep_is_counted_and_later_ones_are_kept(host: Path) -> None:
    """One message longer than the error text holds is left out, the messages after it
    are kept, and the last message says one was not shown."""
    huge = {**ONE, "address": "x" * 5000}  # its message holds the whole value
    demo = {
        "version": 1,
        "shuttle": SHUTTLE,
        "projects": [huge, {**ONE, "subtile": -1}, {**ONE, "clock_hz": "fast"}],
    }
    want = validate.validate(demo, SHUTTLE)
    assert len(want) == 3 and len(want[0]) > limits.ERRORS_TEXT
    assert parse(host, json.dumps(demo).encode()) == (
        "errors",
        [want[1], want[2], f"{limits.NOT_SHOWN}1"],
    )
    assert limits.stored(want) == [want[1], want[2], f"{limits.NOT_SHOWN}1"]


@pytest.mark.parametrize(
    "entry",
    [
        {**ONE, "steps": [{"set": 1}, {"wait_ms": 1}] * 300},
        {**ONE, "clock_hz": 2**40, "duration_ms": 2**31},
        {
            **ONE,
            "steps": [{"gamepad": [{"buttons": list(validate.GAMEPAD_BUTTONS[:11])}]}] * 125
            + [{"wait_ms": 1}],
        },
        {**ONE, "address": 10**120},
    ],
    ids=["steps", "32-bit", "tokens", "digits"],
)
def test_a_limit_fails_at_build_and_at_boot_with_the_same_text(host: Path, entry: dict) -> None:
    """A file the validator accepts but the firmware cannot hold: the build
    (`core.demo_entry`) refuses it with exactly the messages the firmware gives at boot."""
    text = json.dumps(demo_of(entry))
    assert validate.validate(json.loads(text), SHUTTLE) == []
    with pytest.raises(validate.DemoError) as raised:
        core.demo_entry(text.encode(), SHUTTLE)
    assert raised.value.errors
    assert all(m.startswith(limits.PREFIX) for m in raised.value.errors)
    assert parse(host, text.encode()) == ("errors", raised.value.errors)


def test_a_number_beyond_32_bits_is_a_loud_error(host: Path) -> None:
    demo = demo_of({**ONE, "clock_hz": 2**40})
    assert validate.validate(demo, SHUTTLE) == []
    assert parse(host, json.dumps(demo).encode()) == (
        "errors",
        [f'firmware limit: project 0: "clock_hz" is {2**40}, more than 4294967295'],
    )


# Python's str() of values, and messages of mutated files, against the validator ------------

FLOATS = (
    "0.0",
    "-0.0",
    "1.0",
    "1e22",
    "1e16",
    "1e15",
    "1e-5",
    "1e-4",
    "0.0001",
    "123456789.125",
    "2.5e-7",
    "0.1",
    "0.30000000000000004",
    "1.7976931348623157e308",
    "1e309",
    "-1e400",
    "5e-324",
    "4.9e-324",
    "2.4e-324",
    "2.5e-324",
    "1e-400",
    "2.2250738585072014e-308",
    "2.2250738585072011e-308",
    "9007199254740993",
    "9007199254740993.0",
    "123.456e-2",
    "0.000001234",
    "1234567890123456789012345678901234567890.0",
    "3.141592653589793238462643",
    "1E+2",
    "1e-07",
    "100000000000000000000000.0",
    "8.98846567431158e307",
    "0.5",
    "-2.5e+300",
)


def test_python_str_of_numbers(host: Path) -> None:
    rng = random.Random(7)
    literals = list(FLOATS)
    for _ in range(400):
        mant = str(rng.randrange(1, 10 ** rng.randrange(1, 20)))
        dot = rng.randrange(len(mant) + 1)
        lit = (mant[:dot] or "0") + "." + (mant[dot:] or "0")
        literals.append(f"{lit}e{rng.randrange(-330, 310)}")
    for _ in range(200):
        bits = rng.getrandbits(64)
        f = struct.unpack("<d", bits.to_bytes(8, "little"))[0]
        if math.isfinite(f):
            literals.append(repr(f))
    literals += ["-0", "0", "12345678901234567890123", "-99999999999999999999999999"]
    for at in range(0, len(literals), 40):  # 40 messages a file: within DEMO_ERRORS_TEXT
        steps = ",".join(f"[{lit}]" for lit in literals[at : at + 40])
        entry = (
            '{"address": 1, "subtile": 0, "duration_ms": 1, "clock_hz": 1, "ui_in": 0, '
            f'"steps": [{steps}, {{"wait_ms": 1}}]}}'
        )
        text = f'{{"version": 1, "shuttle": "{SHUTTLE}", "projects": [{entry}]}}'
        with pytest.raises(validate.DemoError) as raised:
            validate.parse(text, SHUTTLE)
        assert parse(host, text.encode()) == ("errors", raised.value.errors)


STRINGS = (
    "",
    "a",
    "it's",
    'say "hi"',
    "both ' and \"",
    "back\\slash",
    "tab\there",
    "nl\nx",
    "cr\rx",
    "\x01\x1f\x7f",
    "\x80\x9f\xa0\xad",
    "é ü ß",
    "日本語",
    "\U0001f600",
    "\u2028\u200b\ufeff",
    "\ue000",
    "\x00",
    "/",
    "\b\f",
    # unassigned in Unicode: Python escapes them; a hand-written table did not
    "".join(chr(c) for c in (0x378, 0x380, 0x38B, 0x3A2)),
    chr(0x0B4D) + chr(0x0B4E) + chr(0x1F8FF) + chr(0xE0080),
)


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(9 if depth < 3 else 6)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, -1, 1, 255, 256, 2**31, 2**64 + 3, -(2**70)])
    if kind == 3:
        return rng.choice([0.0, -0.0, 1.5, 1e22, 2.5e-7, 1.0, 1e100, 0.1])
    if kind in (4, 5):
        return rng.choice(STRINGS)
    if kind in (6, 7):
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        rng.choice(STRINGS + ("set", "wait_ms", "gamepad", "buttons")): _random_value(
            rng, depth + 1
        )
        for _ in range(rng.randrange(4))
    }


def _mutate(rng: random.Random, conf):
    """`conf` with one value somewhere replaced by a random one, or a key added or dropped."""
    if isinstance(conf, dict) and conf:
        key = rng.choice(list(conf))
        roll = rng.randrange(10)
        if roll == 0:
            return {k: v for k, v in conf.items() if k != key}
        if roll == 1:
            return {**conf, rng.choice(STRINGS): _random_value(rng)}
        if roll < 5:
            return {**conf, key: _random_value(rng)}
        return {**conf, key: _mutate(rng, conf[key])}
    if isinstance(conf, list) and conf:
        i = rng.randrange(len(conf))
        out = list(conf)
        out[i] = _random_value(rng) if rng.randrange(3) == 0 else _mutate(rng, conf[i])
        return out
    return _random_value(rng)


@pytest.mark.parametrize("seed", range(8))
def test_mutated_files_give_the_validators_messages(host: Path, seed: int) -> None:
    rng = random.Random(seed)
    bases = [json.loads(p.read_text()) for p in VALID + INVALID]
    for _ in range(60):
        conf = rng.choice(bases)
        for _ in range(rng.randrange(1, 4)):
            conf = _mutate(rng, conf)
        text = json.dumps(conf, ensure_ascii=rng.randrange(2) == 0)
        want = validate.validate(conf, SHUTTLE)
        kind, messages = parse(host, text.encode("utf-8", errors="surrogatepass"))
        if want:
            assert (kind, messages) == ("errors", limits.stored(want)), text
        else:  # valid; the firmware may still not hold it, as the build check says
            over = limits.check(text, SHUTTLE)
            assert (kind, messages if over else []) == ("errors" if over else "ok", over), text


def _python_capture(cpl, lpf, hsw, vsl, clock_hz, fb_w, fb_h):
    """What the firmware should report: `vga.classify`, and the decimation that fits the
    picture in the framebuffer (every n-th pixel and row, n = ceil(size / fb), 1 to 3)."""
    mode = vga.classify(cpl, lpf, hsw, vsl, clock_hz)
    if mode is None:
        return None
    h_step = min(3, max(1, -(-mode.width // fb_w)))
    v_step = min(3, max(1, -(-mode.height // fb_h)))
    return mode, h_step, v_step


def test_vga_classify_matches_python_across_the_mode_table(host: Path) -> None:
    """`vga_mode.c` is `vga.classify` in C: every row of the table, with the measured sync
    widths varied, the edges where the picture no longer fits, totals one off, no sync
    pulse, and clocks around each row's rate (the refresh text is Python's `:.1f`)."""
    rng = random.Random(7)
    cases = []
    for spec in vga.MODES:
        for divider in spec.dividers:
            cpl, lpf = spec.h_total * divider, spec.v_total
            h_room = cpl - spec.width * divider - spec.h_back * divider
            v_room = lpf - spec.height - spec.v_back
            for hsw in (0, 1, spec.h_sync * divider, h_room, h_room + 1):
                for vsl in (0, 1, spec.v_sync, v_room, v_room + 1):
                    cases.append((cpl, lpf, hsw, vsl, spec.pixel_clock_hz * divider))
            for dc, dl in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                cases.append((cpl + dc, lpf + dl, spec.h_sync, spec.v_sync, 0))
            for _ in range(40):
                clock = rng.randrange(1, 2 * spec.pixel_clock_hz * divider)
                cases.append((cpl, lpf, spec.h_sync * divider, spec.v_sync, clock))
            cases.append((cpl, lpf, spec.h_sync * divider, spec.v_sync, 0))
    fbs = [(320, 240), (640, 480), (100, 50)]
    lines = [(*c, *fbs[i % len(fbs)]) for i, c in enumerate(cases)]
    text = "".join(" ".join(map(str, ln)) + "\n" for ln in lines)
    done = subprocess.run(
        [str(host), "classify"], input=text.encode(), capture_output=True, check=False
    )
    assert done.returncode == 0, done.stderr.decode()
    got = done.stdout.decode().splitlines()
    assert len(got) == len(lines)
    matched = 0
    for ln, out in zip(lines, got, strict=True):
        want = _python_capture(*ln)
        if want is None:
            assert out == "none", ln
            continue
        matched += 1
        mode, h_step, v_step = want
        name, divider, h_off, v_off, width, height, hs, vs, refresh = out.split(" ")
        python_name = f"{mode.width}x{mode.height}@{refresh}Hz"
        if mode.divider > 1:
            python_name += f" (clock x{mode.divider})"
        assert python_name == mode.name, ln
        assert name.startswith(f"{mode.width}x{mode.height}@"), ln
        assert tuple(map(int, (divider, h_off, v_off, width, height, hs, vs))) == (
            mode.divider,
            mode.h_offset,
            mode.v_offset,
            mode.width,
            mode.height,
            h_step,
            v_step,
        ), ln
    rows = sum(len(spec.dividers) for spec in vga.MODES)
    assert matched >= rows * 45, (matched, len(lines))  # the clock sweep and the fits
    assert len(lines) - matched >= rows * 10  # and the refusals


def test_isprintable_is_pythons_for_every_code_point(host: Path) -> None:
    """`py_isprintable` against `str.isprintable()` for all of 0..0x10FFFF."""
    done = subprocess.run([str(host), "nonprintable"], capture_output=True, check=False)
    assert done.returncode == 0 and done.stderr == b""
    got = [tuple(int(x, 16) for x in line.split()) for line in done.stdout.decode().splitlines()]
    want = []
    for a, b in firmware.nonprintable_runs():  # merge the split at the plane boundary
        if want and want[-1][1] + 1 == a:
            want[-1] = (want[-1][0], b)
        else:
            want.append((a, b))
    assert got == want
    # and those are Python's runs, computed here without the generator
    flags = [not chr(cp).isprintable() for cp in range(0x110000)]
    runs = []
    for cp, np in enumerate(flags):
        if np and (cp == 0 or not flags[cp - 1]):
            start = cp
        if np and (cp == 0x10FFFF or not flags[cp + 1]):
            runs.append((start, cp))
    assert got == runs


@pytest.mark.parametrize("text", STRINGS)
def test_a_string_value_prints_as_pythons_repr(host: Path, text: str) -> None:
    demo = demo_of({**ONE, "address": text})
    want = validate.validate(demo, SHUTTLE)
    assert len(want) == 1
    raw = json.dumps(demo, ensure_ascii=False).encode("utf-8", errors="surrogatepass")
    assert parse(host, raw) == ("errors", want)


def _console_escape(message: bytes) -> bytes:
    """docs/registers.md, "Console", `error msg=`."""
    out = bytearray()
    for b in message:
        if b == 0x5C:
            out += b"\\\\"
        elif b == 0x0A:
            out += b"\\n"
        elif b == 0x0D:
            out += b"\\r"
        elif b < 0x20 or b == 0x7F:
            out += b"\\x%02x" % b
        else:
            out.append(b)
    return bytes(out)


@pytest.mark.parametrize(
    "message",
    [
        b"short",
        bytes(range(256)) * 3,  # every byte; 1,500-odd bytes once escaped
        b"x" * 4000,  # as long as a message can be
        ("\\ \n \r " * 200).encode(),
    ],
    ids=["short", "every-byte", "4000", "escapes"],
)
def test_an_error_line_is_never_cut(host: Path, message: bytes) -> None:
    """The console's `error msg=` line carries the whole message, however long once
    escaped (the firmware once cut it at 511 bytes)."""
    done = subprocess.run([str(host), "errline"], input=message, capture_output=True, check=False)
    assert done.returncode == 0 and done.stderr == b""
    want = b"error msg=" + _console_escape(message) + b"\n"
    assert len(want) > 512 or message == b"short"
    assert done.stdout == want
