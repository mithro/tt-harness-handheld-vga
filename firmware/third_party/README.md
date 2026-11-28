# Third-party code in the firmware

| File | Upstream | Commit | Licence | Licence file |
|---|---|---|---|---|
| [`jsmn.h`](jsmn.h) | [zserge/jsmn](https://github.com/zserge/jsmn) | [`25647e692c7906b96ffd2b05ca54c097948e879c`](https://github.com/zserge/jsmn/blob/25647e692c7906b96ffd2b05ca54c097948e879c/jsmn.h) | MIT | [`jsmn.LICENSE`](jsmn.LICENSE) |

`jsmn.LICENSE` is the upstream `LICENSE` at that commit (the head of `master` on
2026-11-28, dated 2021-10-14), byte for byte. `jsmn.h` is the upstream `jsmn.h` at that
commit with one change, marked `tt-harness` in the file: the token struct `jsmntok_t`.

| File | Upstream SHA-256 | SHA-256 here |
|---|---|---|
| `jsmn.h` | `c04533e9181e1e33baceb0f55ac449b05145bb936e8c68cc77dfe0d8277514fb` | `a719a60091733deba14e608937cbecdcde1e05a49daaa0bad2272b49c5bc9172` |
| `jsmn.LICENSE` | `4675b94a50d2afe811c52785463c854f1156056632cce17cc7133939eac8ed55` | `4675b94a50d2afe811c52785463c854f1156056632cce17cc7133939eac8ed55` |

### The token struct

Upstream's token is four `int`s, 16 bytes; here it is 8, so the parser's 2048 tokens take
16 KiB and main RAM is 32 KiB: block RAM on the Arty A7-35T is the limit
([`tt_harness/demo_build.py`](../../tt_harness/demo_build.py)).

| Field | Upstream | Here | Bound |
|---|---|---|---|
| `start` | `int` | `int : 20` | `DEMO_MAX_TEXT` (65536) |
| `end` | `int` | `int : 20` | `DEMO_MAX_TEXT` (65536) |
| `size` | `int` | `int : 12` | `DEMO_MAX_TOKENS` - 1 (2047) |
| `type` | `jsmntype_t` | `unsigned int : 4` | `JSMN_PRIMITIVE` (8) |
| `parent` | `int` | none | `JSMN_PARENT_LINKS` unset |

The position fields are signed because jsmn marks an open token with -1.
[`demo_parse.c`](../demo_parse.c) checks each bound with a `_Static_assert` and refuses a
longer text with `DEMO_MSG_TEXT` before jsmn runs.
[`tests/test_firmware_jsmn.py`](../../tests/test_firmware_jsmn.py) checks that the file with
upstream's struct put back has upstream's SHA-256.

[`demo_parse.c`](../demo_parse.c) includes it with `JSMN_STATIC` only, in jsmn's default
(non-strict) mode: strict mode refuses the `NaN` and `Infinity` that Python's `json` accepts
(and the validator then reports by value) and a bare value at the end of the text. jsmn
tokenises; it does not check every rule of JSON (it accepts `[1,]`, `{"a" "b"}`, `01`), so
`demo_parse.c` checks the separators between tokens, the grammar of every number and
literal, and string escapes itself.

Nothing else in [`firmware/`](..) is third-party.
