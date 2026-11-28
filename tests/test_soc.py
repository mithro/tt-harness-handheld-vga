# SPDX-License-Identifier: Apache-2.0
"""The LiteX SoC of the demo build (`tt_harness/soc/`): its register map, its registers on the
bus, its crossings, and the BIOS booting in Verilator.

- `test_csr_map_is_documented_and_current`: `docs/registers.md` is the generated CSR map of
  the reference configuration (`tt_harness.soc.registers`), every harness register and field
  has a description, and the addresses agree with LiteX's own `csr.csv` export.
- `test_registers_read_back`: the SoC without a CPU in Migen's simulator, a bus master in place
  of the CPU, two unrelated clocks (`tests/slot_sim.py`) and a synthetic VGA picture on
  `uo_out`: every writable register reads back what was written and reaches the block it
  controls, the build facts read as built, and the measured timing, the framebuffer, the
  seven-segment duty and an at-speed CRC are read back through the registers.
- `test_bios_boots_and_prints_banner` (`toolchain`: Verilator and the RISC-V GCC): the whole
  SoC with VexRiscv and a real project, the BIOS compiled and run, its UART decoded.
"""

from __future__ import annotations

import csv
import io
import json
import zlib
from pathlib import Path

import cdc_structure
import pytest
from litex.soc.integration import export
from litex.soc.interconnect import wishbone
from migen import ClockDomain, Module, Signal
from migen.sim import passive
from slot_sim import gate, run

from tt_harness import tools
from tt_harness.core import vga
from tt_harness.core.slot import ProjectSlot
from tt_harness.demo.validate import DemoError
from tt_harness.fetch import Sources
from tt_harness.manifest import Project
from tt_harness.soc import core, registers, sim

REPO = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
SYS_HZ = 50_000_000
CLOCK_HZ = 25_000_000


def demo(clock_hz: int = CLOCK_HZ, shuttle: str = "tt00", projects: int = 1, **entry) -> bytes:
    one = {
        "address": 1,
        "subtile": 0,
        "duration_ms": 1000,
        "clock_hz": clock_hz,
        "ui_in": 0,
        "steps": [],
    }
    one.update(entry)
    return json.dumps({"version": 1, "shuttle": shuttle, "projects": [one] * projects}).encode()


def sim_slot(clock_hz: int = CLOCK_HZ) -> ProjectSlot:
    return ProjectSlot(None, Signal(), 100_000_000, clock_hz, sim_clock=True)


def soc_for_test(**kw) -> core.HarnessSoC:
    args = {
        "platform": sim.platform(),
        "sys_clk_freq": SYS_HZ,
        "slot": sim_slot(),
        "fb_size": (4, 2),
        "demo_json": demo(),
        "project_name": "tt00/tt_um_test",
        "with_audio": False,
        "with_readback": True,
        "pmods": ("tiny-vga", "gamepad"),
        "reference_ui_in": 0x5A,
        "cpu_type": None,
        "sevenseg_window_log2": 3,
    }
    args.update(kw)
    return core.HarnessSoC(**args)


# The register map document -------------------------------------------------------------------


def test_csr_map_is_documented_and_current() -> None:
    soc = registers.reference_soc()
    doc = (REPO / "docs" / "registers.md").read_text()
    assert registers.splice(doc, registers.render(soc)) == doc, (
        "docs/registers.md is stale: run `uv run python -m tt_harness.soc.registers`"
    )
    harness = [r for r in registers.registers(soc) if r.module in core.CSR_MODULES]
    assert {r.module for r in harness} == set(core.CSR_MODULES)
    for reg in harness:
        assert reg.description, f"{reg.name} has no description"
        for field in reg.fields:
            assert field.description, f"{reg.name}.{field.name} has no description"
    # The addresses are LiteX's own, as its csr.csv export gives them.
    text = export.get_csr_csv(
        csr_regions=soc.csr_regions, constants=soc.constants, mem_regions=soc.bus.regions
    )
    rows = [r for r in csv.reader(io.StringIO(text)) if r and r[0] == "csr_register"]
    litex = {r[1]: (int(r[2], 0), int(r[3])) for r in rows}
    ours = {r.name: (r.address, r.words) for r in registers.registers(soc)}
    assert ours == litex


def test_registers_doc_states_the_firmware_rules() -> None:
    doc = (REPO / "docs" / "registers.md").read_text()
    rules = doc[: doc.index(registers.BEGIN)]
    for phrase in (
        "Reset the design after any at-speed abort",
        "Never start an at-speed check unless `clock_running`",
        "The last start wins",
        "With `clock_hz` 0 nothing waits on the project clock",
    ):
        assert phrase in rules, phrase


# Construction: the build-time checks ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"demo_json": b'{"version": 1}'}, 'missing "shuttle"'),
        ({"demo_json": b"not json"}, "not valid JSON"),
        ({"demo_json": demo(shuttle="tt07")}, '"shuttle" is "tt07" but this chip is "tt00"'),
        ({"demo_json": demo(projects=2)}, "exactly one project"),
        ({"demo_json": b"\xff"}, "not UTF-8"),
    ],
)
def test_an_invalid_demo_json_fails_the_build(kw: dict, message: str) -> None:
    with pytest.raises(DemoError) as e:
        soc_for_test(**kw)
    assert any(message in m for m in e.value.errors), e.value.errors


def test_the_slot_clock_must_be_the_demo_clock() -> None:
    with pytest.raises(ValueError, match="clock_hz 25000000.*slot.*12000000"):
        soc_for_test(slot=sim_slot(12_000_000))


def test_an_unknown_pmod_fails_the_build() -> None:
    with pytest.raises(ValueError, match="unknown Pmod 'laser'"):
        soc_for_test(pmods=("tiny-vga", "laser"))


def test_the_project_name_names_the_shuttle() -> None:
    with pytest.raises(ValueError, match="<shuttle>/<macro>"):
        soc_for_test(project_name="tt_um_test")


def test_the_achieved_clock_is_the_exact_rational_of_the_mmcm_setting() -> None:
    slot = ProjectSlot(None, Signal(), 100_000_000, 25_175_000)
    assert core.achieved_clock(slot) == (3_600_000_000, 143)  # 100 MHz x 9 / 35.75
    assert core.achieved_clock(sim_slot(25_175_000)) == (25_175_000, 1)
    assert core.achieved_clock(ProjectSlot(None, Signal(), 100_000_000, 0)) == (0, 1)


def test_the_demo_json_header_holds_the_exact_bytes() -> None:
    raw = demo() + b"\n"
    text = core.demo_json_header(raw)
    body = text[text.index("{", text.index("demo_json[]")) + 1 : text.index("};")]
    assert bytes(int(v, 0) for v in body.replace("\n", "").split(",") if v.strip()) == raw
    assert f"#define DEMO_JSON_LEN {len(raw)}" in text


# Crossings -----------------------------------------------------------------------------------

PRIMITIVES = {"MMCME2_ADV": "mmcm", "BUFG": "clock", "BUFGCE": "clock"}


class _CoreTop(Module):
    def __init__(self, with_audio: bool) -> None:
        self.clock_domains.cd_sys = ClockDomain("sys")
        self.clk_in = Signal()
        slot = ProjectSlot("tt_um_example", self.clk_in, 100_000_000, 25_175_000)
        self.submodules.core = core.HarnessCore(
            slot, (320, 240), reference_ui_in=0x5A, with_audio=with_audio, sevenseg_window_log2=19
        )

    def ports(self) -> dict:
        out = dict.fromkeys(self.core.sys_inputs(), "sys")
        out[self.clk_in] = "clock"
        out[self.core.slot.clock.reset] = "sys"
        return out


@pytest.mark.parametrize("with_audio", [False, True])
def test_the_only_crossings_of_the_core_are_the_held_buses_and_synchronisers(
    with_audio: bool,
) -> None:
    top = _CoreTop(with_audio)
    a = cdc_structure.analyse(top, top.ports(), {"project": "project", **PRIMITIVES})
    assert a.errors == []
    assert [cdc_structure.name(s) for s in a.mixed] == []
    c = top.core
    crossings = {
        c.slot.ctl_x: "project",
        c.slot.buttons_x: "project",
        c.atspeed.cmd_x: "project",
        c.atspeed.rep_x: "sys",
        c.vga_settings_x: "project",
        c.vga_timing_x: "sys",
        c.sevenseg_x: "sys",
    }
    expected = {(domain, id(x.dst), id(x.hold)) for x, domain in crossings.items()}
    found = {(d, id(next(iter(t))), id(s)) for d, t, s in a.reads if len(t) == 1}
    assert found == expected and len(a.reads) == len(expected)
    assert all(m.stages == 2 for m in a.multiregs)
    assert len(a.multiregs) == 2 * len(crossings) + 3  # + LOCKED, clock enable, alive toggle
    # The framebuffer: written in `project`, read in `sys`, each port fed from its own domain.
    ports = {(p.domain, frozenset(p.input_domains)) for p in a.memory_ports}
    assert ports == {("project", frozenset({"project"})), ("sys", frozenset({"sys"}))}


# The registers on the bus --------------------------------------------------------------------

LINE, HSYNC, FRAME_LINES, PIC_X, PIC_Y = 16, 2, 8, 4, 2  # the synthetic picture
CONSTANT = 0xA5  # uo_out during the at-speed and seven-segment part


def _uo(r: int, g: int, b: int, hsync: int, vsync: int) -> int:
    bits = {
        vga.BIT_R1: r >> 1,
        vga.BIT_R0: r & 1,
        vga.BIT_G1: g >> 1,
        vga.BIT_G0: g & 1,
        vga.BIT_B1: b >> 1,
        vga.BIT_B0: b & 1,
        vga.BIT_HSYNC: hsync,
        vga.BIT_VSYNC: vsync,
    }
    return sum(v << k for k, v in bits.items())


def _colour(x: int, y: int) -> tuple[int, int, int]:
    return x & 3, y & 3, (3 - x) & 3


def _picture(clock: int) -> int:
    """uo_out at project clock `clock`: 16-clock lines (hsync low for 2), 8-line frames (vsync
    low for line 0), a 4 x 2 picture at clocks 4..7 of lines 2..3, negative syncs."""
    x, line = clock % LINE, (clock // LINE) % FRAME_LINES
    hsync, vsync = int(x >= HSYNC), int(line != 0)
    px, py = x - PIC_X, line - PIC_Y
    if 0 <= px < 4 and 0 <= py < 2:
        return _uo(*_colour(px, py), hsync, vsync)
    return _uo(0, 0, 0, hsync, vsync)


def test_registers_read_back() -> None:
    soc = soc_for_test()
    soc.clock_domains.cd_sys = ClockDomain("sys")
    master = wishbone.Interface(data_width=32, address_width=32, addressing="word")
    soc.bus.add_master(name="tb", master=master)
    soc.finalize()
    regs = {r.name: r for r in registers.registers(soc)}
    slot = soc.core.slot
    state = {"constant": False}
    seen = {}

    def write(name: str, value: int):
        reg = regs[name]
        for i in range(reg.words):  # big ordering: the first word holds the top bits
            word = (value >> (32 * (reg.words - 1 - i))) & 0xFFFFFFFF
            yield from master.write((reg.address >> 2) + i, word)

    def read(name: str):
        reg = regs[name]
        value = 0
        for i in range(reg.words):
            value = value << 32 | (yield from master.read((reg.address >> 2) + i))
        return value

    def wait(n: int):
        for _ in range(n):
            yield

    def bus():
        # Build facts.
        assert (yield from read("info_clock_hz")) == CLOCK_HZ
        assert (yield from read("info_clock_hz_num")) == CLOCK_HZ
        assert (yield from read("info_clock_hz_den")) == 1
        assert (yield from read("info_fb_width")) == 4
        assert (yield from read("info_fb_height")) == 2
        pmods = yield from read("info_pmods")
        assert pmods == (1 << core.PMODS.index("tiny-vga")) | (1 << core.PMODS.index("gamepad"))
        # Every writable register reads back what was written.
        patterns = {
            "slot_ena": 1,
            "slot_rst_n": 1,
            "slot_ui_in": 0xC3,
            "slot_clock_enable": 1,
            "gamepad_enable": 1,
            "gamepad_buttons": 0xA5A5A5,
            "vga_divider": 5,
            "vga_h_offset": 0xABC,
            "vga_v_offset": 0x5A5,
            "vga_h_step": 3,
            "vga_v_step": 2,
            "vga_blank": 1,
            "vga_readback_addr": 7,
            "atspeed_clocks": 0xDEADBEEF,
        }
        for name, value in patterns.items():
            yield from write(name, value)
            assert (yield from read(name)) == value, name
        # The slot's controls reach its pins (gamepad mux off).
        yield from write("gamepad_enable", 0)
        yield from wait(200)
        assert (yield slot.pin_ena) == 1 and (yield slot.pin_ui_in) == 0xC3
        assert (yield slot.pin_rst_n) == 1
        assert (yield soc.core.slot.gamepad.buttons) == 0xA5A5A5
        status = yield from read("slot_status")
        assert status == 0b11, "locked and clock_running"
        # The capture settings of the synthetic picture; the measured timing.
        for name, value in (
            ("vga_divider", 1),
            ("vga_h_offset", PIC_X),
            ("vga_v_offset", PIC_Y),
            ("vga_h_step", 1),
            ("vga_v_step", 1),
            ("vga_blank", 0),
        ):
            yield from write(name, value)
        yield from wait(8 * 2 * LINE * FRAME_LINES)  # eight frames at half the sys rate
        assert (yield from read("vga_clocks_per_line")) == LINE
        assert (yield from read("vga_lines_per_frame")) == FRAME_LINES
        assert (yield from read("vga_hsync_width")) == HSYNC
        assert (yield from read("vga_vsync_lines")) == 1
        assert (yield from read("vga_status")) == 0b0100, "locked; negative syncs; a signal"
        assert (yield from read("vga_frames")) >= 6
        pixels = []
        for addr in range(8):
            yield from write("vga_readback_addr", addr)
            pixels.append((yield from read("vga_readback_data")))
        assert pixels == [vga.pack(*_colour(x, y)) for y in range(2) for x in range(4)]
        # A constant on uo_out: the seven-segment duty and an at-speed CRC of it.
        state["constant"] = True
        yield from wait(100)
        duty = yield from read("sevenseg_duty")
        want = sum((255 if CONSTANT >> i & 1 else 0) << (8 * i) for i in range(8))
        assert duty == want, hex(duty)
        yield from write("atspeed_clocks", 40)
        yield from write("atspeed_control", 1)  # start
        for _ in range(100):
            if (yield from read("atspeed_status")) == 0b10:  # done, not busy
                break
            yield from wait(20)
        assert (yield from read("atspeed_status")) == 0b10
        assert (yield from read("atspeed_crc")) == zlib.crc32(bytes([CONSTANT]) * 40)
        yield from write("atspeed_control", 2)  # abort clears done
        assert (yield from read("atspeed_status")) == 0
        seen["done"] = True

    @passive
    def picture():
        clock = 0
        while True:
            yield slot.uo_out.eq(CONSTANT if state["constant"] else _picture(clock))
            clock += 1
            yield

    # A 10 ns tick: the reset release's exact timing is not under test here (`test_slot.py`).
    run(
        gate(soc),
        sys_gens=[bus()],
        project_gens=[picture()],
        project=(40_000, 5_050),
        tick_period=10_000,
    )
    assert seen.get("done")


# The BIOS in Verilator ------------------------------------------------------------------------


def _counter() -> Sources:
    files = ("tt_um_test_counter.v",)
    project = Project(
        shuttle="tt00",
        macro="tt_um_test_counter",
        repo="/nonexistent",
        commit="0" * 40,
        top_module="tt_um_test_counter",
        source_files=files,
        clock_hz=CLOCK_HZ,
        clock_declared=CLOCK_HZ,
        pmods=("tiny-vga",),
        address=None,
        ui_in=0,
        uio_in=0,
        patch=None,
    )
    root = DATA / "projects" / "counter"
    return Sources(project, root, tuple(root / "src" / f for f in files), needs_sv=False)


@pytest.mark.toolchain
@pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed")
@pytest.mark.skipif(tools.find("riscv64-unknown-elf-gcc") is None, reason="no RISC-V GCC")
def test_bios_boots_and_prints_banner(tmp_path: Path) -> None:
    result = sim.run(
        _counter(),
        demo(),
        until=lambda text: "litex> " in sim.plain(text),
        max_cycles=40_000_000,
        out_dir=tmp_path,
    )
    assert result.reached_until, result.text[-2000:]
    text = result.plain
    for phrase in ("Build your hardware, easily!", "BIOS CRC passed", "VexRiscv", "Memtest OK"):
        assert phrase in text, phrase
    assert text.index("Build your hardware") < text.index("litex> ")
