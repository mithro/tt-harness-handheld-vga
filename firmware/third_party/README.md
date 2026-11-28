# Third-party code in the firmware

| File | Upstream | Commit | Licence |
|---|---|---|---|
| [`jsmn.h`](jsmn.h) | [zserge/jsmn](https://github.com/zserge/jsmn) | [`25647e692c7906b96ffd2b05ca54c097948e879c`](https://github.com/zserge/jsmn/blob/25647e692c7906b96ffd2b05ca54c097948e879c/jsmn.h) | MIT, [`jsmn.LICENSE`](jsmn.LICENSE) |

`jsmn.h` and `jsmn.LICENSE` are the upstream `jsmn.h` and `LICENSE` at that commit (the head
of `master` on 2026-11-28, dated 2021-10-14), byte for byte:

| File | SHA-256 |
|---|---|
| `jsmn.h` | `c04533e9181e1e33baceb0f55ac449b05145bb936e8c68cc77dfe0d8277514fb` |
| `jsmn.LICENSE` | `4675b94a50d2afe811c52785463c854f1156056632cce17cc7133939eac8ed55` |

[`demo_parse.c`](../demo_parse.c) includes it with `JSMN_STRICT` and `JSMN_STATIC`. jsmn
tokenises; it does not check every rule of JSON (it accepts `[1,]`, `{"a" "b"}`, `01`),
so `demo_parse.c` checks the separators between tokens and the grammar of every number and
literal itself.

Nothing else in [`firmware/`](..) is third-party.
