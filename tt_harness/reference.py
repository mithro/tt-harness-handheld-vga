# SPDX-License-Identifier: Apache-2.0
"""Local Verilator reference dump: `uo_out` after every rising edge of the clock.

The stimulus is the one the sibling `tt-harness-fpga-vga` reference uses (and the
`tinytapeout-vga-videos` testbench before it): `ena=1` throughout; `ui_in` and `uio_in` held
at the project's values from before the first clock; `rst_n` low for the rising edges of
clocks 0 to `reset_clocks - 1` and high from then on. Byte *i* of the dump is `uo_out` after
the rising edge of clock *i*, clock 0 being the first reset clock.

Files under `out_dir`:

    obj/            Verilator build directory (`obj/Vtop`)
    reference.log   Verilator, make, compiler and simulation output
    uo_out.bin      the dump, one byte per clock, uncompressed
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

from tt_harness import tools
from tt_harness.fetch import Sources

HARNESS = Path(__file__).resolve().parent / "sim" / "reference_main.cpp"
DUMP_NAME = "uo_out.bin"
LOG_NAME = "reference.log"
BUILD_JOBS = 16  # the plan's per-build core limit
BUILD_TIMEOUT_S = 1800
_HEADER_SUFFIXES = (".vh", ".svh", ".h")

# The flags of `tinytapeout-vga-videos` `ttvga/harness/job.py` (and of the sibling reference)
# that decide what the simulation computes or whether it builds: uninitialised state and X
# assignments take Verilator's "fast" values (in practice zero), warnings do not stop the
# build, and modules and includes resolve from the source tree.
_BASE_FLAGS = (
    "--cc",
    "--exe",
    "--build",
    "--prefix",
    "Vtop",
    "-O3",
    "--x-assign",
    "fast",
    "--x-initial",
    "fast",
    "-Wno-fatal",
    "-Wno-lint",
    "-Wno-style",
    "-Wno-MULTIDRIVEN",
    "-Wno-UNOPTFLAT",
    "-CFLAGS",
    "-std=c++17",
    "-MAKEFLAGS",
    "OPT_FAST=-O2",
    "--relative-includes",
)


# The name is fixed by the task interface; it shadows Python's (weakref) ReferenceError only
# inside this module.
class ReferenceError(RuntimeError):
    """Building or running the reference simulation failed; `log` holds the full output."""

    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.log = log


def _run(cmd: list[str], log: Path, cwd: Path, timeout: float | None) -> int:
    """Run `cmd`, appending the command line and all its output to `log`.

    It runs in its own process group so that a timeout also kills the make and compiler
    processes Verilator starts.
    """
    tmp = tools.REPO / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(tmp))
    with log.open("a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            f.write(f"\ntimed out after {timeout}s\n")
            return -1
        except BaseException:
            _kill_group(proc)
            raise


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _verilate(sources: Sources, verilator: Path, obj: Path, log: Path) -> None:
    src = sources.root / "src"
    include_dirs = dict.fromkeys([src, *(f.parent for f in sources.files)])
    files = [str(f) for f in sources.files if not f.name.endswith(_HEADER_SUFFIXES)]
    flags = [*_BASE_FLAGS, "-j", str(BUILD_JOBS)]
    if sources.needs_sv:
        flags.append("-sv")
    cmd = [
        str(verilator),
        *flags,
        "--top-module",
        sources.project.top_module,
        "-Mdir",
        str(obj),
        *(f"-I{d}" for d in include_dirs),
        "-y",
        str(src),
        str(HARNESS),
        *files,
    ]
    rc = _run(cmd, log, sources.root, BUILD_TIMEOUT_S)
    text = log.read_text(errors="replace")
    if rc != 0 and ("NEEDTIMINGOPT" in text or "--timing" in text):
        # As in the videos pipeline: a design with delays or event controls is rebuilt once
        # with --timing.
        shutil.rmtree(obj)
        obj.mkdir()
        rc = _run([cmd[0], "--timing", *cmd[1:]], log, sources.root, BUILD_TIMEOUT_S)
    if rc != 0 or not (obj / "Vtop").is_file():
        text = log.read_text(errors="replace")
        errors = [line for line in text.splitlines() if line.startswith("%Error")]
        detail = f": {errors[0]}" if errors else ""
        raise ReferenceError(
            f"{sources.project.id}: Verilator build failed (exit {rc}){detail}", log
        )


def simulate(sources: Sources, clocks: int, out_dir: Path, reset_clocks: int = 10) -> Path:
    """Simulate `clocks` clocks of `sources` and return the path of `out_dir/uo_out.bin`.

    The build is redone from scratch on every call. Raises `ReferenceError` (carrying the log
    path) if the build or the run fails or the dump is not exactly `clocks` bytes; no dump is
    left behind on failure.
    """
    if clocks < 1:
        raise ValueError(f"clocks must be at least 1, not {clocks}")
    if reset_clocks < 0:
        raise ValueError(f"reset_clocks must not be negative, not {reset_clocks}")
    verilator = tools.find("verilator")
    if verilator is None:
        raise FileNotFoundError("verilator not found; run: uv run scripts/setup_toolchains.py")

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    obj = out_dir / "obj"
    log = out_dir / LOG_NAME
    dump = out_dir / DUMP_NAME
    partial = dump.with_name(dump.name + ".partial")
    for stale in (dump, partial, log):
        stale.unlink(missing_ok=True)
    if obj.exists():
        shutil.rmtree(obj)
    obj.mkdir()

    _verilate(sources, verilator, obj, log)

    project = sources.project
    cmd = [
        str(obj / "Vtop"),
        "--clocks",
        str(clocks),
        "--reset-clocks",
        str(reset_clocks),
        "--ui-in",
        str(project.ui_in),
        "--uio-in",
        str(project.uio_in),
        "--out",
        str(partial),
    ]
    rc = _run(cmd, log, out_dir, timeout=None)
    size = partial.stat().st_size if partial.is_file() else 0
    if rc != 0 or size != clocks:
        partial.unlink(missing_ok=True)
        raise ReferenceError(
            f"{project.id}: simulation failed (exit {rc}, {size} of {clocks} bytes)", log
        )
    partial.rename(dump)
    return dump
