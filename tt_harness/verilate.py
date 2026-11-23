# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for running external tools and building Verilator simulations.

Used by the reference dump (`tt_harness.reference`), the compare-build system simulation
(`tt_harness.compare.sim`) and the FPGA builds (`tt_harness.build`).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tt_harness import tools
from tt_harness.fetch import Sources

BUILD_JOBS = 16  # the plan's per-build core limit
BUILD_TIMEOUT_S = 1800
HEADER_SUFFIXES = (".vh", ".svh", ".h")

# The flags of `tinytapeout-vga-videos` `ttvga/harness/job.py` (and of the sibling reference)
# that decide what the simulation computes or whether it builds: uninitialised state and X
# assignments take Verilator's "fast" values (in practice zero), warnings do not stop the
# build, and modules and includes resolve from the source tree.
BASE_FLAGS = (
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


class ToolError(RuntimeError):
    """An external tool failed; `log` holds its full output."""

    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.message = message
        self.log = log


def scratch_dir() -> Path:
    """`<repo>/tmp`, created if needed: the TMPDIR every subprocess is given."""
    tmp = tools.REPO / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def run(
    cmd: list[str],
    log: Path,
    cwd: Path,
    timeout: float | None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run `cmd`, appending the command line and all its output to `log`; return its status.

    TMPDIR is `<repo>/tmp`. The command runs in its own process group so that a timeout (or
    an interrupt) also kills the processes it starts, such as Verilator's make and compilers.
    Returns -1 on timeout.
    """
    full_env = dict(os.environ if env is None else env, TMPDIR=str(scratch_dir()))
    with log.open("a") as f:
        f.write("$ " + " ".join(cmd) + "\n")
        f.flush()
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=full_env, start_new_session=True
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc)
            f.write(f"\ntimed out after {timeout}s\n")
            return -1
        except BaseException:
            kill_group(proc)
            raise


def kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def project_files(sources: Sources) -> tuple[list[Path], list[Path]]:
    """The project's compilable source files and its include directories (src/ first).

    Header files (`.vh`, `.svh`, `.h`) are left out of the file list; they are reached
    through the include directories.
    """
    src = sources.root / "src"
    include_dirs = list(dict.fromkeys([src, *(f.parent for f in sources.files)]))
    files = [f for f in sources.files if not f.name.endswith(HEADER_SUFFIXES)]
    return files, include_dirs


def first_error(log: Path) -> str:
    """The first `%Error` line in a Verilator log, formatted as `: <line>`, or ''."""
    text = log.read_text(errors="replace")
    errors = [line for line in text.splitlines() if line.startswith("%Error")]
    return f": {errors[0]}" if errors else ""


def build(
    sources: Sources,
    top_module: str,
    harness: Path,
    extra_files: list[Path],
    obj: Path,
    log: Path,
) -> Path:
    """Verilate `sources` (plus `extra_files`) with `harness` as main; return `obj/Vtop`.

    `obj` is emptied first. As in the videos pipeline, a design with delays or event
    controls is rebuilt once with --timing. Raises `ToolError` if the build fails.
    """
    verilator = tools.find("verilator")
    if verilator is None:
        raise FileNotFoundError("verilator not found; run: uv run scripts/setup_toolchains.py")
    if obj.exists():
        shutil.rmtree(obj)
    obj.mkdir(parents=True)
    files, include_dirs = project_files(sources)
    flags = [*BASE_FLAGS, "-j", str(BUILD_JOBS)]
    if sources.needs_sv:
        flags.append("-sv")
    cmd = [
        str(verilator),
        *flags,
        "--top-module",
        top_module,
        "-Mdir",
        str(obj),
        *(f"-I{d}" for d in include_dirs),
        "-y",
        str(sources.root / "src"),
        str(harness),
        *(str(f) for f in extra_files),
        *(str(f) for f in files),
    ]
    rc = run(cmd, log, sources.root, BUILD_TIMEOUT_S)
    text = log.read_text(errors="replace")
    if rc != 0 and ("NEEDTIMINGOPT" in text or "--timing" in text):
        shutil.rmtree(obj)
        obj.mkdir()
        rc = run([cmd[0], "--timing", *cmd[1:]], log, sources.root, BUILD_TIMEOUT_S)
    binary = obj / "Vtop"
    if rc != 0 or not binary.is_file():
        raise ToolError(
            f"{sources.project.id}: Verilator build failed (exit {rc}){first_error(log)}", log
        )
    return binary
