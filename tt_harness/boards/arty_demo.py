# SPDX-License-Identifier: Apache-2.0
"""Digilent Arty A7-35T board layer for the demo build: `HarnessSoC` with a real project.

Pins and the parts are plan 1's Arty layer (`tt_harness.boards.arty`, from litex-boards'
`digilent_arty` platform): the 100 MHz oscillator `clk100` (E3) and the USB UART `serial`
(TX D10, RX A9). Nothing else is used: the demo build has no screen and no sound, and its
gamepad is the emulator inside the slot.

Clocks (`ArtyCrg`):

- `osc`: `clk100` through a BUFG (`OSC_BUFG`), 100 MHz. It clocks one flip-flop, the divider.
- `sys`: the divider's output (`SYS_DIVIDER`, a flip-flop that toggles on every `osc` edge)
  through a BUFG (`SYS_BUFG`): 50 MHz, the SoC's clock. A divider rather than a PLL because a
  7-series PLL with its feedback wired directly, as LiteX's `S7PLL` has it, would meet the
  same nextpnr-xilinx 0.8.2 packer rule that ties an MMCM's feedback to VCC
  (`tt_harness.mmcm_fasm`), and the build could not show that it did not; a flip-flop and
  a BUFG need no such trust. Reset: `sys` is held in reset for 15 of its clocks after
  configuration by a counter in the reset-less domain `por` on the same clock.
- `project`: `ProjectSlot`'s `ProjectClock` (MMCME2_ADV and BUFGCE) from `clk100` at
  `clock_hz` of `demo.json`; asynchronous to `sys`.

The divider register carries the Verilog attribute `(CLOCK_ATTR, "sys")`, so the Vivado
constraints find it without relying on a generated name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from litex.build.xilinx.vivado import _build_xdc
from litex_boards.platforms import digilent_arty
from migen import ClockDomain, If, Instance, Module, Signal

from tt_harness.boards import arty
from tt_harness.core import cdc
from tt_harness.core.slot import ProjectSlot
from tt_harness.manifest import Project
from tt_harness.soc import core, firmware

NAME = arty.NAME
PARTS = arty.PARTS
TOP = "arty_demo"
OSC_PORT = arty.SYS_CLK_PORT  # "clk100"
OSC_HZ = 100_000_000
SYS_CLK_FREQ = 50_000_000
FB_SIZE = (320, 240)  # a quarter of 640x480
POR_CLOCKS = 15
OSC_BUFG = "osc_bufg"
SYS_BUFG = "sys_bufg"
CLOCK_ATTR = "tt_clock"
SYS_DIVIDER = "sys_clk_div"
# Vivado only: configuration bank voltage (bank 0 is 3.3 V on the Arty), which the LiteX
# platform does not set (DRC CFGBVS-1 otherwise).
VIVADO_CONFIG = (
    "set_property CFGBVS VCCO [current_design]",
    "set_property CONFIG_VOLTAGE 3.3 [current_design]",
)


def platform(toolchain: str):
    """litex-boards' Arty A7-35T platform for `toolchain` ("vivado" or "openxc7")."""
    if toolchain not in PARTS:
        raise ValueError(f"unknown toolchain {toolchain!r}; known: {', '.join(PARTS)}")
    return digilent_arty.Platform(variant="a7-35", toolchain=toolchain)


class ArtyCrg(Module):
    """`osc` (100 MHz), `sys` (50 MHz, divided) and `por`; see the module docstring."""

    def __init__(self, clk100: Signal) -> None:
        self.clock_domains.cd_osc = ClockDomain("osc", reset_less=True)
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clock_domains.cd_por = ClockDomain("por", reset_less=True)
        self.specials += Instance("BUFG", name=OSC_BUFG, i_I=clk100, o_O=self.cd_osc.clk)
        self.divider = divider = Signal(name_override=SYS_DIVIDER, reset_less=True)
        divider.attr.add((CLOCK_ATTR, "sys"))
        self.sync.osc += divider.eq(~divider)
        self.specials += Instance("BUFG", name=SYS_BUFG, i_I=divider, o_O=self.cd_sys.clk)
        count = Signal(max=POR_CLOCKS + 1, reset=POR_CLOCKS)
        self.comb += self.cd_por.clk.eq(self.cd_sys.clk)
        self.sync.por += If(count != 0, count.eq(count - 1))
        self.comb += self.cd_sys.rst.eq(count != 0)


def make_soc(plat, project: Project, demo_json: bytes) -> core.HarnessSoC:
    """`HarnessSoC` for `project` on `plat`, with the CRG and the slot at `clock_hz`."""
    entry = core.demo_entry(demo_json, project.shuttle)
    if project.address is not None and entry.address != project.address:
        raise ValueError(
            f"demo.json is for address {entry.address}; {project.id} is at {project.address}"
        )
    clk100 = plat.request(OSC_PORT)
    slot = ProjectSlot(project.top_module, clk100, OSC_HZ, entry.clock_hz, platform=plat)
    soc = core.HarnessSoC(
        plat,
        SYS_CLK_FREQ,
        slot,
        FB_SIZE,
        demo_json,
        project.id,
        with_audio=False,
        with_readback=True,
        pmods=project.pmods,
        reference_ui_in=project.ui_in,
    )
    soc.crg = ArtyCrg(clk100)
    return soc


@dataclass(frozen=True)
class Generated:
    """The files of a generated demo build and the names the flows need."""

    top_v: Path  # the SoC and board top, Verilog
    sources: tuple[Path, ...]  # other harness Verilog (the CPU), read after the top
    gateware: Path  # the directory of `top_v` and the memory init files it reads
    pins_xdc: Path  # pins and IO standards (and, for Vivado, the configuration voltage)
    soc: core.HarnessSoC
    project_clock_net: str  # the net on the project's clock pin: the BUFGCE output
    firmware_image: Path


def generate(project: Project, demo_json: bytes, toolchain: str, out_dir: Path) -> Generated:
    """Compile the firmware into the ROM and write the Verilog and pin constraints.

    LiteX's builder writes the Verilog (with `cdc.special_overrides()`, so every
    synchroniser is marked) and the memory init files under `out_dir/litex/gateware/`; its
    own constraint files and scripts there are deleted, because their blanket false paths
    to every `MultiReg` are exactly what the demo build must not have. `pins.xdc` holds the
    pins and IO standards only; the flows add the clocks and the crossings.
    """
    out_dir = Path(out_dir).resolve()
    plat = platform(toolchain)
    soc = make_soc(plat, project, demo_json)
    builder = firmware.FirmwareBuilder(
        soc,
        output_dir=str(out_dir / "litex"),
        compile_gateware=False,
        build_bundle=False,
    )
    vns = builder.build(run=False, build_name=TOP, special_overrides=cdc.special_overrides())
    gateware = Path(builder.gateware_dir)
    for path in gateware.iterdir():
        if path.suffix in (".xdc", ".tcl", ".sh") or path.name == "Makefile":
            path.unlink()
    top_v = gateware / f"{TOP}.v"
    lines = [_build_xdc(plat.toolchain.named_sc, [])]
    if toolchain == "vivado":
        lines += VIVADO_CONFIG
    pins = out_dir / "pins.xdc"
    pins.write_text("\n".join(lines) + "\n")
    sources = tuple(
        Path(f).resolve()
        for f, *_ in plat.sources
        if str(f).endswith(".v") and Path(f).name != top_v.name
    )
    return Generated(
        top_v=top_v,
        sources=sources,
        gateware=gateware,
        pins_xdc=pins,
        soc=soc,
        project_clock_net=vns.get_name(soc.core.slot.clock.clk),
        firmware_image=builder.image,
    )
