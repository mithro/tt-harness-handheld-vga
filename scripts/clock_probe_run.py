# SPDX-License-Identifier: Apache-2.0
"""Build the Arty clock probe, load it on a real board and print what its UART reports.

    uv run python scripts/clock_probe_run.py A,B,C,D vivado,openxc7 \\
        --host ps1.fpgas.online --port 10722 \\
        --uart /dev/serial/by-id/usb-Digilent_Digilent_USB_Device_<serial>-if01-port0 \\
        --known-hosts tmp/known_hosts

For each setting and toolchain: build `tt_harness.boards.arty_clock_probe` (timing gate
included) or, with `--reuse`, take the bitstream already in `build/clock-probe/`; decode
what the bitstream programs into the MMCM; then, unless `--no-load`, on the board host:

1. the activity check of `tt_harness.host.run.board_activity` (a busy board is waited for,
   up to `--wait-busy-min`, and never interrupted);
2. a directory of its own under the user's home, the receiver script and the bitstream;
3. the receiver (`board_host.py receive`) started and listening, then `openFPGALoader`;
4. the capture fetched and checked, and the directory removed whatever happened.

The probe sends one line per 1 s gate, numbered from configuration. Gate 0 holds the MMCM's
lock time and is reported apart; the next `--gates` gates are the measurement. Lines of an
earlier bitstream, sent before the load, are recognised by the gate number starting again
at 0 and dropped. Results go to `work/clock-probe/<setting>-<toolchain>.json` (and the raw
capture beside it), and a table is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from tt_harness.boards import arty_clock_probe as probe
from tt_harness.boards import arty_clock_probe_build as probe_build
from tt_harness.host import run

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build" / "clock-probe"
RESULTS = REPO / "work" / "clock-probe"
HEADER = b"F "
SPARE_LINES = 4  # room for lines the previous bitstream sent before the load
BUSY_POLL_S = 180


# Building ---------------------------------------------------------------------------------------


def vivado_messages(out_dir: Path) -> dict:
    """The DRC violations and the warnings about the MMCM of a Vivado build."""
    drc = (out_dir / "drc.rpt").read_text()
    rules = re.findall(
        r"^\| (\w+-\d+)\s+\| (\w[\w ]*?)\s+\| (.*?)\s+\| +(\d+) +\|", drc, re.MULTILINE
    )
    log = (out_dir / "vivado.log").read_text()
    critical = sorted(set(re.findall(r"^CRITICAL WARNING: (.*)$", log, re.MULTILINE)))
    mmcm = sorted(
        {
            line
            for line in re.findall(r"^(?:WARNING|ERROR): (.*)$", log, re.MULTILINE)
            if re.search(r"MMCM|PLL|VCO|CLKOUT|CLKFBOUT|DIVCLK", line)
        }
    )
    return {
        "drc": [
            {"rule": r, "severity": s, "description": d, "count": int(n)} for r, s, d, n in rules
        ],
        "drc_errors": sum(int(n) for _, s, _, n in rules if s.lower() == "error"),
        "critical_warnings": critical,
        "mmcm_warnings": mmcm,
    }


VARIANTS = {
    "": "the build as the toolchain makes it",
    "fbfix": "open flow, FASM edited afterwards: CLKFBIN takes CLKFBOUT as in Vivado's bitstream",
    "zhold": "open flow, COMPENSATION set to ZHOLD in the pre-pack script",
}


def build_one(name: str, toolchain: str, reuse: bool, variant: str = "") -> dict:
    """Build (or reuse) one probe; return its facts, also written to `<dir>/probe.json`."""
    if variant and toolchain != "openxc7":
        raise ValueError("variants are of the open flow only")
    out_dir = BUILD / (f"{name}-{toolchain}" + (f"-{variant}" if variant else ""))
    facts_path = out_dir / "probe.json"
    if reuse and facts_path.is_file() and (out_dir / "top.bit").is_file():
        facts = json.loads(facts_path.read_text())
        bits = (out_dir / "top.bit").read_bytes()
        if facts["bitstream_sha256"] == hashlib.sha256(bits).hexdigest():
            return facts
    s = probe.setting(name)
    started = time.monotonic()
    extra = probe_build.PREPACK_ZHOLD if variant == "zhold" else ""
    job = probe_build.build(name, toolchain, out_dir, prepack_extra=extra)
    if variant == "fbfix":
        probe_build.rewrite_feedback(job)
    mmcm = probe_build.programmed_mmcm(job.bitstream)
    facts = {
        "setting": name,
        "toolchain": toolchain,
        "variant": variant,
        "feedback": probe_build.feedback_source(job.bitstream),
        "part": job.board.parts[toolchain],
        "divclk": s.divclk,
        "mult": s.mult,
        "div": s.div,
        "pfd_hz": s.clk_in_hz / s.divclk,
        "vco_hz": s.vco_hz,
        "achieved_hz": s.achieved_hz,
        "ideal_count": str(probe.ideal_count(s)),
        "duty_phases": probe.duty_phases(s),
        "build_s": round(time.monotonic() - started, 1),
        "bitstream": str(job.bitstream),
        "bitstream_bytes": job.bitstream.stat().st_size,
        "bitstream_sha256": hashlib.sha256(job.bitstream.read_bytes()).hexdigest(),
        "timing": {
            k: {
                "constraint_mhz": d.constraint_mhz,
                "achieved_mhz": d.achieved_mhz,
                "met": d.met,
                "setup_slack_ns": d.setup_slack_ns,
                "hold_slack_ns": d.hold_slack_ns,
            }
            for k, d in job.facts["domains"].items()
        },
        "mmcm": {k: mmcm.get(k, 0) for k in probe_build.MMCM_FIELDS},
        "mmcm_all": mmcm,
    }
    if toolchain == "vivado":
        facts["vivado"] = vivado_messages(out_dir)
    facts_path.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
    return facts


# Measuring --------------------------------------------------------------------------------------


def wait_until_free(board: run.Board, runner, wait_s: float, sleep=time.sleep) -> run.Activity:
    """The board's activity once it is not busy; `BoardBusy` if it stays busy for `wait_s`."""
    deadline = time.monotonic() + wait_s
    while True:
        activity = run.board_activity(board, runner)
        if not activity.busy:
            return activity
        if time.monotonic() + BUSY_POLL_S > deadline:
            raise run.BoardBusy(board, activity.reasons)
        print(f"board busy, checking again in {BUSY_POLL_S} s: {activity.reasons}", flush=True)
        sleep(BUSY_POLL_S)


def measure(board: run.Board, bitstream: Path, gates: int, runner, wait_busy_s: float = 0) -> dict:
    """Load `bitstream` and capture gate 0 and the `gates` gates after it from the UART."""
    bits = bitstream.read_bytes()
    activity = wait_until_free(board, runner, wait_busy_s)
    wanted = gates + 1
    length = (wanted + SPARE_LINES) * probe.LINE_BYTES
    timeout_s = wanted + SPARE_LINES + 20.0
    remote = f"/home/{board.user}/tt-harness-probe-{secrets.token_hex(6)}"
    script = f"{remote}/{run.REMOTE_SCRIPT.name}"
    remote_bits = f"{remote}/{bitstream.name}"
    remote_out = f"{remote}/capture.bin"
    doc: dict = {
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "activity_notes": list(activity.notes),
        "remote_dir": remote,
    }
    stream = None
    failed = True
    run._must(runner, ["mkdir", "-m", "700", remote], f"creating {remote}")
    try:
        run._upload(runner, {script: run.REMOTE_SCRIPT.read_bytes(), remote_bits: bits})
        receive = ["python3", script, "receive", "--uart", board.uart, "--baud", str(probe.BAUD)]
        receive += ["--header", HEADER.hex(), "--length", str(length)]
        receive += ["--timeout", f"{timeout_s:.1f}", "--idle-timeout", "8.0", "--out", remote_out]
        stream = runner.start(run._checked(receive))
        doc["listening"] = stream.expect("listening", run.LISTEN_TIMEOUT_S)
        loaded = run._must(
            runner,
            ["openFPGALoader", *board.loader_args, remote_bits],
            "loading the bitstream",
            timeout=run.LOAD_TIMEOUT_S,
        )
        doc["loader_output"] = " ".join(run._text(loaded.stdout).split())[-200:]
        doc["receiver"] = stream.expect("received", timeout_s + 30)
        stream.wait(30)
        fetched = run._must(runner, ["python3", script, "send", remote_out], "fetching")
        data = run.parse_sent(fetched.stdout)
        failed = False
    finally:
        if stream is not None:
            stream.kill()
        try:
            run._must(runner, ["rm", "-rf", "--", remote], f"removing {remote}")
        except run.RunError as e:
            if not failed:
                raise
            print(f"warning: {e}", file=sys.stderr)
    doc["capture"] = data
    return doc


def summarise(data: bytes, gates: int) -> dict:
    """Gate 0 and the next `gates` gates of the last configuration in a capture."""
    good, bad = probe.parse_capture(data)
    starts = [i for i, r in enumerate(good) if r.gate == 0]
    ours = good[starts[-1] :] if starts else []
    # A capture may end in the middle of a line: that is not a damaged line.
    tail = data.rsplit(b"\n", 1)[-1]
    damaged = [repr(b) for b in bad if b != tail]
    numbered = [r.gate for r in ours] == list(range(len(ours)))
    full = ours[1 : gates + 1]
    out = {
        "lines_good": len(good),
        "lines_before_this_load": starts[-1] if starts else len(good),
        "lines_damaged": damaged,
        "gate_numbers_consecutive": numbered,
        "gate0": ours[0].__dict__ if ours else None,
        "gates": [r.__dict__ for r in full],
        "complete": len(full) == gates and numbered,
    }
    if full:
        for key in ("free", "gated", "high"):
            values = [getattr(r, key) for r in full]
            out[f"{key}_min"], out[f"{key}_max"] = min(values), max(values)
        out["locked_all"] = all(r.locked == 1 for r in full)
        out["losses_max"] = max(r.losses for r in full)
    return out


def measure_one(args, facts: dict, runner) -> dict:
    name, toolchain = facts["setting"], facts["toolchain"]
    board = run.Board(host=args.host, port=args.port, uart=args.uart, user=args.user)
    doc = measure(board, Path(facts["bitstream"]), args.gates, runner, args.wait_busy_min * 60)
    data = doc.pop("capture")
    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = f"{name}-{toolchain}{'-' + args.variant if args.variant else ''}{args.tag}"
    (RESULTS / f"{stem}.capture.txt").write_bytes(data)
    result = {"build": facts, "run": doc, **summarise(data, args.gates)}
    (RESULTS / f"{stem}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


# Reporting --------------------------------------------------------------------------------------


def locked_word(gates: list[dict]) -> str:
    """LOCKED at the ends of the gates, in a word: always, never, or how often."""
    locked = sum(1 for g in gates if g["locked"] == 1)
    if locked == len(gates):
        return "always"
    return "never" if locked == 0 else f"not always ({locked} of {len(gates)})"


def table(rows: list[dict]) -> str:
    head = (
        "| Setting | Toolchain | Ideal count | F min / max | G min / max | LOCKED | Lock losses "
        "| H/gate min / max (phases) | TABLE | CLKOUT0 HIGH, LOW, EDGE, WF_R, WF_F, PHASE_MUX_F "
        "| CLKFBIN from |"
    )
    lines = [head, "|" + "---|" * 11]
    for r in rows:
        b = r["build"]
        m = b["mmcm"]
        fields = (
            f"{m['CLKOUT0_CLKOUT1_HIGH_TIME']}, {m['CLKOUT0_CLKOUT1_LOW_TIME']}, "
            f"{m['CLKOUT0_CLKOUT2_EDGE']}, {m['CLKOUT0_CLKOUT2_FRAC_WF_R']}, "
            f"{m['CLKOUT5_CLKOUT2_FRACTIONAL_FRAC_WF_F']}, "
            f"{m['CLKOUT5_CLKOUT2_FRACTIONAL_PHASE_MUX_F']}"
        )
        ideal = eval_fraction(b["ideal_count"])
        if "free_min" in r:
            gate = probe.GATE_CYCLES
            measured = (
                f"{r['free_min']:,} / {r['free_max']:,} | {r['gated_min']:,} / {r['gated_max']:,} "
                f"| {locked_word(r['gates'])} | {r['losses_max']} "
                f"| {r['high_min'] / gate:.5f} / {r['high_max'] / gate:.5f} ({b['duty_phases']})"
            )
        else:
            measured = "not run | | | | "
        lines.append(
            f"| {b['setting']} | {b['toolchain']} {b.get('variant', '')} | {ideal:,.2f} "
            f"| {measured} | {m['TABLE']:#05x} | {fields} | {b.get('feedback', '?')} |"
        )
    return "\n".join(lines)


def eval_fraction(text: str) -> float:
    numerator, _, denominator = text.partition("/")
    return int(numerator) / int(denominator or 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("settings", help="comma-separated: " + ",".join(probe.SETTINGS))
    parser.add_argument("toolchains", help="comma-separated: " + ",".join(probe.TOOLCHAINS))
    parser.add_argument("--reuse", action="store_true", help="use a bitstream already built")
    parser.add_argument("--no-load", action="store_true", help="build and decode only")
    parser.add_argument("--gates", type=int, default=10, help="1 s readings after gate 0")
    parser.add_argument("--tag", default="", help="suffix for the result files, e.g. -60s")
    parser.add_argument("--variant", default="", choices=sorted(VARIANTS), help=str(VARIANTS))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--uart")
    parser.add_argument("--user", default="pi")
    parser.add_argument("--known-hosts", type=Path)
    parser.add_argument("--wait-busy-min", type=float, default=20.0)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]*", args.tag):
        parser.error("--tag must be letters, digits, '_' and '-'")
    if not args.no_load and not (args.host and args.port and args.uart):
        parser.error("--host, --port and --uart are needed unless --no-load is given")

    pairs = [(s, t) for s in args.settings.split(",") for t in args.toolchains.split(",")]
    for name, toolchain in pairs:
        if name not in probe.SETTINGS or toolchain not in probe.TOOLCHAINS:
            parser.error(f"unknown setting or toolchain: {name} {toolchain}")
    built = []
    for name, toolchain in pairs:  # one FPGA build at a time
        print(f"build {name} {toolchain} ...", flush=True)
        facts = build_one(name, toolchain, args.reuse, args.variant)
        built.append(facts)
        print(f"build {name} {toolchain}: {facts['bitstream_sha256'][:12]}", flush=True)
    rows = [{"build": facts} for facts in built]
    if not args.no_load:
        board = run.Board(host=args.host, port=args.port, uart=args.uart, user=args.user)
        runner = run.SshRunner(board, known_hosts=args.known_hosts)
        rows = []
        for facts in built:
            print(f"run {facts['setting']} {facts['toolchain']} ...", flush=True)
            result = measure_one(args, facts, runner)
            rows.append(result)
            state = "complete" if result["complete"] else "INCOMPLETE"
            print(f"run {facts['setting']} {facts['toolchain']}: {state}", flush=True)
            for r in [result["gate0"], *result["gates"]]:
                print(f"    {r}", flush=True)
            if result["lines_damaged"]:
                print(f"    damaged lines: {result['lines_damaged']}", flush=True)
    print(table(rows))
    return 0 if all(r.get("complete", True) for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
