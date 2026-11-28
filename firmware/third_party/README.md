# Third-party code in the firmware

| File | Upstream | Commit | Licence | Licence file |
|---|---|---|---|---|
| [`jsmn.h`](jsmn.h) | [zserge/jsmn](https://github.com/zserge/jsmn) | [`25647e692c7906b96ffd2b05ca54c097948e879c`](https://github.com/zserge/jsmn/blob/25647e692c7906b96ffd2b05ca54c097948e879c/jsmn.h) | MIT | [`jsmn.LICENSE`](jsmn.LICENSE) |

`jsmn.h` and `jsmn.LICENSE` are the upstream `jsmn.h` and `LICENSE` at that commit (the head
of `master` on 2026-11-28, dated 2021-10-14), byte for byte:

| File | SHA-256 |
|---|---|
| `jsmn.h` | `c04533e9181e1e33baceb0f55ac449b05145bb936e8c68cc77dfe0d8277514fb` |
| `jsmn.LICENSE` | `4675b94a50d2afe811c52785463c854f1156056632cce17cc7133939eac8ed55` |

[`demo_parse.c`](../demo_parse.c) includes it with `JSMN_STATIC` only, in jsmn's default
(non-strict) mode: strict mode refuses the `NaN` and `Infinity` that Python's `json` accepts
(and the validator then reports by value) and a bare value at the end of the text. jsmn
tokenises; it does not check every rule of JSON (it accepts `[1,]`, `{"a" "b"}`, `01`), so
`demo_parse.c` checks the separators between tokens, the grammar of every number and
literal, and string escapes itself.

Nothing else in [`firmware/`](..) is third-party.
