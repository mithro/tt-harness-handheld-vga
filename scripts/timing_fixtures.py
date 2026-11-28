# SPDX-License-Identifier: Apache-2.0
"""Regenerate the saved tool reports under `tests/data/timing/` from real compare builds.

    uv run python scripts/timing_fixtures.py [--work DIR] [--reuse]

Builds `tt08/tt_um_tinytapeout_logo_screensaver` and `ttsky26a/tt_um_algofoogle_dottee` for
the Arty with openXC7 and with Vivado (one build at a time, `build.build_compare`, the
default settle), under `--work` (default `tmp/timing-fixtures/`, never the default
`build/compare` tree), and writes an excerpt of each build's timing output:

- `nextpnr-arty-<name>.log`: from `nextpnr.log`, the lines the gate reads and their
  neighbours: the clock constraints, the `tt-harness` clock lines, checksums, the `Max
  frequency` and `Max delay` lines before and after routing, and `Routing complete`.
- `vivado-arty-<name>-timing-summary.rpt`: `timing.rpt` up to the end of its summary
  tables, without the `Command` and `Host` lines. For dottee (the design with a project
  clock) the lines the flow puts in front of the report are kept in front: the clock-root
  facts, the project clock mapping and the pair-report requirements.

`nextpnr-arty-logo-pclk125-fail.log` is not regenerated: it is the logo netlist with pclk
constrained at 125 MHz by hand, and no number in it depends on the settle.

The numbers in these files change with every regeneration (and with the settle): the tests
in `tests/test_build.py` that quote them must be updated to match. `--reuse` extracts from
builds already under `--work` instead of building.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from tt_harness import build, fetch, manifest
from tt_harness import project_clocks as pc
from tt_harness.tools import REPO

DESIGNS = {
    "logo": "tt08/tt_um_tinytapeout_logo_screensaver",
    "dottee": "ttsky26a/tt_um_algofoogle_dottee",
}
CLOCKS = 6_000_000
OUT = REPO / "tests" / "data" / "timing"
_NEXTPNR_KEEP = re.compile(
    r"^(Info: constraining clock net |tt-harness |Info: Checksum: "
    r"|(Info|ERROR|Warning): Max (frequency|delay) |Info: Routing complete\.|Info: Router1 time )"
)
_VIVADO_DROP = ("| Command ", "| Host ")
_VIVADO_END = "| Timing Details"
_VIVADO_FRONT = (pc.ROOT_PREFIX, pc.CONTRIBUTOR_PREFIX, build._VIVADO_PROJECT_CLOCK_PREFIX)


def nextpnr_excerpt(out_dir: Path) -> str:
    lines = (out_dir / "nextpnr.log").read_text(errors="replace").splitlines()
    return "\n".join(ln for ln in lines if _NEXTPNR_KEEP.match(ln)) + "\n"


def vivado_excerpt(out_dir: Path, front: bool) -> str:
    report = (out_dir / "timing.rpt").read_text().splitlines()
    end = next(i for i, ln in enumerate(report) if ln.startswith(_VIVADO_END))
    kept = [ln for ln in report[:end] if not ln.startswith(_VIVADO_DROP)]
    head: list[str] = []
    if front:
        log = (out_dir / build.LOG_NAME).read_text(errors="replace").splitlines()
        # The last Vivado attempt's lines (an unmodified attempt that failed prints none).
        head = [ln for ln in log if ln.startswith(_VIVADO_FRONT)]
        head = list(dict.fromkeys(head)) + build._pair_report_lines(out_dir)
    return "\n".join([*head, *kept]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, default=REPO / "tmp" / "timing-fixtures")
    parser.add_argument("--reuse", action="store_true", help="extract only; build nothing")
    args = parser.parse_args()
    videos = manifest.default_videos_repo()
    projects = manifest.load(videos)
    for toolchain in build.TOOLCHAINS:
        for name, project_id in DESIGNS.items():
            project = manifest.get(projects, project_id)
            out_dir = args.work.resolve() / f"arty-{toolchain}" / name
            if not args.reuse:
                sources = fetch.fetch(project, REPO / "work" / "sources", videos)
                for warning in sources.warnings:
                    print(f"{project_id}: warning: {warning}", flush=True)
                print(f"building {project_id} with {toolchain} in {out_dir}", flush=True)
                build.build_compare(project, sources, "arty", toolchain, out_dir, CLOCKS)
            if toolchain == "openxc7":
                target = OUT / f"nextpnr-arty-{name}.log"
                target.write_text(nextpnr_excerpt(out_dir))
            else:
                target = OUT / f"vivado-arty-{name}-timing-summary.rpt"
                target.write_text(vivado_excerpt(out_dir, front=name == "dottee"))
            print(f"wrote {target}", flush=True)
    print("timing_fixtures finished", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
