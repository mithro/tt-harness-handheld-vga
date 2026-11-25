# SPDX-License-Identifier: Apache-2.0
"""Build the compare bitstream of every pool design and run each on a real board.

    uv run python scripts/pool_run.py build [--toolchain T ...] [ID ...]
    uv run python scripts/pool_run.py hw --host H --port P --uart U --known-hosts F \\
        [--toolchain T ...] [ID ...]

`build` calls `tt_harness.build.build_compare` (the code behind `tt-harness build-compare`,
timing gate included) for each design and toolchain, one build at a time, into the default
directory `build/compare/arty-<toolchain>/<shuttle>/<macro>/`, and writes everything the
build reported to `work/pool/<shuttle>/<macro>/build-<toolchain>.json`: timing per domain,
limitations, rewritten sources, resource use read from the tools' own reports, or the exact
error of a build that failed. A failed build is a result; nothing is retried or relaxed.

`hw` runs `tt-harness run-compare` for each design and toolchain whose build succeeded, one
run at a time, reusing those bitstreams, and writes the verdict document to
`work/pool/<shuttle>/<macro>/hw-<toolchain>.json` (the recording beside it as `.bin`, the
command's stderr as `.log`). It stops at the first run refused because the board is busy.

Both print one line per step with the time remaining and the expected finish time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from tt_harness import build, fetch, manifest, pool, prescreen
from tt_harness.host import frame
from tt_harness.tools import REPO

BOARD = "arty"
CLOCKS = 6_000_000
WORK = pool.DEFAULT_WORK
# Seconds, for the first estimate only; later ones use what was measured.
FIRST_GUESS_S = {"openxc7": 90.0, "vivado": 270.0, "hw": 90.0}

_NEXTPNR_USE = re.compile(r"^Info:\s+(?P<name>[A-Z0-9_]+):\s*(?P<used>\d+)/\s*(?P<of>\d+)\s+\d+%")
_YOSYS_CELL = re.compile(r"^\s+(?P<count>\d+)\s+(?P<name>[A-Z][A-Z0-9_]*)\s*$")
_VIVADO_ROW = re.compile(r"^\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<used>\d+)\s*\|(?P<rest>.*)\|\s*$")
_VIVADO_SITES = (
    "Slice LUTs",
    "LUT as Logic",
    "LUT as Memory",
    "Slice Registers",
    "Register as Flip Flop",
    "Register as Latch",
    "F7 Muxes",
    "F8 Muxes",
    "Slice",
    "Block RAM Tile",
    "DSPs",
    "BUFGCTRL",
)
_VIVADO_PRIMITIVES = re.compile(r"^(LUT\d|FD[A-Z]+|LD[A-Z]+|CARRY4|MUXF\d|BUFG|RAMB\w+|DSP\w+)$")


def _say(text: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {text}", flush=True)


def _eta(done_s: list[float], left: int, guess: float) -> str:
    each = sum(done_s) / len(done_s) if done_s else guess
    remaining = each * left
    finish = datetime.now() + timedelta(seconds=remaining)
    return f"about {remaining / 60:.0f} min left, finishing near {finish.strftime('%H:%M')}"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _build_dir(project: manifest.Project, toolchain: str) -> Path:
    return REPO / "build" / "compare" / f"{BOARD}-{toolchain}" / project.shuttle / project.macro


# Resource use, from the tools' own reports -------------------------------------------------


def openxc7_use(out_dir: Path) -> dict:
    """Cells after Yosys synthesis, and nextpnr-xilinx's device utilisation after packing."""
    cells: dict[str, int] = {}
    stat = out_dir / "utilization-yosys.txt"
    if stat.is_file():
        for line in stat.read_text(errors="replace").splitlines():
            m = _YOSYS_CELL.match(line)
            if m:
                cells[m.group("name")] = int(m.group("count"))
    sites: dict[str, list[int]] = {}
    log = out_dir / "nextpnr.log"
    if log.is_file():
        for line in log.read_text(errors="replace").splitlines():
            m = _NEXTPNR_USE.match(line)
            if m and int(m.group("used")):
                # The last seed's figures stand; packing is the same for every seed.
                sites[m.group("name")] = [int(m.group("used")), int(m.group("of"))]
    return {
        "source": "utilization-yosys.txt (cells) and nextpnr.log (Device utilisation)",
        "luts": sum(n for name, n in cells.items() if re.fullmatch(r"LUT\d", name)),
        "ffs": sum(n for name, n in cells.items() if re.fullmatch(r"FD[A-Z]+(_1)?", name)),
        "latches": sum(n for name, n in cells.items() if re.fullmatch(r"LD[A-Z]+", name)),
        "cells": cells,
        "sites": sites,
    }


def vivado_use(out_dir: Path) -> dict:
    """Rows of Vivado's `report_utilization` after place and route."""
    sites: dict[str, list[int]] = {}
    cells: dict[str, int] = {}
    report = out_dir / "utilization.rpt"
    if report.is_file():
        for line in report.read_text(errors="replace").splitlines():
            m = _VIVADO_ROW.match(line)
            if not m:
                continue
            name, used = m.group("name"), int(m.group("used"))
            columns = [c.strip() for c in m.group("rest").split("|")]
            if name in _VIVADO_SITES and name not in sites:
                available = next((int(c) for c in columns[1:] if c.isdigit() and int(c)), 0)
                sites[name] = [used, available]
            elif _VIVADO_PRIMITIVES.match(name):
                cells[name] = used
    return {
        "source": "utilization.rpt (report_utilization after route)",
        "luts": sites.get("Slice LUTs", [None])[0],
        "ffs": sites.get("Register as Flip Flop", sites.get("Slice Registers", [None]))[0],
        "latches": sites.get("Register as Latch", [None])[0],
        "cells": cells,
        "sites": sites,
    }


USE = {"openxc7": openxc7_use, "vivado": vivado_use}


# build ---------------------------------------------------------------------------------------


def build_one(project_id: str, toolchain: str, clocks: int, jobs: int) -> dict:
    doc: dict = {"id": project_id, "toolchain": toolchain, "board": BOARD, "clocks": clocks}
    doc["started"] = datetime.now().astimezone().isoformat(timespec="seconds")
    began = time.monotonic()
    out_dir = None
    try:
        projects = manifest.load(manifest.default_videos_repo())
        project = manifest.get(projects, project_id)
        sources = fetch.fetch(project, REPO / "work" / "sources")
        out_dir = _build_dir(project, toolchain)
        doc["build_dir"] = str(out_dir)
        doc["log"] = str(out_dir / build.LOG_NAME)
        screen = prescreen.check(sources, WORK / project.shuttle / project.macro / "prescreen")
        doc["prescreen"] = {
            "latches": screen.latches,
            "comb_loops": screen.comb_loops,
            "derived_clocks": screen.derived_clocks,
            "flagged": screen.flagged,
            "sv_retried": screen.sv_retried,
        }
        result = build.build_compare(project, sources, BOARD, toolchain, out_dir, clocks, jobs=jobs)
    except (build.BuildError, build.TimingError) as e:
        doc.update(ok=False, error_kind=type(e).__name__, error=str(e))
    except (KeyError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        doc.update(ok=False, error_kind=type(e).__name__, error=str(e))
    else:
        bits = result.bitstream.read_bytes()
        doc.update(
            ok=True,
            bitstream=str(result.bitstream),
            bitstream_bytes=len(bits),
            bitstream_sha256=hashlib.sha256(bits).hexdigest(),
            part=result.part,
            language=result.language,
            project_clocks=list(result.project_clocks),
            prescreen_derived_clocks=result.prescreen_derived_clocks,
            source_rewrites=[[Path(p).name, n] for p, n in result.source_rewrites],
            limitations=list(result.limitations),
            input_drivers=[list(item) for item in result.input_drivers],
            timing={key: asdict(d) for key, d in sorted(result.timing.items())},
            timing_lines=[
                f"{key}: {build.describe(d)}, met" for key, d in sorted(result.timing.items())
            ],
        )
    doc["seconds"] = round(time.monotonic() - began, 1)
    if out_dir is not None:
        # Also for a failed build: what was placed says whether it fitted.
        doc["use"] = USE[toolchain](out_dir)
    return doc


def cmd_build(args: argparse.Namespace) -> int:
    steps = [(pid, tc) for tc in args.toolchain for pid in args.ids]
    taken: dict[str, list[float]] = {tc: [] for tc in args.toolchain}
    failed = 0
    for i, (pid, tc) in enumerate(steps):
        left = {t: sum(1 for _, x in steps[i:] if x == t) for t in args.toolchain}
        remaining = sum(
            (sum(taken[t]) / len(taken[t]) if taken[t] else FIRST_GUESS_S[t]) * n
            for t, n in left.items()
        )
        finish = datetime.now() + timedelta(seconds=remaining)
        _say(
            f"build {i + 1}/{len(steps)} {pid} {tc}: starting; about {remaining / 60:.0f} min "
            f"left, finishing near {finish.strftime('%H:%M')}"
        )
        doc = build_one(pid, tc, args.clocks, args.jobs)
        _write(WORK / pid / f"build-{tc}.json", doc)
        taken[tc].append(doc["seconds"])
        if doc["ok"]:
            _say(f"build {i + 1}/{len(steps)} {pid} {tc}: ok in {doc['seconds']:.0f} s")
        else:
            failed += 1
            _say(
                f"build {i + 1}/{len(steps)} {pid} {tc}: FAILED in {doc['seconds']:.0f} s: "
                f"{doc['error_kind']}: {' '.join(doc['error'].split())[:600]}"
            )
    _say(f"builds done: {len(steps) - failed} ok, {failed} failed")
    return 1 if failed else 0


# hw ------------------------------------------------------------------------------------------


def hw_one(pid: str, tc: str, args: argparse.Namespace) -> dict:
    base = WORK / pid / f"hw-{tc}"
    recording = base.with_suffix(".bin")
    for stale in (recording, Path(str(recording) + ".json"), base.with_suffix(".json")):
        stale.unlink(missing_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "tt_harness.cli",
        "run-compare",
        pid,
        "--toolchain",
        tc,
        "--clocks",
        str(args.clocks),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--uart",
        args.uart,
        "--known-hosts",
        str(args.known_hosts),
        "--out",
        str(recording),
    ]
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    began = time.monotonic()
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    base.with_suffix(".log").write_text(done.stderr)
    try:
        doc = json.loads(done.stdout) if done.stdout.strip() else {}
    except ValueError:
        doc = {}
    doc = {
        "id": pid,
        **doc,
        "toolchain": tc,
        "exit_status": done.returncode,
        "started": started,
        "seconds": round(time.monotonic() - began, 1),
        "stderr_tail": done.stderr.strip().splitlines()[-6:],
    }
    if "verdict" not in doc:
        doc["verdict"] = "error"
    if recording.is_file():
        # Independent of the verdict code: the payload's own SHA-256.
        rec = frame.decode(recording.read_bytes())
        doc["payload_sha256"] = hashlib.sha256(rec.samples).hexdigest()
        doc["measured"] = json.loads(Path(str(recording) + ".json").read_text())
    return doc


def cmd_hw(args: argparse.Namespace) -> int:
    steps = []
    for pid in args.ids:
        for tc in args.toolchain:
            built = WORK / pid / f"build-{tc}.json"
            if built.is_file() and json.loads(built.read_text()).get("ok"):
                steps.append((pid, tc))
            else:
                _say(f"hw {pid} {tc}: skipped, no successful build recorded in {built}")
    taken: list[float] = []
    worst = 0
    for i, (pid, tc) in enumerate(steps):
        _say(
            f"hw {i + 1}/{len(steps)} {pid} {tc}: starting; "
            f"{_eta(taken, len(steps) - i, FIRST_GUESS_S['hw'])}"
        )
        doc = hw_one(pid, tc, args)
        _write(WORK / pid / f"hw-{tc}.json", doc)
        taken.append(doc["seconds"])
        text = f"hw {i + 1}/{len(steps)} {pid} {tc}: {doc['verdict']} in {doc['seconds']:.0f} s"
        if doc["verdict"] != "exact":
            text += f": {doc.get('detail') or ' | '.join(doc['stderr_tail'])}"
        _say(text)
        worst = max(worst, doc["exit_status"])
        if any("board busy" in line for line in doc["stderr_tail"]):
            _say("the board is busy: stopping; nothing was uploaded for this run")
            return 2
    _say(f"hardware runs done: {len(steps)} run, worst exit status {worst}")
    return worst


def _terminated(signum: int, _frame) -> None:
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("build", cmd_build), ("hw", cmd_hw)):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="*", default=list(pool.POOL), metavar="SHUTTLE/MACRO")
        p.add_argument(
            "--toolchain", action="append", choices=build.TOOLCHAINS, help="default: both"
        )
        p.add_argument("--clocks", type=int, default=CLOCKS)
        p.set_defaults(func=func)
        if name == "build":
            p.add_argument("--jobs", type=int, default=build.MAX_JOBS)
        else:
            p.add_argument("--host", required=True)
            p.add_argument("--port", type=int, required=True)
            p.add_argument("--uart", required=True)
            p.add_argument("--known-hosts", type=Path, required=True)
    # A terminal line on every path (a usage error, an uncaught exception, an interrupt and
    # SIGTERM included), so that whoever watches the log can tell "ended" from "still
    # running". SIGTERM becomes SystemExit so that the `finally` runs.
    command = "?"
    rc: int | str = "exception"
    previous = signal.signal(signal.SIGTERM, _terminated)
    try:
        args = parser.parse_args(argv)
        command = args.command
        args.toolchain = args.toolchain or list(build.TOOLCHAINS)
        rc = args.func(args)
        return rc
    except KeyboardInterrupt:
        rc = "interrupted"
        raise
    except SystemExit as e:
        rc = f"exit {e.code}"
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)
        _say(f"pool_run {command} finished rc={rc}")


if __name__ == "__main__":
    sys.exit(main())
