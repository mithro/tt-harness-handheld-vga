# SPDX-License-Identifier: Apache-2.0
"""The demo build's SoC with a real project, in Verilator, its UART captured.

`run(sources, demo_json, until, max_cycles)` builds `HarnessSoC` on the LiteX simulation
platform (`platform()`) with a simulation slot (`ProjectSlot(sim_clock=True)`: no MMCM, the
bench drives the project clock), compiles the firmware (`firmware/`, in the ROM:
`tt_harness.soc.firmware`), or with `with_firmware=False` LiteX's BIOS, with the RISC-V
GCC, generates the Verilog
(with VexRiscv's), verilates it with the project's sources and the harness
`tt_harness/sim/soc_main.cpp`, and runs it. The harness drives the 50 MHz system clock and the
project clock at `clock_hz` of `demo.json` (not at all for 0), decodes the UART at 115200 baud
and streams the bytes; `until` is called with the text so far (decoded as UTF-8, bad bytes
replaced) after every chunk and the simulation stops as soon as it returns True, or after
`max_cycles` system cycles.

LiteX's own simulation build (`SimPlatform.build`) is not used: its C++ modules need
libevent and json-c, and its UART is a stream model, not the serial line.

Files under `out_dir`:

    litex/            the Builder's output: software/ (the firmware or the BIOS, the
                      generated headers, with the firmware's `demo_json.h` and
                      `firmware_config.h`), csr.csv, gateware/ (the Verilog and the ROM and
                      RAM init files the simulation reads)
    obj/              Verilator build directory
    sim.log           Verilator, make, compiler and simulation output
    uart.bin          every byte received on the UART
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from litex.build.generic_platform import Pins, Subsignal
from litex.build.io import CRG
from litex.build.sim import SimPlatform
from migen import ClockSignal, Signal

from tt_harness import tools, verilate
from tt_harness.core.slot import ProjectSlot
from tt_harness.fetch import Sources
from tt_harness.soc import core, firmware

SYS_CLK_HZ = 50_000_000
FB_SIZE = (320, 240)
TOP = "harness_sim"
HARNESS = Path(__file__).resolve().parent.parent / "sim" / "soc_main.cpp"
RISCV_GCC = tools.TOOLCHAINS / "riscv-gcc" / "bin"  # `scripts/setup_toolchains.py` puts it here
RUN_TIMEOUT_S = 1800
EXIT_CYCLE_LIMIT = 3
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

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

    @property
    def plain(self) -> str:
        """`text` without ANSI escape sequences and carriage returns."""
        return plain(self.text)


def plain(text: str) -> str:
    """`text` without ANSI escape sequences (the BIOS colours its output) and carriage returns."""
    return _ANSI.sub("", text).replace("\r", "")


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
    """The repository's RISC-V GCC (`scripts/setup_toolchains.py`) first on PATH, for the
    BIOS build (LiteX finds it there). No other GCC is used: a missing one is an error."""
    gcc = RISCV_GCC / "riscv64-unknown-elf-gcc"
    if not (gcc.is_file() and os.access(gcc, os.X_OK)):
        raise FileNotFoundError(f"no RISC-V GCC at {gcc}: run scripts/setup_toolchains.py")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{RISCV_GCC}{os.pathsep}{old}"
    try:
        yield
    finally:
        os.environ["PATH"] = old


def _generate(
    soc,
    litex_dir: Path,
    *,
    with_firmware: bool,
    fb_probes: Sequence[int],
    boot_demo_json: bytes | None,
) -> Path:
    """Compile the firmware (or the BIOS) into the ROM and write the Verilog; return the
    gateware directory."""
    from litex.soc.integration.builder import Builder

    options = {
        "output_dir": str(litex_dir),
        "compile_gateware": False,
        "build_bundle": False,
    }
    if with_firmware:
        builder = firmware.FirmwareBuilder(
            soc, fb_probes=fb_probes, demo_json=boot_demo_json, **options
        )
    else:
        if fb_probes or boot_demo_json is not None:
            raise ValueError("fb_probes and boot_demo_json are firmware options")
        builder = Builder(soc, compile_software=True, **options)
    with _toolchain_path():
        builder.build(build=False, run=False)
    gateware = Path(builder.gateware_dir)
    fragment = soc.get_fragment()
    soc.platform.finalize(fragment)
    output = soc.platform.get_verilog(fragment, name=TOP)
    with contextlib.chdir(gateware):  # the memory init files are written to the cwd
        output.write(f"{TOP}.v")
    return gateware


def run(
    sources: Sources,
    demo_json: bytes,
    until: Callable[[str], bool],
    max_cycles: int,
    *,
    out_dir: Path | None = None,
    timeout_s: float = RUN_TIMEOUT_S,
    with_firmware: bool = True,
    fb_probes: Sequence[int] = (),
    boot_demo_json: bytes | None = None,
) -> SimResult:
    """Simulate the SoC with `sources`' project; see the module docstring.

    `with_firmware=False` boots LiteX's BIOS instead of the firmware. `fb_probes`: the
    framebuffer pixels (`y * width + x`) the firmware reports in its `fb` console lines.
    `boot_demo_json`, for tests of the firmware's check at boot: the bytes embedded in the
    firmware instead of `demo_json` (which the build still validates).

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
    gateware = _generate(
        soc,
        out_dir / "litex",
        with_firmware=with_firmware,
        fb_probes=fb_probes,
        boot_demo_json=boot_demo_json,
    )
    extra = [Path(f) for f, *_ in soc.platform.sources if str(f).endswith(".v")]
    extra.append(gateware / f"{TOP}.v")
    try:
        binary = verilate.build(sources, TOP, HARNESS, extra, out_dir / "obj", log)
    except verilate.ToolError as e:
        raise SimError(e.message, log) from e

    clock_hz = soc.demo_entry.clock_hz
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
    # The UART bytes come on a pipe of their own: Verilator prints its messages on stdout.
    uart_r, uart_w = os.pipe()
    cmd += ["--uart-fd", str(uart_w)]
    with log.open("a") as err, os.fdopen(uart_r, "rb", buffering=0) as uart:
        err.write("$ " + " ".join(cmd) + "\n")
        err.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=gateware,
                stdout=err,
                stderr=err,
                pass_fds=(uart_w,),
                env=dict(os.environ, TMPDIR=str(verilate.scratch_dir())),
                start_new_session=True,
            )
        finally:
            os.close(uart_w)
        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            while True:
                left = deadline - time.monotonic()
                ready, _, _ = select.select([uart], [], [], max(left, 0))
                if not ready:
                    timed_out = True
                    break
                chunk = os.read(uart.fileno(), 4096)
                if not chunk:
                    break
                received += chunk
                if until(received.decode("utf-8", errors="replace")):
                    reached = True
                    break
        finally:
            if proc.poll() is None:
                verilate.kill_group(proc)
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
