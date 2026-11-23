# SPDX-License-Identifier: Apache-2.0
import json
import os
import shutil
from pathlib import Path

import pytest

from tt_harness import manifest

FIXTURE = Path(__file__).parent / "data" / "manifest"
DEFAULT_VIDEOS = "~/github/TinyTapeout/tinytapeout-vga-videos"

POOL = [
    "tt07/tt_um_tinytapeout_dvd_screensaver",
    "tt08/tt_um_tinytapeout_logo_screensaver",
    "ttihp26a/tt_um_urish_rings",
    "ttihp0p3/tt_um_algofoogle_fun_vga_clock",
    "ttihp25a/tt_um_algofoogle_vga",
    "ttsky26a/tt_um_algofoogle_dottee",
    "ttsky25a/tt_um_gamepad_pmod_demo",
    "ttihp25a/tt_um_2048_vga_game",
]


@pytest.fixture
def projects():
    return manifest.load(FIXTURE)


def test_three_fixture_projects_load_exactly(projects):
    assert sorted(projects) == [
        "tt06/tt_um_toivoh_retro_console",
        "tt07/tt_um_tinytapeout_dvd_screensaver",
        "ttihp26a/tt_um_urish_rings",
    ]
    p = projects["tt07/tt_um_tinytapeout_dvd_screensaver"]
    assert p.id == "tt07/tt_um_tinytapeout_dvd_screensaver"
    assert p.shuttle == "tt07"
    assert p.macro == "tt_um_tinytapeout_dvd_screensaver"
    assert p.repo == "https://github.com/TinyTapeout/tt07-dvd-screensaver"
    assert p.commit == "3c59c2f366595854edc1f8a9477f8304e6eaf832"
    assert p.top_module == "tt_um_tinytapeout_dvd_screensaver"
    assert p.source_files == (
        "tt_um_tinytapeout_dvd_screensaver.v",
        "bitmap_rom.v",
        "vga_sync_generator.v",
        "palette.v",
    )
    assert p.clock_hz == 25175000
    assert p.pmods == ("tiny-vga",)
    assert p.address == 130
    assert (p.ui_in, p.uio_in, p.patch) == (0, 0, None)


def test_undeclared_clock_defaults_to_reference_clock(projects):
    assert projects["ttihp26a/tt_um_urish_rings"].clock_hz == 25_175_000


def test_retro_console_has_fixed_ui_in(projects):
    assert projects["tt06/tt_um_toivoh_retro_console"].ui_in == 0x30


def test_by_address_takes_integer(projects):
    p = manifest.by_address(projects, "tt07", 130)
    assert p.macro == "tt_um_tinytapeout_dvd_screensaver"
    with pytest.raises(KeyError, match="131.*tt07"):
        manifest.by_address(projects, "tt07", 131)


def test_unknown_id_names_the_id(projects):
    with pytest.raises(KeyError, match="tt99/nope"):
        manifest.get(projects, "tt99/nope")


def test_missing_repository_mentions_env_var(tmp_path):
    with pytest.raises(FileNotFoundError, match="TT_VIDEOS_REPO"):
        manifest.load(tmp_path / "absent")
    with pytest.raises(FileNotFoundError, match="TT_VIDEOS_REPO"):
        manifest.load(tmp_path)  # exists but has no data/targets.json


def _repo_with_override(tmp_path, yaml_text, result_override=None):
    root = tmp_path / "videos"
    shutil.copytree(FIXTURE, root)
    macro = "tt_um_tinytapeout_dvd_screensaver"
    (root / "overrides/tt07").mkdir(parents=True, exist_ok=True)
    (root / f"overrides/tt07/{macro}.yaml").write_text(yaml_text)
    (root / "overrides/tt07/fix.diff").write_text("--- a/x\n+++ b/x\n")
    if result_override is not None:
        rpath = root / f"data/results/tt07/{macro}/result.json"
        rpath.write_text(json.dumps({"override": result_override}))
    return root, f"tt07/{macro}"


def test_override_sources_top_module_and_patch(tmp_path):
    root, pid = _repo_with_override(
        tmp_path,
        "sources: [a.v, b.sv]\ntop_module: tt_um_other\nclock_hz: 50000000\n"
        "ui_in: 0x80\nuio_in: 3\npatch: fix.diff\n",
        {"sources": ["a.v", "b.sv"]},
    )
    p = manifest.load(root)[pid]
    assert p.source_files == ("a.v", "b.sv")
    assert p.top_module == "tt_um_other"
    assert p.clock_hz == 50_000_000
    assert (p.ui_in, p.uio_in) == (0x80, 3)
    assert p.patch == str(root / "overrides/tt07/fix.diff")


def test_override_from_result_json_only(tmp_path):
    root, pid = _repo_with_override(tmp_path, "{}\n", {"sources": ["z.v"], "ui_in": 5})
    p = manifest.load(root)[pid]
    assert p.source_files == ("z.v",)
    assert p.ui_in == 5


def test_first_input_event_gives_ui_in_and_uio_in(tmp_path):
    root, pid = _repo_with_override(
        tmp_path, "inputs:\n  - at: 0\n    ui_in: 8\n    uio_in: 2\n  - at: 5\n    ui_in: 1\n"
    )
    p = manifest.load(root)[pid]
    assert (p.ui_in, p.uio_in) == (8, 2)


def test_empty_inputs_default_to_zero(tmp_path):
    root, pid = _repo_with_override(tmp_path, "inputs: []\n")
    assert manifest.load(root)[pid].ui_in == 0


def _real_repo() -> Path:
    return Path(os.environ.get("TT_VIDEOS_REPO", DEFAULT_VIDEOS)).expanduser()


@pytest.mark.skipif(not _real_repo().is_dir(), reason="TT_VIDEOS_REPO checkout not present")
def test_real_manifest_has_pool():
    real = manifest.load(_real_repo())
    assert len(real) >= 400
    for pid in POOL:
        assert pid in real, pid
