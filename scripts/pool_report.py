# SPDX-License-Identifier: Apache-2.0
"""Print the pool results under `work/pool/` as Markdown tables.

    uv run python scripts/pool_report.py [sim|build|timing|limits|hw ...]

Reads what `tt-harness pool-sim` (`result.json`) and `scripts/pool_run.py`
(`build-<toolchain>.json`, `hw-<toolchain>.json`) wrote. The tables of
`docs/results/2026-11-spike-a-compare-build.md` are this script's output.
"""

from __future__ import annotations

import json
import sys

from tt_harness import build, pool

WORK = pool.DEFAULT_WORK
TOOL = {"openxc7": "openXC7", "vivado": "Vivado"}
STANDING = (build.LIMIT_NO_HOLD, build.LIMIT_CROSS_BUDGET)


def _load(pid: str, name: str) -> dict | None:
    path = WORK / pid / name
    return json.loads(path.read_text()) if path.is_file() else None


def _short(pid: str) -> str:
    return f"`{pid.split('/')[1].removeprefix('tt_um_')}`"


def _table(head: list[str], rows: list[list]) -> None:
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for row in rows:
        print("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
    print()


def _num(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def sim() -> None:
    rows = []
    for pid in pool.POOL:
        d = _load(pid, "result.json")
        if d is None:
            rows.append([f"`{pid}`", "not run"])
            continue
        s = d["prescreen"] or {}
        diff = d["first_diff_clock"]
        rows.append(
            [
                f"`{pid}`",
                f"**{d['verdict']}**",
                "none" if diff is None else diff,
                s.get("latches"),
                s.get("comb_loops"),
                s.get("derived_clocks"),
                "yes" if s.get("flagged") else "no",
                "yes" if s.get("sv_retried") else "no",
                d["reference_source"],
                d["clocks"],
            ]
        )
    _table(
        [
            "Design",
            "Verdict",
            "First differing clock",
            "Latches",
            "Comb. loops",
            "Derived clocks",
            "Flagged",
            "Read as SystemVerilog",
            "Reference",
            "Clocks",
        ],
        rows,
    )


def builds() -> None:
    for tc in build.TOOLCHAINS:
        print(f"### {TOOL[tc]}\n")
        rows = []
        for pid in pool.POOL:
            d = _load(pid, f"build-{tc}.json")
            if d is None:
                rows.append([_short(pid), "not built"])
                continue
            use = d.get("use") or {}
            sites = use.get("sites", {})
            placed = (
                f"{sites.get('SLICE_LUTX', ['?'])[0]} / {sites.get('SLICE_FFX', ['?'])[0]}"
                if tc == "openxc7"
                else f"{sites.get('Slice', ['?'])[0]} slices"
            )
            row = [
                _short(pid),
                "ok" if d["ok"] else f"**failed** ({d['error_kind']})",
                use.get("luts"),
                use.get("ffs"),
                use.get("cells", {}).get("CARRY4", 0),
                use.get("cells", {}).get("DSP48E1", 0),
                placed,
                f"{d['seconds']:.0f} s",
            ]
            if d["ok"]:
                rewrites = ", ".join(f"`{n}` ({c})" for n, c in d["source_rewrites"]) or "none"
                row += [
                    d["language"],
                    ", ".join(f"`{c}`" for c in d["project_clocks"]) or "none",
                    rewrites,
                    f"`{d['bitstream_sha256'][:12]}`",
                ]
            rows.append(row)
        placed_head = "Placed LUT / FF sites" if tc == "openxc7" else "Occupied slices"
        _table(
            [
                "Design",
                "Build",
                "LUTs",
                "FFs",
                "CARRY4",
                "DSP48E1",
                placed_head,
                "Time",
                "Sources read as",
                "Project clocks",
                "`default_nettype` rewrites",
                "Bitstream sha256",
            ],
            rows,
        )


def timing() -> None:
    for pid in pool.POOL:
        print(f"### `{pid}`\n")
        rows = []
        for tc in build.TOOLCHAINS:
            d = _load(pid, f"build-{tc}.json")
            if d is None or not d["ok"]:
                rows.append(
                    [TOOL[tc], "no timing result: " + ("not built" if d is None else "failed")]
                )
                continue
            for key, t in d["timing"].items():
                cross = t["kind"] == "cross"
                rows.append(
                    [
                        TOOL[tc],
                        f"`{key}`",
                        t["kind"],
                        t["root_kind"] or "-",
                        "yes" if t["signed_off"] else "**no**",
                        "yes" if t["half_cycle"] else "no",
                        "-" if cross else _num(t["constraint_mhz"]),
                        "-" if cross else _num(t["achieved_mhz"]),
                        _num(t["requirement_ns"], 3),
                        _num(t["setup_slack_ns"], 3),
                        _num(t["hold_slack_ns"], 3),
                        "met" if t["met"] else "**NOT MET**",
                    ]
                )
        _table(
            [
                "Toolchain",
                "Domain",
                "kind",
                "root_kind",
                "signed_off",
                "half_cycle",
                "Constraint MHz",
                "Achieved MHz",
                "Requirement ns",
                "Setup slack ns",
                "Hold slack ns",
                "Result",
            ],
            rows,
        )


def limits() -> None:
    rows = []
    for pid in pool.POOL:
        for tc in build.TOOLCHAINS:
            d = _load(pid, f"build-{tc}.json")
            if d is None:
                continue
            if not d["ok"]:
                rows.append([_short(pid), TOOL[tc], "-", f"build failed ({d['error_kind']})"])
                continue
            own = [x for x in d["limitations"] if x not in STANDING]
            standing = sum(1 for x in d["limitations"] if x in STANDING)
            text = "; ".join(f'"{x}"' for x in own) or "none"
            rows.append([_short(pid), TOOL[tc], standing, text])
    _table(["Design", "Toolchain", "Standing entries", "Further entries, verbatim"], rows)


def hw() -> None:
    rows = []
    for pid in pool.POOL:
        ref = None
        for tc in build.TOOLCHAINS:
            d = _load(pid, f"hw-{tc}.json")
            if d is None:
                rows.append([_short(pid), TOOL[tc], "not run"])
                continue
            run = d.get("run") or {}
            rec = d.get("recording") or {}
            errors = run.get("uart_errors")
            ref = (d.get("reference") or {}).get("sha256")
            same = d.get("payload_sha256") == ref if ref else None
            diff = d.get("first_diff_clock")
            rows.append(
                [
                    _short(pid),
                    TOOL[tc],
                    f"**{d['verdict']}**",
                    "none" if diff is None else diff,
                    run.get("bytes_received"),
                    {True: "good", False: f"bad: {rec.get('problem')}"}.get(rec.get("ok"), "?"),
                    "?" if errors is None else ("all 0" if not any(errors.values()) else errors),
                    {True: "equal", False: "differs", None: "?"}[same],
                    _num(run.get("load_s")),
                    _num(run.get("receive_s")),
                    _num(d.get("seconds"), 1),
                    d.get("started", "")[11:19],
                    f"`{run.get('bitstream_sha256', '')[:12]}`",
                ]
            )
    _table(
        [
            "Design",
            "Toolchain",
            "Verdict",
            "First differing clock",
            "Bytes received",
            "Frame CRC",
            "UART error counters",
            "Payload sha256 against reference",
            "Load s",
            "Load to last byte s",
            "Whole command s",
            "Started (local)",
            "Bitstream sha256",
        ],
        rows,
    )


SECTIONS = {"sim": sim, "build": builds, "timing": timing, "limits": limits, "hw": hw}


def main() -> int:
    for name in sys.argv[1:] or list(SECTIONS):
        print(f"<!-- {name} -->\n")
        SECTIONS[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
