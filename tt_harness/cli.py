# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point.  Later tasks add sub-commands via `add_subparsers`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from tt_harness import __version__, build, fetch, manifest, prescreen, reference, verdict
from tt_harness.compare.recorder import frame_length
from tt_harness.core.stimulus import RESET_CLOCKS
from tt_harness.host import frame
from tt_harness.host import run as hw
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
        "--no-vivado-nettype-rewrite",
        dest="vivado_nettype_rewrite",
        action="store_false",
        help="fail instead of letting Vivado read `default_nettype wire` copies (Synth 8-6735)",
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

    rc_p = sub.add_parser(
        "run-compare",
        help="build, run on a real board, record and compare with the reference; print JSON",
        description=(
            "Build the compare bitstream (or reuse the one already built for the same "
            "settings), load it on a board through its host over SSH, record the UART frame "
            "and print the verdict JSON. Exit codes as `verdict`; a busy board, a failed "
            "build or a failed run is an error (2)."
        ),
    )
    rc_p.add_argument("project", metavar="SHUTTLE/MACRO")
    rc_p.add_argument("--host", required=True, help="the board host (SSH)")
    rc_p.add_argument("--port", type=_positive_int, required=True, help="its SSH port")
    rc_p.add_argument("--uart", required=True, help="the board's /dev/serial/by-id/... path")
    rc_p.add_argument("--user", default="pi", help="SSH user (default: pi)")
    rc_p.add_argument("--board", default="arty", choices=sorted(build.BOARDS))
    rc_p.add_argument("--toolchain", default="vivado", choices=build.TOOLCHAINS)
    rc_p.add_argument(
        "--clocks", type=_positive_int, default=6_000_000, help="project clocks to record"
    )
    rc_p.add_argument("--baud", type=_positive_int, default=1_000_000, help="UART baud rate")
    rc_p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="seconds the receiver waits for the frame (default: from --clocks and --baud)",
    )
    rc_p.add_argument("--rebuild", action="store_true", help="build even if a build exists")
    rc_p.add_argument(
        "--known-hosts",
        type=Path,
        default=None,
        help="accept only host keys in this file (default: as the user's ssh configuration)",
    )
    rc_p.add_argument(
        "--jobs", type=_positive_int, default=build.MAX_JOBS, help="cores the tools may use"
    )
    rc_p.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        help="default: <repo>/build/compare/<board>-<toolchain>/<shuttle>/<macro>",
    )
    rc_p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="the recording (default: <repo>/work/runs/<shuttle>/<macro>/<toolchain>.bin)",
    )
    rc_p.add_argument(
        "--videos-repo",
        type=Path,
        default=None,
        help="tinytapeout-vga-videos checkout (default: $TT_VIDEOS_REPO)",
    )
    rc_p.add_argument(
        "--cache", type=Path, default=REPO / "work" / "sources", help="default: <repo>/work/sources"
    )
    rc_p.add_argument(
        "--reference-dir",
        type=Path,
        default=None,
        help="shared reference files (default: $TT_REFERENCE_DIR)",
    )
    rc_p.set_defaults(func=_cmd_run_compare)
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
    # build_compare itself warns on stderr about rewritten sources and disagreements.
    for name, d in sorted(result.timing.items()):
        print(f"{name}: {build.describe(d)}, {'met' if d.met else 'NOT MET'}", file=sys.stderr)
    print(f"sources read as {result.language}", file=sys.stderr)
    for item in result.limitations:
        print(f"limitation: {item}", file=sys.stderr)
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
    doc = _verdict_doc(project, ref, rec, screen)
    print(json.dumps(doc))
    return _VERDICT_EXIT[doc["verdict"]]


def _verdict_doc(
    project: manifest.Project,
    ref: verdict.Reference,
    rec: frame.Recording,
    screen: prescreen.Prescreen,
) -> dict:
    result = verdict.compare(rec, ref.samples[: ref.clocks], screen.flagged)
    return {
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


def _existing_build(
    project: manifest.Project, board: str, clocks: int, baud: int, out_dir: Path
) -> Path | None:
    """The bitstream in `out_dir` if it was built for exactly these settings, else None.

    A build is taken as matching when the board top generated now, which carries the
    project, its inputs, `clocks` and the baud divider, is byte for byte the one in
    `out_dir`, and the bitstream there is not older than it. `build_compare` leaves no
    bitstream behind when a build fails.
    """
    board_mod = build.BOARDS[board]
    top_v = out_dir / f"{board_mod.MODULE_NAME}.v"
    bitstream = out_dir / f"{board_mod.MODULE_NAME}.bit"
    if not top_v.is_file() or not bitstream.is_file():
        return None
    if bitstream.stat().st_mtime < top_v.stat().st_mtime:
        return None
    fresh = out_dir / f"{board_mod.MODULE_NAME}.check.v"
    try:
        top = board_mod.Top(project, baud, clocks, settle=build.SETTLE)
        same = board_mod.to_verilog(top, fresh).read_bytes() == top_v.read_bytes()
    finally:
        fresh.unlink(missing_ok=True)
    return bitstream if same else None


def _cmd_run_compare(args: argparse.Namespace) -> int:
    step = "board"
    try:
        board = hw.Board(host=args.host, port=args.port, uart=args.uart, user=args.user)
        hw.check_board(board)
        step = "reference"
        # The reference first, as `verdict` does: without one there is nothing to run for.
        ref = verdict.load_reference(
            args.reference_dir or verdict.default_reference_dir(), args.project
        )
        step = "manifest"
        projects = manifest.load(args.videos_repo or manifest.default_videos_repo())
        project = manifest.get(projects, args.project)
        _check_stimulus(ref, project)
        if args.clocks < ref.clocks:
            raise ValueError(
                f"{project.id}: --clocks {args.clocks} is less than the reference span "
                f"{ref.clocks}; the recording could not cover it"
            )
        step = "fetch"
        sources = fetch.fetch(project, args.cache, args.videos_repo)
        step = "pre-screen"
        screen = prescreen.check(sources)
        step = "build"
        build_dir = args.build_dir or (
            REPO
            / "build"
            / "compare"
            / f"{args.board}-{args.toolchain}"
            / project.shuttle
            / project.macro
        )
        bitstream = None
        if not args.rebuild:
            bitstream = _existing_build(project, args.board, args.clocks, args.baud, build_dir)
        reused = bitstream is not None
        if bitstream is None:
            bitstream = build.build_compare(
                project,
                sources,
                args.board,
                args.toolchain,
                build_dir,
                args.clocks,
                baud=args.baud,
                jobs=args.jobs,
            ).bitstream
        print(
            f"tt-harness run-compare: {'reusing' if reused else 'built'} {bitstream}",
            file=sys.stderr,
        )
        step = "run"
        out = args.out or (
            REPO / "work" / "runs" / project.shuttle / project.macro / f"{args.toolchain}.bin"
        )
        # Ten bit times per byte, a quarter more, and time for the load.
        frame_s = frame_length(args.clocks) * 10 / args.baud
        timeout_s = args.timeout if args.timeout is not None else frame_s * 1.25 + 30
        runner = hw.SshRunner(board, known_hosts=args.known_hosts)
        hw.record(board, bitstream, args.clocks, args.baud, out, timeout_s, runner=runner)
        step = "decode"
        data = out.read_bytes()
        rec = frame.decode(data)
        meta = json.loads(hw.meta_path(out).read_text())
    except hw.BoardBusy as e:
        print(f"tt-harness run-compare: {args.project}: board busy:", file=sys.stderr)
        for reason in e.reasons:
            print(f"  {reason}", file=sys.stderr)
        return 2
    except build.TimingError as e:
        print(f"tt-harness run-compare: {args.project}: timing: {e}", file=sys.stderr)
        return 2
    except (
        KeyError,
        OSError,
        ValueError,
        fetch.FetchError,
        prescreen.PrescreenError,
        build.BuildError,
        hw.RunError,
    ) as e:
        message = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        text = " ".join(str(message).split())
        print(f"tt-harness run-compare: {args.project}: {step}: {text}", file=sys.stderr)
        return 2
    doc = _verdict_doc(project, ref, rec, screen)
    receiver = meta.get("receiver", {})
    doc["run"] = {
        "host": board.host,
        "port": board.port,
        "uart": board.uart,
        "board": args.board,
        "toolchain": args.toolchain,
        "baud": args.baud,
        "bitstream": str(bitstream),
        "bitstream_sha256": meta["bitstream_sha256"],
        "bitstream_reused": reused,
        "recording": str(out),
        "bytes_received": len(data),
        "load_s": meta.get("load_s"),
        "receive_s": meta.get("receive_s"),
        "receiver_stopped": receiver.get("stopped"),
        "uart_errors": _uart_errors(receiver),
    }
    print(json.dumps(doc))
    return _VERDICT_EXIT[doc["verdict"]]


def _uart_errors(receiver: dict) -> dict | None:
    """How far the kernel's serial error counters moved during the reception, if known."""
    before, after = receiver.get("icount_before"), receiver.get("icount_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    return {
        name: after[name] - before[name]
        for name in ("frame", "overrun", "parity", "brk", "buf_overrun")
        if name in before and name in after
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
