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

from pathlib import Path

from tt_harness import verilate
from tt_harness.fetch import Sources

HARNESS = Path(__file__).resolve().parent / "sim" / "reference_main.cpp"
DUMP_NAME = "uo_out.bin"
LOG_NAME = "reference.log"

_run = verilate.run


# The name is fixed by the task interface; it shadows Python's (weakref) ReferenceError only
# inside this module.
class ReferenceError(RuntimeError):
    """Building or running the reference simulation failed; `log` holds the full output."""

    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.log = log


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

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    obj = out_dir / "obj"
    log = out_dir / LOG_NAME
    dump = out_dir / DUMP_NAME
    partial = dump.with_name(dump.name + ".partial")
    for stale in (dump, partial, log):
        stale.unlink(missing_ok=True)

    project = sources.project
    try:
        binary = verilate.build(sources, project.top_module, HARNESS, [], obj, log)
    except verilate.ToolError as e:
        raise ReferenceError(e.message, log) from e

    cmd = [
        str(binary),
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
    rc = verilate.run(cmd, log, out_dir, timeout=None)
    size = partial.stat().st_size if partial.is_file() else 0
    if rc != 0 or size != clocks:
        partial.unlink(missing_ok=True)
        raise ReferenceError(
            f"{project.id}: simulation failed (exit {rc}, {size} of {clocks} bytes)", log
        )
    partial.rename(dump)
    return dump
