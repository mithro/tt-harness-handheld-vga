# SPDX-License-Identifier: Apache-2.0
"""The demo build's SoC with a real project, in Verilator, its UART captured.

`run(sources, demo_json, until, max_cycles)` builds `HarnessSoC` on the LiteX simulation
platform (`platform()`) with a simulation slot (`ProjectSlot(sim_clock=True)`: no MMCM, the
bench drives the project clock), compiles the BIOS with the RISC-V GCC, generates the Verilog
(with VexRiscv's), verilates it with the project's sources and the harness
`tt_harness/sim/soc_main.cpp`, and runs it. The harness drives the 50 MHz system clock and the
project clock at `clock_hz` of `demo.json` (not at all for 0), decodes the UART at 115200 baud
and streams the bytes; `until` is called with the text so far (decoded as UTF-8, bad bytes
replaced) after every chunk and the simulation stops as soon as it returns True, or after
`max_cycles` system cycles.

LiteX's own simulation build (`SimPlatform.build`) is not used: its C++ modules need
libevent and json-c, and its UART is a stream model, not the serial line.

Files under `out_dir`:

    litex/            the Builder's output: software/ (BIOS, generated headers, with
                      `demo_json.h` for the firmware), csr.csv, gateware/ (the Verilog and
                      the ROM and RAM init files the simulation reads)
    obj/              Verilator build directory
    sim.log           Verilator, make, compiler and simulation output
    uart.bin          every byte received on the UART
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from litex.build.generic_platform import Pins, Subsignal
from litex.build.io import CRG
from litex.build.sim import SimPlatform
from migen import ClockSignal, Signal

from tt_harness import tools, verilate
from tt_harness.core.slot import ProjectSlot
from tt_harness.fetch import Sources
from tt_harness.soc import core

SYS_CLK_HZ = 50_000_000
FB_SIZE = (320, 240)
TOP = "harness_sim"
HARNESS = Path(__file__).resolve().parent.parent / "sim" / "soc_main.cpp"
RUN_TIMEOUT_S = 1800
EXIT_CYCLE_LIMIT = 3

_IO = (
    ("sys_clk", 0, Pins(1)),
    ("sys_rst", 0, Pins(1)),
    ("serial", 0, Subsignal("tx", Pins(1)), Subsignal("rx", Pins(1))),
    ("project_clk", 0, Pins(1)),
)


class SimError(RuntimeError):
    """Building or running the SoC simulation failed; `log` holds the full output."""

    def __init__(self, message: str, log: Path) -> None:
        super().__init__(f"{message} (log: {log})")
        self.log = log


@dataclass(frozen=True)
class SimResult:
    uart: bytes  # every byte received
    reached_until: bool  # `until` returned True (else the cycle limit ended the run)
    log: Path

    @property
    def text(self) -> str:
        return self.uart.decode("utf-8", errors="replace")


def platform() -> SimPlatform:
    """The LiteX simulation platform: `sys_clk`, `sys_rst`, `serial`, `project_clk`."""
    return SimPlatform("SIM", list(_IO))


def build_soc(sources: Sources, demo_json: bytes):
    """`HarnessSoC` for `sources`' project on the simulation platform, with its CRG."""
    project = sources.project
    entry = core.demo_entry(demo_json, project.shuttle)
    if project.address is not None and entry.address != project.address:
        raise ValueError(
            f"demo.json is for address {entry.address}; {project.id} is at {project.address}"
        )
    plat = platform()
    slot = ProjectSlot(project.top_module, Signal(), 0, entry.clock_hz, sim_clock=True)
    soc = core.HarnessSoC(
        plat,
        SYS_CLK_HZ,
        slot,
        FB_SIZE,
        demo_json,
        project.id,
        with_audio=False,
        with_readback=True,
        pmods=project.pmods,
        reference_ui_in=project.ui_in,
    )
    soc.crg = CRG(plat.request("sys_clk"), rst=plat.request("sys_rst"))
    soc.comb += ClockSignal("project").eq(plat.request("project_clk"))
    return soc


@contextlib.contextmanager
def _toolchain_path():
    """The repository's RISC-V GCC on PATH, for the BIOS build (LiteX finds it there)."""
    gcc = tools.find("riscv64-unknown-elf-gcc")
    old = os.environ.get("PATH", "")
    if gcc is not None:
        os.environ["PATH"] = f"{gcc.parent}{os.pathsep}{old}"
    try:
        yield
    finally:
        os.environ["PATH"] = old


def _generate(soc, litex_dir: Path) -> Path:
    """Compile the BIOS into the ROM and write the Verilog; return the gateware directory."""
    from litex.soc.integration.builder import Builder

    builder = Builder(
        soc,
        output_dir=str(litex_dir),
        compile_software=True,
        compile_gateware=False,
        build_bundle=False,
    )
    with _toolchain_path():
        builder.build(build=False, run=False)
    generated = Path(builder.generated_dir)
    (generated / "demo_json.h").write_text(core.demo_json_header(soc.demo_json))
    gateware = Path(builder.gateware_dir)
    fragment = soc.get_fragment()
    soc.platform.finalize(fragment)
    output = soc.platform.get_verilog(fragment, name=TOP)
    output.write(str(gateware / f"{TOP}.v"))
    return gateware


def run(
    sources: Sources,
    demo_json: bytes,
    until: Callable[[str], bool],
    max_cycles: int,
    *,
    out_dir: Path | None = None,
    timeout_s: float = RUN_TIMEOUT_S,
) -> SimResult:
    """Simulate the SoC with `sources`' project; see the module docstring.

    Raises `SimError` if the build fails, the harness reports an error, or the run takes
    longer than `timeout_s` of wall time without `until` or the cycle limit ending it.
    """
    if out_dir is None:
        out_dir = tools.REPO / "build" / "soc-sim" / sources.project.macro
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "sim.log"
    uart_file = out_dir / "uart.bin"
    for stale in (log, uart_file):
        stale.unlink(missing_ok=True)

    soc = build_soc(sources, demo_json)
    gateware = _generate(soc, out_dir / "litex")
    extra = [Path(f) for f, *_ in soc.platform.sources if str(f).endswith(".v")]
    extra.append(gateware / f"{TOP}.v")
    try:
        binary = verilate.build(sources, TOP, HARNESS, extra, out_dir / "obj", log)
    except verilate.ToolError as e:
        raise SimError(e.message, log) from e

    clock_hz = core.demo_entry(demo_json, sources.project.shuttle).clock_hz
    cmd = [
        str(binary),
        "--sys-hz",
        str(SYS_CLK_HZ),
        "--project-hz",
        str(clock_hz),
        "--baud",
        str(core.UART_BAUD),
        "--max-cycles",
        str(max_cycles),
    ]
    received = bytearray()
    reached = False
    with log.open("a") as err:
        err.write("$ " + " ".join(cmd) + "\n")
        err.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=gateware,
            stdout=subprocess.PIPE,
            stderr=err,
            env=dict(os.environ, TMPDIR=str(verilate.scratch_dir())),
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            while True:
                left = deadline - time.monotonic()
                ready, _, _ = select.select([proc.stdout], [], [], max(left, 0))
                if not ready:
                    timed_out = True
                    break
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                received += chunk
                if until(received.decode("utf-8", errors="replace")):
                    reached = True
                    break
        finally:
            if proc.poll() is None:
                verilate.kill_group(proc)
            proc.stdout.close()
            proc.wait()
    uart_file.write_bytes(bytes(received))
    if timed_out:
        raise SimError(f"{sources.project.id}: no end after {timeout_s} s of wall time", log)
    if not reached and proc.returncode != EXIT_CYCLE_LIMIT:
        raise SimError(
            f"{sources.project.id}: the simulation ended with status {proc.returncode} "
            f"after {len(received)} UART bytes",
            log,
        )
    return SimResult(bytes(received), reached, log)
