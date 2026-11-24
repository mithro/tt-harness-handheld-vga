# SPDX-License-Identifier: Apache-2.0
"""The first test pool, and one design of it through the compare build in simulation.

`run_sim` fetches a design, pre-screens it, simulates the compare top with the design in
Verilator (`tt_harness.compare.sim`, the simulation `tests/test_compare_top.py` uses),
decodes the frame from the simulated UART and compares it with a reference, clock by clock:
the shared reference (`$TT_REFERENCE_DIR`) when the design has one, else a local Verilator
reference made on the spot. Which one was used is recorded. A shared reference that is
present but unusable is an error, never a silent fall back to the local one.

Files under `<work>/<shuttle>/<macro>/`: `compare/` (the simulation, `compare.log`),
`prescreen/` (Yosys logs), `reference/` (only when the local reference was made) and
`result.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from tt_harness import fetch, manifest, prescreen, reference, verdict
from tt_harness.compare import sim
from tt_harness.core.stimulus import RESET_CLOCKS
from tt_harness.fetch import Sources
from tt_harness.host import frame
from tt_harness.tools import REPO

# The eight designs of "First test pool" in docs/test-designs.md, in its order.
POOL: tuple[str, ...] = (
    "tt07/tt_um_tinytapeout_dvd_screensaver",
    "tt08/tt_um_tinytapeout_logo_screensaver",
    "ttihp26a/tt_um_urish_rings",
    "ttihp0p3/tt_um_algofoogle_fun_vga_clock",
    "ttihp25a/tt_um_algofoogle_vga",
    "ttsky26a/tt_um_algofoogle_dottee",
    "ttsky25a/tt_um_gamepad_pmod_demo",
    "ttihp25a/tt_um_2048_vga_game",
)

# Simulation only: the system clock of the model and a UART divider of 4, for speed. The
# bytes sent do not depend on either.
SIM_SYS_CLK_FREQ = 100_000_000
SIM_BAUD = SIM_SYS_CLK_FREQ // 4
DEFAULT_CLOCKS = 1_300_000  # about three 640x480 frames
DEFAULT_WORK = REPO / "work" / "pool"
RESULT_NAME = "result.json"
SUMMARY_NAME = "summary.json"

_EXIT = {"exact": 0, "mismatch": 1, "nondeterministic": 1, "inconclusive": 2, "error": 2}


# What stops a run: fetch, pre-screen, simulation and reference errors are RuntimeErrors,
# an unusable shared reference a ValueError, an unknown project a KeyError.
_RUN_FAILURES = (KeyError, OSError, ValueError, RuntimeError, subprocess.SubprocessError)


class PoolError(RuntimeError):
    """A pool design could not be run (as opposed to: it ran and differed)."""


@dataclass(frozen=True)
class PoolResult:
    id: str
    verdict: str  # a `verdict.Verdict` kind, or "error" when the run itself failed
    first_diff_clock: int | None
    detail: str
    prescreen: dict  # latches, comb_loops, derived_clocks, flagged, sv_retried, log
    reference_source: str  # "shared" or "local"
    reference_path: str
    reference_sha256: str  # of the bytes compared against
    clocks: int
    seconds: float

    def to_json(self) -> dict:
        return asdict(self)


def load_sources(project_id: str) -> Sources:
    """The fetched sources of a manifest project (`$TT_VIDEOS_REPO`, cache `work/sources`)."""
    projects = manifest.load(manifest.default_videos_repo())
    return fetch.fetch(manifest.get(projects, project_id), REPO / "work" / "sources")


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _shared_reference(sources: Sources, clocks: int) -> tuple[bytes, Path] | None:
    """The first `clocks` samples of the shared reference and its path; None if there is none."""
    project = sources.project
    path = verdict.default_reference_dir() / project.shuttle / project.macro / "reference.json"
    if not path.is_file():
        return None
    ref = verdict.load_reference(verdict.default_reference_dir(), project.id)
    for name, theirs, ours in (
        ("ui_in", ref.ui_in, project.ui_in),
        ("uio_in", ref.uio_in, project.uio_in),
        ("reset_clocks", ref.reset_clocks, RESET_CLOCKS),
    ):
        if theirs != ours:
            raise PoolError(
                f"{project.id}: shared reference {name} {theirs} differs from the compare "
                f"build's {ours} ({ref.path})"
            )
    if ref.clocks < clocks:
        raise PoolError(
            f"{project.id}: shared reference spans {ref.clocks} clocks, fewer than the "
            f"{clocks} to run ({ref.path})"
        )
    return ref.samples[:clocks], ref.path


def run_sim(project_id: str, clocks: int, work: Path) -> PoolResult:
    """Run one design through the simulated compare build and judge it; write `result.json`.

    Raises on anything that stops the run (unknown project, fetch, pre-screen, simulation,
    an unusable shared reference); a frame that decodes badly or differs is a verdict.
    """
    began = time.monotonic()
    sources = load_sources(project_id)
    out = Path(work) / sources.project.shuttle / sources.project.macro
    out.mkdir(parents=True, exist_ok=True)
    (out / RESULT_NAME).unlink(missing_ok=True)

    screen = prescreen.check(sources, out / "prescreen")
    shared = _shared_reference(sources, clocks)
    data = sim.simulate(sources, clocks, out / "compare", SIM_SYS_CLK_FREQ, SIM_BAUD)
    rec = frame.decode(data)
    if shared is not None:
        samples, ref_path = shared
        source = "shared"
    else:
        ref_path = reference.simulate(sources, clocks, out / "reference")
        samples = ref_path.read_bytes()
        source = "local"
    judged = verdict.compare(rec, samples, screen.flagged)
    result = PoolResult(
        id=sources.project.id,
        verdict=judged.kind,
        first_diff_clock=judged.first_diff_clock,
        detail=judged.detail,
        prescreen={
            "latches": screen.latches,
            "comb_loops": screen.comb_loops,
            "derived_clocks": screen.derived_clocks,
            "flagged": screen.flagged,
            "sv_retried": screen.sv_retried,
            "log": str(screen.log),
        },
        reference_source=source,
        reference_path=str(ref_path),
        reference_sha256=hashlib.sha256(samples).hexdigest(),
        clocks=clocks,
        seconds=round(time.monotonic() - began, 1),
    )
    _write(out / RESULT_NAME, result.to_json())
    return result


# pool-sim sub-command -----------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def add_parser(sub) -> None:
    """Register `pool-sim` on the CLI's sub-parsers."""
    p = sub.add_parser(
        "pool-sim",
        help="run every pool design through the simulated compare build; print a table",
        description=(
            "For each design of the first test pool: fetch, pre-screen, simulate the compare "
            "top in Verilator and compare the frame with the shared reference (the local one "
            "if there is none). Exit 0 if all are exact, 1 if any differs, 2 if any run "
            "failed or was inconclusive."
        ),
    )
    p.add_argument(
        "--clocks", type=_positive_int, default=DEFAULT_CLOCKS, help="project clocks per design"
    )
    p.add_argument("--jobs", type=_positive_int, default=1, help="designs simulated at once")
    p.add_argument("--work", type=Path, default=DEFAULT_WORK, help="default: <repo>/work/pool")
    p.set_defaults(func=_cmd_pool_sim)


def _row(doc: dict) -> str:
    screen = doc.get("prescreen") or {}
    flags = (
        "-"
        if not screen
        else f"{screen['latches']}/{screen['comb_loops']}/{screen['derived_clocks']}"
    )
    diff = doc.get("first_diff_clock")
    return (
        f"{doc['id']:<44} {doc['verdict']:<16} {'-' if diff is None else diff:<10} "
        f"{flags:<9} {doc.get('reference_source') or '-':<6} {doc.get('seconds', 0):>7.1f}"
    )


def _cmd_pool_sim(args: argparse.Namespace) -> int:
    work = Path(args.work)
    docs: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(run_sim, pid, args.clocks, work): pid for pid in POOL}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                doc = future.result().to_json()
            except _RUN_FAILURES as e:  # a failed run is a row, and the others still run
                message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
                doc = {
                    "id": pid,
                    "verdict": "error",
                    "first_diff_clock": None,
                    "detail": f"{type(e).__name__}: {' '.join(str(message).split())}",
                    "prescreen": None,
                    "reference_source": None,
                    "clocks": args.clocks,
                }
                _write(work / pid / RESULT_NAME, doc)
            docs[pid] = doc
            text = f"tt-harness pool-sim: {pid}: {doc['verdict']}"
            if doc["detail"]:
                text += f": {doc['detail']}"
            print(text, file=sys.stderr, flush=True)
    results = [docs[pid] for pid in POOL]
    _write(work / SUMMARY_NAME, {"clocks": args.clocks, "results": results})
    print(
        f"{'design':<44} {'verdict':<16} {'first diff':<10} {'l/loop/dc':<9} {'ref':<6} "
        f"{'seconds':>7}"
    )
    for doc in results:
        print(_row(doc))
    print(f"l/loop/dc: latches / combinational loops / derived clocks; clocks {args.clocks}")
    return max((_EXIT[doc["verdict"]] for doc in results), default=0)
