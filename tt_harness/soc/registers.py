# SPDX-License-Identifier: Apache-2.0
"""The CSR map of `HarnessSoC` as Markdown: the generated part of `docs/registers.md`.

`registers(soc)` lists every CSR register of a finalised SoC with its address (the CSR region's
origin plus 4 bytes per 32-bit word before it, as LiteX's `csr.csv` export computes it),
width, access, reset value, description and fields. `render(soc)` is the Markdown of that list;
`splice(doc, generated)` puts it between `BEGIN` and `END` in a document whose other text is
written by hand. `reference_soc()` is the configuration the document shows: the slot of an
Arty (100 MHz in) at 25.175 MHz, a 320 x 240 framebuffer, readback on, audio off.

Regenerate the document with::

    uv run python -m tt_harness.soc.registers
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from litex.soc.interconnect.csr import CSRStatus, CSRStorage
from migen import Memory, Signal

from tt_harness.core.slot import ProjectSlot
from tt_harness.soc import core, sim

DOC = Path(__file__).resolve().parent.parent.parent / "docs" / "registers.md"
BEGIN = "<!-- BEGIN GENERATED CSR MAP (uv run python -m tt_harness.soc.registers) -->"
END = "<!-- END GENERATED CSR MAP -->"
REFERENCE_DEMO = (
    b'{"version": 1, "shuttle": "tt00", "projects": [{"address": 1, "subtile": 0,'
    b' "duration_ms": 1000, "clock_hz": 25175000, "ui_in": 0, "steps": []}]}'
)


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    size: int
    access: str
    reset: int | None
    description: str


@dataclass(frozen=True)
class Register:
    module: str
    name: str  # the C name, `<module>_<register>`
    address: int
    size: int  # bits
    words: int  # 32-bit words; the first holds the top bits (big ordering)
    access: str
    reset: int | None
    description: str
    fields: tuple[Field, ...]


def _access(csr) -> str:
    """LiteX's own rule (`export.get_csr_csv`, and `csr.h`'s write accessors): read-only when
    the CSR says so, or for a `CSRStatus` with no write port. An event manager's `pending` is
    a `CSRStatus(read_only=False)`, written 1 to clear."""
    if getattr(csr, "read_only", False):
        return "read-only"
    if isinstance(csr, CSRStatus) and not hasattr(csr, "wr_data"):
        return "read-only"
    return "read-write"


def _reset(csr) -> int | None:
    """The reset value of a register the CPU writes; None for one the hardware drives."""
    sig = getattr(csr, "storage", None)
    return sig.reset.value if isinstance(sig, Signal) else None


def _hex(value: int | None) -> str:
    return "-" if value is None else f"`0x{value:x}`"


def _fields(csr) -> tuple[Field, ...]:
    agg = getattr(csr, "fields", None)
    out = []
    for f in getattr(agg, "fields", []) or []:
        access = "write: pulse" if f.pulse else _access(csr)
        desc = " ".join(str(f.description or "").split())
        reset = None if _reset(csr) is None else f.reset_value
        out.append(Field(f.name, f.offset, f.size, access, reset, desc))
    return tuple(out)


def registers(soc) -> list[Register]:
    """Every CSR register of the finalised `soc`, in address order."""
    out = []
    for module, region in soc.csr_regions.items():
        if isinstance(region.obj, Memory):
            continue
        address = region.origin
        for csr in region.obj:
            words = (csr.size + region.busword - 1) // region.busword
            desc = " ".join(str(getattr(csr, "description", None) or "").split())
            if not isinstance(csr, (CSRStatus, CSRStorage)):
                desc = desc or "A LiteX core register."
            out.append(
                Register(
                    module,
                    f"{module}_{csr.name}",
                    address,
                    csr.size,
                    words,
                    _access(csr),
                    _reset(csr),
                    desc,
                    _fields(csr),
                )
            )
            address += 4 * words
    return sorted(out, key=lambda r: r.address)


def _bits(offset: int, size: int) -> str:
    return str(offset) if size == 1 else f"{offset + size - 1}:{offset}"


def render(soc) -> str:
    """The generated Markdown: one section per CSR module, and the CSR memories."""
    regs = registers(soc)
    lines = [BEGIN, "", "## CSR map", ""]
    lines += [
        (
            "Generated from `tt_harness.soc.registers.reference_soc()`. Addresses are byte"
            " addresses; a register of more than 32 bits takes several words, the first holding"
            " the top bits. Reset is given for the registers the CPU writes; `-` marks one the"
            " hardware drives. LiteX's own cores (`ctrl`, `timer0`, `uart`) are listed without"
            " their descriptions, which are LiteX's."
        ),
        "",
    ]
    modules = list(dict.fromkeys(r.module for r in regs))
    for module in modules:
        mine = [r for r in regs if r.module == module]
        lines += [f"### `{module}`", ""]
        lines += [
            "| Register | Address | Words | Bits | Access | Reset |",
            "|---|---|---|---|---|---|",
        ]
        for r in mine:
            lines.append(
                f"| `{r.name}` | `0x{r.address:08x}` | {r.words} | {r.size} | {r.access} |"
                f" {_hex(r.reset)} |"
            )
        lines.append("")
        if module not in core.CSR_MODULES:
            continue  # LiteX's own cores: described by LiteX
        for r in mine:
            lines += [f"#### `{r.name}`", ""]
            if r.description:
                lines += [r.description, ""]
            if r.fields:
                lines += ["| Field | Bits | Access | Reset |", "|---|---|---|---|"]
                for f in r.fields:
                    lines.append(
                        f"| `{f.name}` | {_bits(f.offset, f.size)} | {f.access} | {_hex(f.reset)} |"
                    )
                lines.append("")
                lines += [f"- `{f.name}`: {f.description}" for f in r.fields if f.description]
                lines.append("")
    memories = [(n, r) for n, r in soc.csr_regions.items() if isinstance(r.obj, Memory)]
    if memories:
        lines += ["### CSR memories", "", "| Memory | Address | Address span (bytes) |", "|---|---|---|"]
        for name, region in memories:
            span = region.obj.depth * region.busword // 8  # one bus word per entry
            lines.append(f"| `{name}` | `0x{region.origin:08x}` | {span} |")
        lines.append("")
    lines.append(END)
    return "\n".join(lines) + "\n"


def splice(doc: str, generated: str) -> str:
    """`doc` with the text from `BEGIN` to `END` (inclusive) replaced by `generated`."""
    if doc.count(BEGIN) != 1 or doc.count(END) != 1:
        raise ValueError(f"the document must hold {BEGIN!r} and {END!r} once each")
    start = doc.index(BEGIN)
    stop = doc.index(END) + len(END) + 1  # and its newline
    return doc[:start] + generated + doc[stop:]


def reference_soc():
    """The configuration `docs/registers.md` shows, finalised."""
    slot = ProjectSlot(None, Signal(), 100_000_000, 25_175_000)
    soc = core.HarnessSoC(
        sim.platform(),
        50_000_000,
        slot,
        (320, 240),
        REFERENCE_DEMO,
        "tt00/tt_um_reference",
        with_audio=False,
        with_readback=True,
        pmods=("tiny-vga", "gamepad"),
        reference_ui_in=0,
    )
    soc.finalize()
    return soc


def main() -> int:
    doc = DOC.read_text()
    new = splice(doc, render(reference_soc()))
    if new != doc:
        DOC.write_text(new)
        print(f"updated {DOC}")
    else:
        print(f"{DOC} is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
