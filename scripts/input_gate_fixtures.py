# SPDX-License-Identifier: Apache-2.0
"""Cut a saved case for the input gate's tests out of a real Yosys netlist.

    uv run python scripts/input_gate_fixtures.py BUILD_DIR OUT.json

`BUILD_DIR` is an openXC7 compare build directory (`arty_compare.v`, `arty_compare.json`).
The netlists are megabytes; the excerpt keeps, of the top module, only the nets on the
project's inputs and the cells that drive them, with those cells' own input nets renamed
away, which is all `tt_harness.input_gate.yosys_drivers` reads. The cases under
`tests/data/input_gate/` were made this way from builds of the test counter design: one with
the compare top as it is, one with `rst_n` as the comparator it was before Task 9c.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tt_harness import input_gate
from tt_harness.boards import arty
from tt_harness.compare.top import PROJECT_INSTANCE


def excerpt(build_dir: Path) -> dict:
    top = arty.MODULE_NAME
    nets = input_gate.input_nets((build_dir / f"{top}.v").read_text(), PROJECT_INSTANCE)
    module = json.loads((build_dir / f"{top}.json").read_text())["modules"][top]
    # Bits only: attributes and cell names carry the build's file paths.
    netnames = {
        n: {"bits": module["netnames"][n]["bits"]} for n in nets.values() if n in module["netnames"]
    }
    wanted = {bit for net in netnames.values() for bit in net["bits"] if not isinstance(bit, str)}
    cells = {}
    for cell in module["cells"].values():
        outputs = [p for p, d in cell["port_directions"].items() if d == "output"]
        if any(bit in wanted for p in outputs for bit in cell["connections"][p]):
            cells[f"driver{len(cells)}"] = {
                "type": cell["type"],
                "port_directions": cell["port_directions"],
                "connections": {p: cell["connections"][p] for p in outputs},
            }
    return {
        "comment": f"excerpt of a Yosys netlist of {top} (scripts/input_gate_fixtures.py)",
        "nets": nets,
        "modules": {top: {"netnames": netnames, "cells": cells}},
    }


def main() -> int:
    build_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(excerpt(build_dir), indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
