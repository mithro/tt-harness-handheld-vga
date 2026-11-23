# SPDX-License-Identifier: Apache-2.0
"""Pre-screen a design for constructs that make simulation and fabric disagree.

Latches, combinational loops (ring oscillators) and clocks made in logic behave
differently in the reference simulation and on an FPGA, so a mismatch on a design that has
any of them is not the harness's fault (verdict `nondeterministic`).

Method. Yosys reads the sources (`-sv` when a source is `.sv`, or as a retry when plain
Verilog parsing fails), then `hierarchy -check -top <top>; proc -noopt; flatten`.
`proc -noopt` matters: `proc`'s closing `opt_expr` folds an inverter ring away.

- latches: latch cells (`$dlatch`, `$adlatch`, `$dlatchsr`, `$sr` and their gate forms) in
  the flattened top, counted from `write_json`;
- comb_loops: loops reported by `check -force-detailed-loop-check` (bit-level), one
  "found logic loop" warning each, read from that pass's own output (`tee -o`);
- derived_clocks: distinct nets clocking a flip-flop or memory port that do not reach the
  top-level `clk` port through only wires, buffers and inverters, from `write_json`.

Known blind spots: registers without a reset (whose initial values differ between the
simulator and the fabric) and asynchronous resets driven from logic are not detected, so a
mismatch they cause is reported as `mismatch`. Gated or multiplexed clocks are
deliberately counted as derived; that is conservative and can label a real harness
mismatch `nondeterministic`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tt_harness import tools
from tt_harness.fetch import Sources

CLOCK_PORT = "clk"
_YOSYS_TIMEOUT_S = 600
_LOOP_RE = re.compile(r"^Warning: found logic loop in module ", re.MULTILINE)
_CONST_BITS = ("0", "1", "x", "z")

_FF_TYPES = {
    "$dff",
    "$dffe",
    "$adff",
    "$adffe",
    "$sdff",
    "$sdffe",
    "$sdffce",
    "$dffsr",
    "$dffsre",
    "$aldff",
    "$aldffe",
}
_FF_GATE_PREFIXES = ("$_DFF_", "$_DFFE_", "$_SDFF", "$_DFFSR_", "$_DFFSRE_", "$_ALDFF")
_MEM_PORT_TYPES = {"$memrd", "$memrd_v2", "$memwr", "$memwr_v2"}
_LATCH_TYPES = {"$dlatch", "$adlatch", "$dlatchsr", "$sr"}
_LATCH_GATE_PREFIXES = ("$_DLATCH", "$_SR_")
# Cells whose output bit i is input bit i or its inverse: a clock may pass through these.
_PASS_THROUGH = {"$not", "$pos", "$buf", "$_NOT_", "$_BUF_"}


class PrescreenError(RuntimeError):
    """The pre-screen could not run: no Yosys, or Yosys rejected the sources."""


@dataclass(frozen=True)
class Prescreen:
    latches: int
    comb_loops: int
    derived_clocks: int
    flagged: bool  # any of the three is non-zero
    log: Path  # Yosys log of the run the counts come from
    sv_retried: bool = False  # plain Verilog failed and `read_verilog -sv` succeeded


def _quote(path: Path) -> str:
    text = str(path)
    if '"' in text or "\n" in text:
        raise PrescreenError(f"path {text!r} cannot be quoted in a Yosys script")
    return f'"{text}"'


def _script(sources: Sources, sv: bool, out: Path) -> str:
    include_dirs = sorted({f.parent for f in sources.files} | {sources.root / "src"})
    read = ["read_verilog", *(["-sv"] if sv else [])]
    read += [f"-I{_quote(d)}" for d in include_dirs]
    read += [_quote(f) for f in sources.files]
    return "\n".join(
        [
            " ".join(read),
            f"hierarchy -check -top {sources.project.top_module}",
            "proc -noopt",
            "flatten",
            f"tee -q -o {_quote(out / 'check.log')} check -force-detailed-loop-check",
            f"write_json {_quote(out / 'design.json')}",
            "",
        ]
    )


def _yosys(yosys: Path, sources: Sources, sv: bool, out: Path) -> tuple[bool, Path]:
    """Run the pre-screen script; returns (succeeded, log path)."""
    stem = "yosys-sv" if sv else "yosys"
    script = out / f"{stem}.ys"
    log = out / f"{stem}.log"
    script.write_text(_script(sources, sv, out))
    for stale in (out / "check.log", out / "design.json"):
        stale.unlink(missing_ok=True)
    with log.open("w") as f:
        try:
            result = subprocess.run(
                [str(yosys), "-s", str(script)],
                cwd=out,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=_YOSYS_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            f.write(f"\nprescreen: yosys timed out after {_YOSYS_TIMEOUT_S} s\n")
            return False, log
    return result.returncode == 0, log


def _first_error(log: Path) -> str:
    for line in log.read_text(errors="replace").splitlines():
        if "ERROR:" in line or line.startswith("prescreen: yosys timed out"):
            return line.strip()
    return "no ERROR line in the log"


def _bit_names(module: dict) -> dict:
    names: dict = {}
    for name, net in module.get("netnames", {}).items():
        for i, bit in enumerate(net["bits"]):
            if not net.get("hide_name"):
                names.setdefault(bit, f"{name}[{i}]" if len(net["bits"]) > 1 else name)
    return names


def _clock_bits(cell: dict) -> list:
    """The clock bits of a flip-flop or clocked memory port; [] for any other cell."""
    kind = cell["type"]
    conns = cell["connections"]
    if kind in _FF_TYPES:
        return conns["CLK"]
    if kind.startswith(_FF_GATE_PREFIXES):
        return conns["C"]
    if kind in _MEM_PORT_TYPES and "1" in str(cell["parameters"].get("CLK_ENABLE", "0")):
        return conns["CLK"]
    return []


def _analyse(design: dict, top: str) -> tuple[int, dict]:
    """Count latch cells and find derived clock roots (root bit -> flops it clocks)."""
    try:
        module = design["modules"][top]
    except KeyError:
        raise PrescreenError(f"top module {top} missing from Yosys JSON") from None
    cells = module.get("cells", {}).values()
    port = module.get("ports", {}).get(CLOCK_PORT)
    clk_bits = set(port["bits"]) if port and port.get("direction") == "input" else set()

    drivers: dict = {}
    for cell in cells:
        for name, direction in cell.get("port_directions", {}).items():
            if direction == "output":
                for i, bit in enumerate(cell["connections"][name]):
                    drivers[bit] = (cell, i)

    def root(bit):
        seen = set()
        while bit not in clk_bits and bit not in _CONST_BITS and bit not in seen:
            seen.add(bit)
            cell, i = drivers.get(bit, (None, 0))
            if cell is None or cell["type"] not in _PASS_THROUGH:
                return bit
            inputs = cell["connections"]["A"]
            if i >= len(inputs):
                return bit
            bit = inputs[i]
        return None if bit in clk_bits or bit in _CONST_BITS else bit

    latches = 0
    derived: dict = {}
    for cell in cells:
        kind = cell["type"]
        if kind in _LATCH_TYPES or kind.startswith(_LATCH_GATE_PREFIXES):
            latches += 1
        for bit in _clock_bits(cell):
            r = root(bit)
            if r is not None:
                derived[r] = derived.get(r, 0) + 1
    return latches, derived


def check(sources: Sources, out_dir: Path | None = None) -> Prescreen:
    """Pre-screen `sources`; logs go to `out_dir` (default build/prescreen/<shuttle>/<macro>)."""
    project = sources.project
    yosys = tools.find("yosys")
    if yosys is None:
        raise PrescreenError(f"{project.id}: pre-screen needs yosys, which was not found")
    out = Path(out_dir) if out_dir else tools.REPO / "build" / "prescreen" / project.id
    out.mkdir(parents=True, exist_ok=True)
    out = out.resolve()

    ok, log = _yosys(yosys, sources, sources.needs_sv, out)
    sv_retried = False
    if not ok and not sources.needs_sv:
        first_log = log
        ok, log = _yosys(yosys, sources, True, out)
        if not ok:
            raise PrescreenError(
                f"{project.id}: pre-screen: yosys failed reading or elaborating the sources, "
                f"also with -sv: {_first_error(first_log)} (log {first_log}, {log})"
            )
        sv_retried = True
    elif not ok:
        raise PrescreenError(
            f"{project.id}: pre-screen: yosys failed reading or elaborating the sources: "
            f"{_first_error(log)} (log {log})"
        )

    comb_loops = len(_LOOP_RE.findall((out / "check.log").read_text()))
    design = json.loads((out / "design.json").read_text())
    latches, derived = _analyse(design, project.top_module)

    names = _bit_names(design["modules"][project.top_module])
    with log.open("a") as f:
        f.write("\nprescreen summary\n")
        if sv_retried:
            f.write("prescreen: plain Verilog failed; read with -sv\n")
        f.write(f"prescreen: latches {latches}, comb_loops {comb_loops}\n")
        for bit, count in sorted(derived.items(), key=lambda kv: str(kv[0])):
            f.write(f"prescreen: derived clock {names.get(bit, bit)} clocks {count} cell(s)\n")
    return Prescreen(
        latches=latches,
        comb_loops=comb_loops,
        derived_clocks=len(derived),
        flagged=bool(latches or comb_loops or derived),
        log=log,
        sv_retried=sv_retried,
    )
