# The demo runner: what it does, for firmware authors

This describes the behaviour of the Tiny Tapeout demo board's demo runner so our firmware can
behave the same. It is a description in our own words of
`tt-demo-configurator/src/data/demo.py` (checkout at commit 8b7c392; the file is a pinned copy of
`ttboard/demo.py` from the firmware, branch demo-gamepad, commit c32d62a). That file is
LGPL-2.1 and this repository is Apache-2.0: implement from this text, not from that code. Line
numbers below are lines of that `demo.py`. The Python model in `tt_harness/demo/model.py` and the
hand-written traces in `tests/data/demo/traces/` follow this document; the validator is
`tt_harness/demo/validate.py`.

All times are the runner's own waits in milliseconds, measured from the moment an entry is
selected. Calls are taken to cost no time, except the 2 ms sleep during a reset (see "Nominal
time").

## The file

`/demo.json` is an object with exactly `version` (the integer 1), `shuttle` (a string that must
equal the chip's shuttle name) and `projects` (a non-empty list) (lines 71, 282-308). An entry has
exactly the fields `address`, `subtile`, `duration_ms`, `clock_hz`, `ui_in`, `steps` (72, 244-269):

| field | type | meaning |
|---|---|---|
| `address`, `subtile` | integer >= 0 | which project; subtile 0 is the plain address (162-170) |
| `duration_ms` | integer > 0 | how long to show it |
| `clock_hz` | integer >= 0 | project clock; 0 means no clock |
| `ui_in` | integer 0..255 | value written to `ui_in` after enabling |
| `steps` | list | scripted writes, played in order (may be empty) |

Steps (212-241): `{"set": N}` with N in 0..255, `{"wait_ms": N}` with N >= 0, or `{"gamepad":
[...]}`. Each step has exactly one key. A non-empty `steps` must have waits adding up to more than
0 ms (239-241), so the loop never spins. Every number must be a true integer: booleans and
floats such as `1.0` are rejected (137-138).

An entry with any `gamepad` step must have `ui_in` and every `set` value with bits 4..6 (0x70)
clear, and `clock_hz` > 0 (225-228, 272-278). A `gamepad` value is a list of 0 to 2 controllers;
each is `{"buttons": [...]}` naming buttons from the list in "The gamepad" below, each at most
once and in that order; all twelve at once is rejected (182-209).

## What the runner does for one entry

Entries are shown in list order, wrapping from the last to the first (706-708). Selecting an entry
(722-748), in this order, all at time 0:

1. Enable the project at `address`/`subtile` (726-727). If that is refused, or the project that
   the mux reports as enabled is not the one asked for, the entry fails (733-736, see "Errors").
2. Clock: if `clock_hz` is non-zero start the project clock at that frequency, else stop it
   (739-742).
3. Write `ui_in` with the entry's `ui_in` (743).
4. If `steps` is not empty, start playing them at once (744-747); with no steps nothing more is
   written, ever (552-554).
5. Arm the duration timer (717-719), see below. This happens after steps 1-4 have returned, so in
   real time it starts after the first run of the steps (including the first gamepad report).

Nothing writes `rst_n` when an entry is selected: whatever enabling a project does to the reset
line is the SDK's, not the runner's (open question 1). Selecting the next entry stops the step
player of the previous one first (723, 750-753), then does the same five things.

### The duration timer

With more than one entry, a one-shot timer of `duration_ms` is armed for each entry and, when it
fires, the runner selects the next entry (717-719, 755-761). With exactly one entry the timer is
never armed: the entry stays selected for ever and its steps keep looping (717). `next()` from the
REPL does the same as the timer firing (701-704).

### Steps

The player (511-614) keeps a step position, starting at the first step, and `ui_in`'s base value,
starting at the entry's `ui_in`.

- `set N`: write `ui_in` = N at once (576-578); N becomes the base value.
- `wait_ms N`, N > 0: stop and arm a one-shot timer of N ms; when it fires, continue with the next
  step (582-588). N = 0 does not wait at all and does not touch the timer (582, 587-588).
- `gamepad`: send a report at once (579-581), see below.
- When the position runs past the last step a pass is over (589-598). A pass ends at the end of
  its last wait, not at its start (570-573). The player then:
  1. asserts project reset (`rst_n` low) (593),
  2. sleeps 2 ms (594),
  3. releases reset (`rst_n` high) (595),
  4. if any `gamepad` step has run since the entry was selected, sends that last report again
     (596-598), because the reset cleared the design's copy and the buttons are still "held",
  5. starts again at the first step, at once.

What a pass does not do (29-32, 593-598): it does not rewrite `ui_in` (a `set` at the start of the
steps is how an entry re-applies a value), does not restart the clock, and does not re-enable the
project. A `set` that is the last step runs, then the reset follows immediately with no wait in
between. When there are no steps at all, nothing ever resets (32, 552-554).

## The gamepad

A `gamepad` step emulates a Gamepad Pmod with no Pmod plugged in. The design reads three of its
`ui_in` bits: `ui_in[4]` latch (0x10), `ui_in[5]` clock (0x20), `ui_in[6]` data (0x40); mask
0x70 (74-83). All three lines idle low. The design's receiver (not the runner) shifts in one
data bit per rising clock edge and takes the 24 bits it has on each rising latch edge; the runner's
comments say designs sample on the rising edge (109-111) and the configurator's `decodeGamepad`
in `src/lib/gamepad.ts` models the receiver that way.

### The 24-bit word

A report is 24 bits, sent bit 0 first (86-102). Each controller is a 12-bit mask, bit `i` set when
button `i` is held, with the buttons numbered in this order (bit 0 sent first):

`b, y, select, start, up, down, left, right, a, x, l, r`

Word = (player 2's mask) in bits 0..11, (player 1's mask) in bits 12..23. So the 12 bits sent
first are player 2 (or "no controller"), and the 12 sent last are player 1, which is the
controller `gamepad_pmod_single` and index 0 of `gamepad_pmod_dual` read. A controller that is not
listed sends 0xfff (twelve 1s), "no controller": `[]` sends 0xffffff, one controller sends player
2 as 0xfff. This is why a controller with all twelve buttons is rejected. `[{"buttons": []},
{"buttons": []}]` sends 0 (nothing held, both present). The Python form is
`validate.gamepad_bits`.

### The frame: levels and timing

For each of the 24 bits, bit 0 first, the runner writes `ui_in` three times (105-129): data with
clock low; data with clock high; data with clock low (so data changes only while the clock is
low, and a design samples on the rising clock edge). After bit 23 it writes the latch high (with
bit 23's data still on the data line), then the latch low (same data), then all three lines low.
A write whose level equals the one before is left out; the first level is compared with idle (0).
So a report is a list of 0x70-masked levels, 24 rising clock edges and one rising latch edge
long. Each written byte is the level ORed onto the base value (the last `set`, else the entry's
`ui_in`), over bits 0..3 and 7 (606-614).

There is no deliberate delay: no pulse length, no frame period. Every level lasts as long as one
write takes: some 17 us per write when the board can write the byte directly (RP2350, SDK
3.1.0), about 1 ms when it goes through the slow port, so a report takes about 1 ms or 60 ms
(600-607). A report is sent only when a `gamepad` step runs and after each reset (596-598),
never periodically, as a real Pmod would. The buttons "stay held" by the design keeping the last
report until the next one (36-38, 521-524). The level list for a report is worked out once, at
construction (531-535), not during the timers.

Because the next step starts only after the report is written, waits after a report start when
the report ends; the model treats the report as instantaneous (see "Nominal time").

## Nominal time

The model is `model.run(entry, total_projects, until_ms) -> list[Event]`. It returns the writes
of one entry from selection until `until_ms` (included), each `Event(t_ms, kind, value)`, in the
order they happen:

| kind | when | value |
|---|---|---|
| `ena` | selection, time 0 | 1 |
| `clock_hz` | selection | the entry's `clock_hz` (0 is "stop the clock") |
| `ui_in` | selection (the entry's `ui_in`) and each `set` | the byte written |
| `gamepad` | each level write of a report, all at one time | the whole `ui_in` byte written (base ORed with the level) |
| `rst_n` | each reset pulse | 0 when reset is asserted, 1 when released |
| `next` | the duration timer expires (only with `total_projects` > 1) | 0 |

Events at one time are in the order the runner makes the writes. `next` is the last event: the
model stops there, and an event exactly at `until_ms` is included. The model uses the nominal waits and the nominal 2 ms reset sleep;
real calls (enabling a project, starting the PWM clock, each report) take time that moves every
later event and the duration timer by a small, unspecified amount. The model also counts the
duration timer from time 0, not from the end of the first run of the steps.

## Errors

- An invalid file is never shown partly: every problem is logged, the demo does not run, and the
  board shows an error pattern (42-43, 658-670). A file that cannot be read gives `cannot read
  PATH`, one that is not JSON gives `not valid JSON: ...` (336-343).
- The error display (428-508) enables the factory-test design (which mirrors `ui_in` to
  `uo_out` while held in reset), takes the RP's control of the pins, stops the clock, asserts
  reset, and every 250 ms toggles `ui_in` between 0x79 (an "E" on the 7-segment display) and 0,
  blinking the board LED with it.
- If enabling an entry fails or raises while the demo runs, the demo stops and shows the same
  error display (706-716). An exception in a timer callback is logged and, for the duration timer,
  leaves the demo as it was (755-761); one in the step timer is logged and the steps stop where
  they were (616-620).
- Enabling a project by hand stops the demo (772-778).

## Differences between the board's validator and the configurator's

`validate.validate` follows the board (`demo.py` 282-329); the board has the final word. The
configurator's `demoFile.ts` differs as follows; its own header names the first.

1. Floats. `JSON.parse` turns `1.0` and `1e3` into integers, so the configurator accepts them;
   the board keeps floats and rejects them (`got 1.0`, `got 1000.0`). Integers beyond 2^53 and
   floats such as `1e22` likewise: the configurator sees one integer, Python sees a float or an
   exact big integer, and the printed values differ.
2. `NaN`, `Infinity`: Python's JSON reader accepts them (and validation then rejects them,
   `got nan`); `JSON.parse` calls the file invalid JSON.
3. Printing nested values: the configurator's `pyRepr` escapes only newline, tab, carriage return,
   backslash and quotes in strings; Python also escapes other control characters
   (`'\x01'`) and unprintable characters. It also prints an object's keys in JavaScript order,
   where keys that look like integers come first; Python keeps file order.
4. Duplicate keys: both take the last value. This repository's `parse()` rejects them instead,
   as `not valid JSON: ...`. That is stricter than the board, by choice: the configurator never
   writes duplicate keys, so a file with them was not made by it and a firmware need not choose.
   Likewise `gamepad_bits` raises for more than two controllers, which the board's word code
   would not (the validator has already limited a file to two).
5. Text that is not JSON: the detail after `not valid JSON: ` is each parser's own.
6. The configurator can check a lookup (unknown project, not marked safe); `validate()` here has
   no chip index and does not. The messages for that are in the configurator's `lookup` fixture,
   used here only as a valid file.

No other difference of logic or wording was found in the checks themselves.

## The harness firmware

[`firmware/runner.c`](../firmware/runner.c) is this runner in C and
[`firmware/demo_parse.c`](../firmware/demo_parse.c) is `validate.validate`, with the same
messages; [`tests/test_firmware_host.py`](../tests/test_firmware_host.py) holds both to the
model and to the vectors in [`tests/data/demo/`](../tests/data/demo). Where they differ:

1. Enabling (open question 1): the slot powers up in reset, and the firmware releases `rst_n`
   once, after the selection's writes (`state ... rst_n=1 source=enable`).
2. A reset cut by the duration timer (open question 3) is still released, at its own time, unless
   the next entry asserts reset first: a design is never left held in reset. The model does
   not show that release.
3. Firmware limits: a file that passes the validator can be more than the firmware holds:
   JSON tokens, projects, steps, nesting, digits of a number, or a value beyond 32 bits. The
   limits and their `firmware limit: ...` messages are defined once, in
   [`tt_harness/soc/limits.py`](../tt_harness/soc/limits.py), generated into the firmware
   as `firmware_limits.h`, and checked at build time with the same text the firmware prints
   at boot. Messages that do not fit `DEMO_ERRORS_TEXT` bytes are left out and counted in
   a last message; later ones that fit are kept.
4. Unprintable characters in a printed string: the firmware's `str.isprintable` is a table
   generated at build time from `str.isprintable()` of every code point in the build's
   Python (`py_printable.h`, which names that Python and its Unicode version), and
   [`tests/test_firmware_host.py`](../tests/test_firmware_host.py) compares the two for all
   of U+0000..U+10FFFF. A board whose Python has another Unicode version may print a code
   point assigned in one version and not the other differently.
5. The error display: the board enables its factory-test design to show the pattern on the
   7-segment display; the harness holds the chosen project disabled and in reset and writes
   the pattern to `ui_in` ([`docs/registers.md`](registers.md), "The error pattern").

## Not covered here

The harness firmware does not need any of these (demo.py 395-425 and nearby):

- autostart: building the runner and starting it at boot;
- precedence of `/demo.json` over the bundled `/shuttles/<shuttle>.demo.json`;
- the `demo = yes` switch in config.ini, and the project reset button held at power-up that
  flips it for one boot;
- the REPL helpers `start()`, `stop()` and `errors`.

## Open questions

1. Does enabling a project (`design.enable()`, SDK code, not in `demo.py`) pulse reset, toggle
   `ena` for the previous design, or change `ui_in` or the clock? The model writes only `ena` 1
   at time 0, and no `rst_n` at selection.
2. Two timers due in the same millisecond (a step wait and the duration timer): the order is
   not specified. The model puts `next` last at that time, so the step's writes at that time are
   kept.
3. The duration timer firing inside the 2 ms reset sleep (or inside a report): the real timer
   callback may run in the middle. The model keeps every event at a time <= the `next` time and
   drops the rest, so a reset released after `next` is not shown.
4. `clock_hz`: the real PWM clock may round the frequency; the model reports the requested one.
5. Real report time (1 ms or 60 ms) shifts later events; the model takes zero.
