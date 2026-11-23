# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point.  Later tasks add sub-commands via `add_subparsers`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from tt_harness import __version__, build, fetch, manifest, prescreen, reference, verdict
from tt_harness.core.stimulus import RESET_CLOCKS
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

    ref_p = sub.add_parser(
        "reference", help="simulate a design with Verilator and dump uo_out once per clock"
    )
    ref_p.add_argument("project", metavar="SHUTTLE/MACRO")
    ref_p.add_argument("--clocks", type=_positive_int, required=True, help="clocks to simulate")
    ref_p.add_argument(
        "--videos-repo",
        type=Path,
        default=None,
        help="tinytapeout-vga-videos checkout (default: $TT_VIDEOS_REPO)",
    )
    ref_p.add_argument(
        "--cache", type=Path, default=REPO / "work" / "sources", help="default: <repo>/work/sources"
    )
    ref_p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="default: <repo>/work/reference/<shuttle>/<macro>",
    )
    ref_p.set_defaults(func=_cmd_reference)

    bc_p = sub.add_parser(
        "build-compare", help="build the compare bitstream for a design and check its timing"
    )
    bc_p.add_argument("project", metavar="SHUTTLE/MACRO")
    bc_p.add_argument("--board", required=True, choices=sorted(build.BOARDS))
    bc_p.add_argument("--toolchain", required=True, choices=build.TOOLCHAINS)
    bc_p.add_argument(
        "--clocks", type=_positive_int, default=6_000_000, help="project clocks to record"
    )
    bc_p.add_argument("--baud", type=_positive_int, default=1_000_000, help="UART baud rate")
    bc_p.add_argument(
        "--vivado-nettype-rewrite",
        action="store_true",
        help="let Vivado read copies with `default_nettype wire` if it rejects the sources",
    )
    bc_p.add_argument(
        "--jobs", type=_positive_int, default=build.MAX_JOBS, help="cores the tools may use"
    )
    bc_p.add_argument(
        "--videos-repo",
        type=Path,
        default=None,
        help="tinytapeout-vga-videos checkout (default: $TT_VIDEOS_REPO)",
    )
    bc_p.add_argument(
        "--cache", type=Path, default=REPO / "work" / "sources", help="default: <repo>/work/sources"
    )
    bc_p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="default: <repo>/build/compare/<board>-<toolchain>/<shuttle>/<macro>",
    )
    bc_p.set_defaults(func=_cmd_build_compare)

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


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


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


def _cmd_reference(args: argparse.Namespace) -> int:
    try:
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        project = manifest.get(projects, args.project)
        sources = fetch.fetch(project, args.cache)
        out_dir = args.out_dir or REPO / "work" / "reference" / project.shuttle / project.macro
        dump = reference.simulate(sources, args.clocks, out_dir)
    except (KeyError, FileNotFoundError, fetch.FetchError, reference.ReferenceError) as e:
        print(
            f"tt-harness reference: {e.args[0] if isinstance(e, KeyError) else e}",
            file=sys.stderr,
        )
        return 1
    print(dump)
    return 0


def _cmd_build_compare(args: argparse.Namespace) -> int:
    step = "manifest"
    try:
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        project = manifest.get(projects, args.project)
        step = "fetch"
        sources = fetch.fetch(project, args.cache)
        for warning in sources.warnings:
            print(f"tt-harness build-compare: warning: {warning}", file=sys.stderr)
        out_dir = args.out_dir or (
            REPO
            / "build"
            / "compare"
            / f"{args.board}-{args.toolchain}"
            / project.shuttle
            / project.macro
        )
        step = "build"
        result = build.build_compare(
            project,
            sources,
            args.board,
            args.toolchain,
            out_dir,
            args.clocks,
            baud=args.baud,
            jobs=args.jobs,
            vivado_nettype_rewrite=args.vivado_nettype_rewrite,
        )
    except build.TimingError as e:
        print(f"tt-harness build-compare: {args.project}: timing: {e}", file=sys.stderr)
        return 1
    except (
        KeyError,
        ValueError,
        FileNotFoundError,
        fetch.FetchError,
        build.BuildError,
    ) as e:
        text = e.args[0] if isinstance(e, KeyError) else e
        print(f"tt-harness build-compare: {args.project}: {step}: {text}", file=sys.stderr)
        return 1
    for name, d in sorted(result.timing.items()):
        print(
            f"{name}: {d.achieved_mhz:.2f} MHz achieved, constraint {d.constraint_mhz:.2f} MHz, "
            f"{'met' if d.met else 'NOT MET'}",
            file=sys.stderr,
        )
    print(f"sources read as {result.language}", file=sys.stderr)
    for path in result.source_rewrites:
        print(f"warning: {path} was given to {args.toolchain} modified", file=sys.stderr)
    print(result.bitstream)
    return 0


_VERDICT_EXIT = {"exact": 0, "mismatch": 1, "nondeterministic": 1, "inconclusive": 2}


def _check_stimulus(ref: verdict.Reference, project: manifest.Project) -> None:
    """Refuse a reference made under different inputs from those the compare build applies.

    A difference there would show up as a `mismatch` that is not the design's or the
    harness's fault, so it is an error instead.
    """
    for name, ref_value, build_value in (
        ("ui_in", ref.ui_in, project.ui_in),
        ("uio_in", ref.uio_in, project.uio_in),
    ):
        if ref_value != build_value:
            raise ValueError(
                f"{project.id}: reference {name} 0x{ref_value:02x} differs from the "
                f"compare build's 0x{build_value:02x} ({ref.path})"
            )
    if ref.reset_clocks != RESET_CLOCKS:
        raise ValueError(
            f"{project.id}: reference reset_clocks {ref.reset_clocks} differs from the "
            f"compare build's {RESET_CLOCKS} ({ref.path})"
        )


def _cmd_verdict(args: argparse.Namespace) -> int:
    try:
        # The reference first: a missing or invalid one is reported before fetch and Yosys.
        ref = verdict.load_reference(
            args.reference_dir or verdict.default_reference_dir(), args.project
        )
        rec = frame.decode(args.recording.read_bytes())
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        project = manifest.get(projects, args.project)
        _check_stimulus(ref, project)
        sources = fetch.fetch(project, args.cache, args.videos_repo)
        screen = prescreen.check(sources)
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
