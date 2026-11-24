# SPDX-License-Identifier: Apache-2.0
"""System simulation of the compare build: `CompareTop` and the project in Verilator.

There is no clock buffer in simulation, so `pclk` drives the project's `clk` directly. The
harness (`tt_harness/sim/uart_capture_main.cpp`) decodes the UART and writes the bytes it
received.

Files under `out_dir`:

    compare_top.v   the Migen-generated top
    obj/            Verilator build directory
    compare.log     Verilator, make, compiler and simulation output
    frame.bin       the bytes received on the UART
"""

from __future__ import annotations

from pathlib import Path

from tt_harness import verilate
from tt_harness.compare import recorder, top
from tt_harness.fetch import Sources

HARNESS = Path(__file__).resolve().parent.parent / "sim" / "uart_capture_main.cpp"
LOG_NAME = "compare.log"
FRAME_NAME = "frame.bin"


class CompareSimError(RuntimeError):
    """Building or running the compare simulation failed; `log` holds the full output."""

    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.log = log


def cycle_budget(clocks: int, divisor: int, settle: int) -> int:
    """A generous upper bound on the system cycles needed to send the whole frame."""
    uart = 10 * divisor * recorder.frame_length(clocks)
    stepping = (2 * (settle + 1) + 2) * clocks
    return 2 * (uart + stepping) + 100_000


def simulate(
    sources: Sources,
    clocks: int,
    out_dir: Path,
    sys_clk_freq: int,
    baud: int,
    settle: int = 2,
    max_cycles: int | None = None,
) -> bytes:
    """Run the compare build for `clocks` project clocks; return the bytes seen on `tx`.

    Raises `CompareSimError` if the build fails, the harness reports an error (including
    reaching `max_cycles` before `done`), or nothing was written.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / LOG_NAME
    frame = out_dir / FRAME_NAME
    for stale in (log, frame):
        stale.unlink(missing_ok=True)

    compare = top.CompareTop(sources.project, sys_clk_freq, baud, clocks, settle=settle)
    divisor = round(sys_clk_freq / baud)
    verilog = top.to_verilog(compare, out_dir / "compare_top.v")
    try:
        binary = verilate.build(sources, top.MODULE_NAME, HARNESS, [verilog], out_dir / "obj", log)
    except verilate.ToolError as e:
        raise CompareSimError(e.message, log) from e

    limit = max_cycles if max_cycles is not None else cycle_budget(clocks, divisor, settle)
    cmd = [
        str(binary),
        "--divisor",
        str(divisor),
        "--max-cycles",
        str(limit),
        "--out",
        str(frame),
    ]
    rc = verilate.run(cmd, log, out_dir, timeout=None)
    if rc != 0 or not frame.is_file():
        lines = [
            line
            for line in log.read_text(errors="replace").splitlines()
            if line.startswith("uart_capture:")
        ]
        detail = f": {lines[-1]}" if lines else ""
        raise CompareSimError(
            f"{sources.project.id}: compare simulation failed (exit {rc}){detail}", log
        )
    return frame.read_bytes()
