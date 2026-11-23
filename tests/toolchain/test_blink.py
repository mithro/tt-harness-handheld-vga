# SPDX-License-Identifier: Apache-2.0
"""Toolchain smoke test: build a blink bitstream with the openXC7 flow.

Runs yosys -> nextpnr-xilinx -> fasm2frames -> xc7frames2bit for each target
part using the tools installed by scripts/setup_toolchains.py, and skips when
they are absent.  Outputs and per-step logs go to build/toolchain/<part>/.

    uv run pytest tests/toolchain -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TOOLCHAINS = REPO / ".toolchains"
OPENXC7_BIN = TOOLCHAINS / "openxc7" / "bin"
YOSYS = TOOLCHAINS / "oss-cad-suite" / "bin" / "yosys"
CHIPDB = TOOLCHAINS / "chipdb"
PRJXRAY_DB = TOOLCHAINS / "openxc7" / "squashfs-root" / "opt" / "nextpnr-xilinx" / "external" / "prjxray-db" / "artix7"

# (part, constraints, clock in MHz, expected configuration data size in bytes)
# Sizes are the 7-series bitstream lengths from UG470 table 1-1, in bytes.
TARGETS = [
    pytest.param("xc7a35tcsg324-1", "arty_a7_35t.xdc", 100.0, 17_536_096 // 8, id="arty-a7-35t"),
    pytest.param("xc7a100tcsg324-1", "gamebub.xdc", 50.0, 30_606_304 // 8, id="gamebub"),
]

SYNC_WORD = bytes.fromhex("aa995566")


def run_step(name: str, cmd: list[str], build: Path, stdout_to: Path | None = None) -> str:
    """Run one flow step, keeping its full output in build/<name>.log."""
    scratch = REPO / "tmp"
    scratch.mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(scratch))
    logfile = build / f"{name}.log"
    with open(logfile, "w") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        if stdout_to is None:
            result = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, cwd=build)
        else:
            with open(stdout_to, "wb") as out:
                result = subprocess.run(cmd, stdout=out, stderr=lf, env=env, cwd=build)
    text = logfile.read_text()
    assert result.returncode == 0, f"{name} failed (exit {result.returncode}); see {logfile}\n{text[-2000:]}"
    return text


@pytest.mark.parametrize(("part", "xdc", "clock_mhz", "config_bytes"), TARGETS)
def test_blink_bitstream(part: str, xdc: str, clock_mhz: float, config_bytes: int) -> None:
    if not TOOLCHAINS.is_dir():
        pytest.skip(".toolchains/ is absent; run: uv run scripts/setup_toolchains.py")
    chipdb = CHIPDB / f"{part}.bin"
    required = [YOSYS, OPENXC7_BIN / "nextpnr-xilinx", chipdb, PRJXRAY_DB / part / "part.yaml"]
    missing = [str(p.relative_to(REPO)) for p in required if not p.exists()]
    if missing:
        pytest.skip(f"toolchain incomplete, missing: {', '.join(missing)}")

    build = REPO / "build" / "toolchain" / part
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    run_step(
        "1-yosys",
        [str(YOSYS), "-p", "synth_xilinx -flatten -abc9 -arch xc7 -top blink; write_json blink.json", str(HERE / "blink.v")],
        build,
    )
    pnr_log = run_step(
        "2-nextpnr",
        [
            str(OPENXC7_BIN / "nextpnr-xilinx"),
            "--chipdb", str(chipdb),
            "--xdc", str(HERE / xdc),
            # nextpnr-xilinx 0.8.2 accepts create_clock in the XDC but does not
            # carry it through the IBUF/BUFG to the clock net, so the target
            # is also given with --freq.
            "--freq", str(clock_mhz),
            "--json", "blink.json",
            "--fasm", "blink.fasm",
        ],
        build,
    )
    run_step(
        "3-fasm2frames",
        [str(OPENXC7_BIN / "fasm2frames"), "--part", part, "--db-root", str(PRJXRAY_DB), "blink.fasm"],
        build,
        stdout_to=build / "blink.frames",
    )
    run_step(
        "4-xc7frames2bit",
        [
            str(OPENXC7_BIN / "xc7frames2bit"),
            "--part_file", str(PRJXRAY_DB / part / "part.yaml"),
            "--part_name", part,
            "--frm_file", "blink.frames",
            "--output_file", "blink.bit",
        ],
        build,
    )

    # Timing: nextpnr reports the final result for the constrained clock last.
    fmax = re.findall(r"Max frequency for clock\s+'[^']*':\s+([\d.]+) MHz \((PASS|FAIL) at ([\d.]+) MHz\)", pnr_log)
    assert fmax, "nextpnr-xilinx reported no clock frequency"
    achieved, verdict, target = fmax[-1]
    assert float(target) == pytest.approx(clock_mhz), f"clock target not applied (nextpnr used {target} MHz)"
    assert verdict == "PASS", f"timing not met: {achieved} MHz against {target} MHz"

    # Bitstream: a .bit header followed by the full-device configuration data.
    bit = (build / "blink.bit").read_bytes()
    sync = bit.find(SYNC_WORD)
    assert 0 < sync < 512, "no configuration sync word near the start of the .bit file"
    assert part.removeprefix("xc").replace("-1", "").encode() in bit[:sync], "part name missing from .bit header"
    assert abs(len(bit) - config_bytes) < 1024, f"unexpected bitstream size {len(bit)} (configuration is {config_bytes})"
    print(f"\n{part}: {len(bit)} byte bitstream, {achieved} MHz achieved against {target} MHz")
