# Test manifest

A small stand-in for a [tinytapeout-vga-videos](https://github.com/mithro/tinytapeout-vga-videos)
checkout, in its layout, read by [`tt_harness/manifest.py`](../../../tt_harness/manifest.py)
in [`tests/test_manifest.py`](../../test_manifest.py), [`tests/test_cli.py`](../../test_cli.py),
[`tests/test_cli_run_compare.py`](../../test_cli_run_compare.py) and
[`tests/test_pool_run_script.py`](../../test_pool_run_script.py).

| File | What it is |
|---|---|
| [`data/targets.json`](data/targets.json) | three entries of the repository's `data/targets.json` |
| [`data/results/`](data/results/) | `result.json` files reduced to `id` and `override` |
| [`overrides/tt06/tt_um_toivoh_retro_console.yaml`](overrides/tt06/tt_um_toivoh_retro_console.yaml) | the repository's override file for that design |

Source repository: [mithro/tinytapeout-vga-videos](https://github.com/mithro/tinytapeout-vga-videos).

Source commit: not recorded. The files were added on 2026-11-23 in commit `2c5804e` of this
repository ("Add manifest loader reading the shared videos checkout") without naming the
commit of the videos repository they came from, and it could not be established
afterwards. The override file is byte for byte the one in that repository's checkout on
2026-11-28.

Licence: Apache-2.0, the licence of tinytapeout-vga-videos (its `LICENSE` and
`pyproject.toml`). The entries describe third-party Tiny Tapeout projects (repository,
commit, file names, clock); no project source is included.
