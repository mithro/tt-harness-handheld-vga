# Demo runner test vectors

Source: the tt-demo-configurator checkout at commit 8b7c3929066f2f85c98eb5800f31db3e4fc97a64
(8b7c392), read on 25-11-2026. The runner `src/data/demo.py` there is a pinned copy of the
board firmware's `ttboard/demo.py` (branch demo-gamepad, commit c32d62a).

- `valid/`: files that must parse. `gamepad.json`, `minimal.json`, `steps-and-subtile.json` are
  the configurator's `test/fixtures/good/`. `lookup-keys.json` is the configurator's
  `test/fixtures/bad/lookup.json`: it is only bad against a chip index (unknown address, project
  not marked safe), which this repository has no copy of, so here it is a structurally valid
  file. `shipped-*.json` are the configurator's `src/data/demos/*.demo.json`. Each file is
  checked against the shuttle named inside it.
- `invalid/`: files that must be rejected, each with the exact messages in `NAME.errors.json`
  (checked against shuttle `ttsky25a`). The first seven are the configurator's
  `test/fixtures/bad/` (all but `lookup`); `float-where-integer-required`,
  `booleans-where-integer-required` and `python-repr-of-values` are added here, with
  messages written by hand from Python's `str()`/`repr()` rules.
- `traces/`: hand-written expected writes of the runner for one entry (see
  `docs/demo-runner.md`), each explained in its `comment` field.
