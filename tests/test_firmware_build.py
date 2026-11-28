# SPDX-License-Identifier: Apache-2.0
"""The firmware build (`tt_harness.soc.firmware.FirmwareBuilder`), without a simulation.

- `test_firmware_builder_needs_the_pinned_gcc`: with the pinned RISC-V GCC missing the build
  is a `FileNotFoundError` naming its path, before anything is compiled (no GCC on PATH is
  ever used).
- `test_firmware_is_in_the_rom_and_the_bios_is_not_built` (`toolchain`): the overrides of
  LiteX's `Builder` took effect: no `bios.bin` is built, and the ROM's contents are
  `firmware.bin`, word for word, then zeros.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from test_soc import _counter, demo

from tt_harness.soc import core, firmware, sim


def _builder(tmp_path: Path) -> firmware.FirmwareBuilder:
    soc = sim.build_soc(_counter(), demo())
    return firmware.FirmwareBuilder(
        soc, output_dir=str(tmp_path / "litex"), compile_gateware=False, build_bundle=False
    )


def test_firmware_builder_needs_the_pinned_gcc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-such-toolchain" / "bin"
    monkeypatch.setattr(firmware, "RISCV_GCC", missing)
    builder = _builder(tmp_path)
    with pytest.raises(FileNotFoundError, match=str(missing / "riscv64-unknown-elf-gcc")):
        builder.build(build=False, run=False)
    assert not (tmp_path / "litex" / "software").exists(), "nothing was generated"


@pytest.mark.toolchain
@pytest.mark.skipif(
    not (firmware.RISCV_GCC / f"{firmware.GCC_TRIPLE}-gcc").is_file(), reason="no RISC-V GCC"
)
def test_firmware_is_in_the_rom_and_the_bios_is_not_built(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.build(build=False, run=False)
    software = tmp_path / "litex" / "software"
    assert not (software / "bios" / "bios.bin").exists(), "the BIOS was built"
    image = (software / "firmware" / "firmware.bin").read_bytes()
    image += b"\0" * (-len(image) % 4)
    words = list(struct.unpack(f"<{len(image) // 4}I", image))
    rom = list(builder.soc.rom.mem.init)  # what the gateware's ROM is initialised with
    assert len(rom) == core.ROM_SIZE // 4
    assert rom[: len(words)] == words
    assert not any(rom[len(words) :])
