# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point.  Later tasks add sub-commands via `add_subparsers`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from tt_harness import __version__, fetch, manifest, prescreen, verdict
from tt_harness.host import frame
from tt_harness.tools import REPO


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tt-harness",
        description="Run Tiny Tapeout VGA designs on handheld-console FPGA boards.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    fetch_p = sub.add_parser("fetch", help="fetch a design's sources and print the source root")
    fetch_p.add_argument("project", metavar="SHUTTLE/MACRO")
    fetch_p.add_argument(
        "--videos-repo",
        type=Path,
        default=None,
        help="tinytapeout-vga-videos checkout (default: $TT_VIDEOS_REPO)",
    )
    fetch_p.add_argument(
        "--cache", type=Path, default=REPO / "work" / "sources", help="default: <repo>/work/sources"
    )
    fetch_p.set_defaults(func=_cmd_fetch)

    verdict_p = sub.add_parser(
        "verdict",
        help="compare a compare-build UART recording with the shared reference; print JSON",
        description=(
            "Decode a recording (raw UART bytes), pre-screen the design and compare the "
            "recording with the reference clock by clock. Exit 0 for exact, 1 for mismatch "
            "or nondeterministic, 2 for inconclusive or any error."
        ),
    )
    verdict_p.add_argument("project", metavar="SHUTTLE/MACRO")
    verdict_p.add_argument("recording", type=Path, metavar="RECORDING")
    verdict_p.add_argument(
        "--videos-repo",
        type=Path,
        default=None,
        help="tinytapeout-vga-videos checkout (default: $TT_VIDEOS_REPO)",
    )
    verdict_p.add_argument(
        "--cache", type=Path, default=REPO / "work" / "sources", help="default: <repo>/work/sources"
    )
    verdict_p.add_argument(
        "--reference-dir",
        type=Path,
        default=None,
        help="shared reference files (default: $TT_REFERENCE_DIR)",
    )
    verdict_p.set_defaults(func=_cmd_verdict)
    return parser


def _cmd_fetch(args: argparse.Namespace) -> int:
    try:
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        sources = fetch.fetch(manifest.get(projects, args.project), args.cache)
    except (KeyError, FileNotFoundError, fetch.FetchError) as e:
        print(f"tt-harness fetch: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
        return 1
    for warning in sources.warnings:
        print(f"tt-harness fetch: warning: {warning}", file=sys.stderr)
    print(sources.root)
    return 0


_VERDICT_EXIT = {"exact": 0, "mismatch": 1, "nondeterministic": 1, "inconclusive": 2}


def _cmd_verdict(args: argparse.Namespace) -> int:
    try:
        rec = frame.decode(args.recording.read_bytes())
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        project = manifest.get(projects, args.project)
        sources = fetch.fetch(project, args.cache, args.videos_repo)
        screen = prescreen.check(sources)
        ref = verdict.load_reference(
            args.reference_dir or verdict.default_reference_dir(), project.id
        )
    except (
        KeyError,
        OSError,
        ValueError,
        fetch.FetchError,
        prescreen.PrescreenError,
    ) as e:  # ReferenceInvalid is a ValueError
        message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        print(f"tt-harness verdict: {' '.join(str(message).split())}", file=sys.stderr)
        return 2
    result = verdict.compare(rec, ref.samples[: ref.clocks], screen.flagged)
    doc = {
        "id": project.id,
        "verdict": result.kind,
        "first_diff_clock": result.first_diff_clock,
        "detail": result.detail,
        "clocks": ref.clocks,
        "recording": {
            "clocks": rec.clocks,
            "received": len(rec.samples),
            "ok": rec.ok,
            "problem": rec.problem,
        },
        "prescreen": {
            "latches": screen.latches,
            "comb_loops": screen.comb_loops,
            "derived_clocks": screen.derived_clocks,
            "flagged": screen.flagged,
            "sv_retried": screen.sv_retried,
            "log": str(screen.log),
        },
        "reference": {"path": str(ref.path), "sha256": ref.sha256},
    }
    print(json.dumps(doc))
    return _VERDICT_EXIT[result.kind]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
