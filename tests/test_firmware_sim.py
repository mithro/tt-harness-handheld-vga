# SPDX-License-Identifier: Apache-2.0
"""The firmware on the whole SoC in Verilator (`tt_harness.soc.sim`), with real designs of the
pool (`docs/test-designs.md`): VexRiscv runs `firmware/` from the ROM, the UART console is
decoded, and the tests read its lines (docs/registers.md, "Console").

- `test_sim_boots_runs_dvd_screensaver_and_reports_mode`: `tt07/tt_um_tinytapeout_dvd_screensaver`
  is selected (ena, clock, ui_in, then the release of reset), its clock runs, and the mode
  the firmware classifies from the measured counters is 640x480@60 with the capture
  settings `tt_harness.core.vga.classify` gives for the same measurements.
- `test_sim_invalid_demo_is_loud_and_project_stays_disabled`: an invalid demo.json in the
  image (the gamepad vector of `tests/data/demo/invalid/`) prints every message of the
  vector, exactly, and the project is never enabled: in reset, clock stopped, ui_in
  blinking the seven-segment error pattern.
- `test_sim_gamepad_step_reaches_the_design`: `ttsky25a/tt_um_gamepad_pmod_demo` draws a
  white glyph for every button and a green one for a held button; framebuffer pixels read
  back through the CSR window (`fb` lines) show the A glyph white before the step that
  presses A and green after it, the B glyph white throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tt_harness import pool, tools
from tt_harness.core import vga
from tt_harness.soc import sim

DATA = Path(__file__).resolve().parent / "data" / "demo"
DVD = "tt07/tt_um_tinytapeout_dvd_screensaver"
GAMEPAD = "ttsky25a/tt_um_gamepad_pmod_demo"
WHITE = vga.pack(3, 3, 3)
GREEN = vga.pack(0, 3, 0)
FB_WIDTH = sim.FB_SIZE[0]

pytestmark = [
    pytest.mark.toolchain,
    pytest.mark.xdist_group("work-sources"),  # fetches into the shared work/sources cache
    pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed"),
    pytest.mark.skipif(
        not (sim.RISCV_GCC / "riscv64-unknown-elf-gcc").is_file(), reason="no RISC-V GCC"
    ),
]


def sources(project_id: str):
    try:
        return pool.load_sources(project_id)
    except (OSError, ValueError, KeyError, RuntimeError) as e:  # no checkout or no network
        pytest.skip(f"cannot load sources of {project_id}: {e}")


def demo(project, **entry) -> bytes:
    one = {
        "address": project.address,
        "subtile": 0,
        "duration_ms": 15000,
        "clock_hz": project.clock_hz,
        "ui_in": 0,
        "steps": [],
    }
    one.update(entry)
    return json.dumps({"version": 1, "shuttle": project.shuttle, "projects": [one]}).encode()


def lines(text: str) -> list[str]:
    return sim.plain(text).splitlines()


def fields(line: str) -> dict[str, str]:
    """The `key=value` fields of a console line (after its first word)."""
    return dict(f.split("=", 1) for f in line.split()[1:] if "=" in f)


def complete(text: str) -> list[str]:
    """The lines ended by a newline (the last may still be arriving)."""
    return lines(text[: text.rfind("\n") + 1])


def test_sim_boots_runs_dvd_screensaver_and_reports_mode(tmp_path: Path) -> None:
    src = sources(DVD)
    project = src.project
    result = sim.run(
        src,
        demo(project),
        until=lambda text: any(
            ln.startswith("mode ") and "status=ok" in ln for ln in complete(text)
        ),
        max_cycles=30_000_000,
        out_dir=tmp_path,
    )
    assert result.reached_until, result.text[-3000:]
    out = complete(result.text)
    assert out[0].startswith(f"boot project={project.id} sys_hz=50000000 clock_hz=25175000")
    assert fields(out[0])["fb_width"] == "320" and fields(out[0])["fb_height"] == "240"
    assert out[1] == "demo ok projects=1 steps=0"
    # the selection, then the release of reset (what enabling a project does)
    states = [ln for ln in out if ln.startswith("state ")]
    t0 = fields(states[0])["t_ms"]
    assert states[:5] == [
        f"state t_ms={t0} entry=0 address={project.address} subtile=0",
        f"state t_ms={t0} ena=1",
        f"state t_ms={t0} clock_hz=25175000",
        f"state t_ms={t0} ui_in=0",
        f"state t_ms={t0} rst_n=1 source=enable",
    ]
    assert len(states) == 5, "no steps: nothing else is ever written"
    clock = [fields(ln) for ln in out if ln.startswith("clock ")]
    assert clock and clock[-1]["running"] == "1"
    assert (clock[-1]["achieved_num"], clock[-1]["achieved_den"]) == ("25175000", "1")
    mode = [fields(ln) for ln in out if ln.startswith("mode ")][-1]
    assert mode["status"] == "ok" and mode["name"] == "640x480@60"
    m = {k: int(mode[k]) for k in ("clocks_per_line", "lines_per_frame", "hsync_width")}
    assert (m["clocks_per_line"], m["lines_per_frame"]) == (800, 525)
    want = vga.classify(
        m["clocks_per_line"],
        m["lines_per_frame"],
        m["hsync_width"],
        int(mode["vsync_lines"]),
        25_175_000,
    )
    assert want is not None
    assert want.name == f"640x480@{mode['refresh_hz']}Hz"
    assert (int(mode["divider"]), int(mode["h_offset"]), int(mode["v_offset"])) == (
        want.divider,
        want.h_offset,
        want.v_offset,
    )
    assert (mode["h_step"], mode["v_step"]) == ("2", "2")  # 640x480 into 320x240
    # the design drives its syncs high in the pulse (src/vga_sync_generator.v)
    assert (mode["hsync_positive"], mode["vsync_positive"]) == ("1", "1")


def test_sim_invalid_demo_is_loud_and_project_stays_disabled(tmp_path: Path) -> None:
    src = sources(GAMEPAD)
    project = src.project
    invalid = DATA / "invalid" / "gamepad.json"
    expected = json.loads(invalid.with_suffix(".errors.json").read_text())
    assert project.shuttle == "ttsky25a"  # the shuttle the vectors are checked against
    result = sim.run(
        src,
        demo(project),  # the build validates this one
        until=lambda text: sum(ln.startswith("error display ") for ln in complete(text)) >= 2,
        max_cycles=40_000_000,
        out_dir=tmp_path,
        boot_demo_json=invalid.read_bytes(),  # the image holds this one
    )
    assert result.reached_until, result.text[-3000:]
    out = complete(result.text)
    assert out[0].startswith(f"boot project={project.id} ")
    assert out[1] == f"demo error count={len(expected)}"
    messages = [ln.removeprefix("error msg=") for ln in out if ln.startswith("error msg=")]
    assert messages == expected  # none of these holds a character the console escapes
    assert not [ln for ln in out if ln.startswith(("state ", "demo ok", "mode ", "clock "))]
    display = [fields(ln) for ln in out if ln.startswith("error display ")]
    for d in display:
        assert (d["ena"], d["rst_n"], d["clock_enable"]) == ("0", "0", "0")
    assert [d["ui_in"] for d in display[:2]] == [str(0x79), "0"]
    assert int(display[1]["t_ms"]) - int(display[0]["t_ms"]) in (250, 251)


def _addr(x: int, y: int) -> int:
    return y * FB_WIDTH + x


# Pixels of the design's glyphs (640x480, drawn 4x: 32x32 each) in the 320x240 buffer: the
# capture keeps every second pixel and row, row y being the design's line 2y + 1.
A_GLYPH = (_addr(288, 126), _addr(290, 127))  # the A glyph at (560, 240), its row 3
B_GLYPH = (_addr(262, 154),)  # the B glyph at (512, 296), its row 3
PRESS_MS = 150


def test_sim_gamepad_step_reaches_the_design(tmp_path: Path) -> None:
    src = sources(GAMEPAD)
    project = src.project
    assert "gamepad" in project.pmods
    steps = [
        {"gamepad": [{"buttons": []}]},  # a controller, nothing held
        {"wait_ms": PRESS_MS},
        {"gamepad": [{"buttons": ["a"]}]},
        {"wait_ms": 100_000},
    ]

    def fb_lines(text: str) -> list[dict[str, str]]:
        return [fields(ln) for ln in complete(text) if ln.startswith("fb ")]

    def start(text: str) -> int | None:
        sel = [ln for ln in complete(text) if ln.startswith("state ") and " entry=" in ln]
        return int(fields(sel[0])["t_ms"]) if sel else None

    def until(text: str) -> bool:
        t0 = start(text)
        return t0 is not None and any(int(f["t_ms"]) >= t0 + PRESS_MS + 100 for f in fb_lines(text))

    result = sim.run(
        src,
        demo(project, steps=steps),
        until=until,
        max_cycles=60_000_000,
        out_dir=tmp_path,
        fb_probes=A_GLYPH + B_GLYPH,
    )
    assert result.reached_until, result.text[-3000:]
    out = complete(result.text)
    t0 = start(result.text)
    states = [ln.split(" ", 2)[2] for ln in out if ln.startswith("state ")]
    assert states[:6] == [
        f"entry=0 address={project.address} subtile=0",
        "ena=1",
        "clock_hz=25175000",
        "ui_in=0",
        "gamepad=0x000fff",  # player 1 present, nothing held; no player 2
        "rst_n=1 source=enable",
    ]
    assert f"state t_ms={t0 + PRESS_MS} gamepad=0x100fff" in out  # a: bit 8 of player 1
    mode = [fields(ln) for ln in out if ln.startswith("mode ")]
    assert mode and mode[-1]["status"] == "ok" and mode[-1]["name"] == "640x480@60"

    def pixels(f: dict[str, str]) -> dict[int, int]:
        return {int(a): int(v, 16) for a, v in (p.split(":") for p in f["pixels"].split(","))}

    # Sample only once two whole frames have been captured since the event: after the
    # capture is programmed (the `mode ... status=ok` line) and after the press.
    fb = fb_lines(result.text)
    mode_ok = int(mode[-1]["t_ms"])
    press = t0 + PRESS_MS

    def settled(since: int) -> list[dict[str, str]]:
        later = [f for f in fb if int(f["t_ms"]) > since]
        assert later, f"no fb line after t_ms={since}"
        first = int(later[0]["frames"])
        return [f for f in later if int(f["frames"]) >= first + 2]

    before = [pixels(f) for f in settled(mode_ok) if int(f["t_ms"]) < press]
    after = [pixels(f) for f in settled(press)]
    assert before and after
    for p in before:
        assert [p[a] for a in A_GLYPH + B_GLYPH] == [WHITE] * 3, p
    for p in after:
        assert [p[a] for a in A_GLYPH] == [GREEN] * 2, p
        assert [p[a] for a in B_GLYPH] == [WHITE], p
