# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point.  Later tasks add sub-commands via `add_subparsers`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from tt_harness import __version__, fetch, manifest
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
