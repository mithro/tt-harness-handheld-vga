# SPDX-License-Identifier: Apache-2.0
"""The test pool: its list, one design through simulation, and the `pool-sim` command."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest

from tt_harness import cli, pool, reference, tools, verdict
from tt_harness.fetch import Sources
from tt_harness.manifest import Project

REPO = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data" / "projects"
ID = "tt00/tt_um_test_counter"
CLOCKS = 3000

needs_tools = [
    pytest.mark.toolchain,
    pytest.mark.skipif(tools.find("verilator") is None, reason="verilator not installed"),
    pytest.mark.skipif(tools.find("yosys") is None, reason="yosys not installed"),
]


def _counter() -> Sources:
    top = "tt_um_test_counter"
    project = Project(
        shuttle="tt00",
        macro=top,
        repo="/nonexistent",
        commit="0" * 40,
        top_module=top,
        source_files=(f"{top}.v",),
        clock_hz=25_175_000,
        clock_declared=25_175_000,
        pmods=(),
        address=None,
        ui_in=0xA5,
        uio_in=0,
        patch=None,
    )
    root = DATA / "counter"
    return Sources(project, root, (root / "src" / f"{top}.v",), needs_sv=False)


@pytest.fixture
def one_pool(tmp_path, monkeypatch):
    """The test counter registered as a one-entry pool, with an empty shared reference dir."""
    sources = _counter()
    monkeypatch.setattr(pool, "POOL", (ID,))
    monkeypatch.setattr(pool, "load_sources", lambda project_id: sources)
    shared = tmp_path / "shared"
    monkeypatch.setenv("TT_REFERENCE_DIR", str(shared))
    return {"sources": sources, "shared": shared, "work": tmp_path / "pool"}


def _write_shared(env, samples: bytes, **fields) -> Path:
    d = env["shared"] / ID / "ideal"
    d.mkdir(parents=True)
    (d / "uo_out.bin.gz").write_bytes(gzip.compress(samples))
    doc = {
        "schema": 3,
        "id": ID,
        "profile": "ideal",
        "ui_in_unmet": 0,
        "uio_in_unmet": 0,
        "clock_hz": 25_175_000,
        "reset_clocks": 10,
        "ui_in": 0xA5,
        "uio_in": 0,
        "clocks": len(samples),
        "dump": "uo_out.bin.gz",
        "dump_sha256": hashlib.sha256(samples).hexdigest(),
    }
    doc.update(fields)
    path = d / "reference.json"
    path.write_text(json.dumps(doc))
    return path


def _true_samples(env, tmp_path: Path, clocks: int) -> bytes:
    return reference.simulate(env["sources"], clocks, tmp_path / "truth").read_bytes()


def test_pool_is_the_first_test_pool_of_the_document() -> None:
    text = (REPO / "docs" / "test-designs.md").read_text()
    section = text.split("## First test pool")[1].split("Awkward cases")[0]
    ids = tuple(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE))
    assert len(ids) == 8
    assert ids == pool.POOL


class TestRunSim:
    pytestmark = needs_tools

    def test_exact_against_the_local_reference(self, one_pool) -> None:
        result = pool.run_sim(ID, CLOCKS, one_pool["work"])
        assert result.verdict == "exact"
        assert result.first_diff_clock is None
        assert result.reference_source == "local"
        assert result.clocks == CLOCKS
        doc = json.loads((one_pool["work"] / ID / "result.json").read_text())
        assert doc == result.to_json()
        assert {"id", "verdict", "first_diff_clock", "prescreen", "reference_source", "clocks"} <= (
            set(doc)
        )
        assert doc["id"] == ID
        assert doc["verdict"] == "exact"
        assert doc["first_diff_clock"] is None
        assert doc["reference_source"] == "local"
        assert doc["clocks"] == CLOCKS
        assert doc["prescreen"]["flagged"] is False
        assert doc["prescreen"]["derived_clocks"] == 0

    def test_exact_against_the_shared_reference_when_present(self, one_pool, tmp_path) -> None:
        # The shared dump is longer than the run: only the first CLOCKS are compared.
        path = _write_shared(one_pool, _true_samples(one_pool, tmp_path, CLOCKS + 500))
        result = pool.run_sim(ID, CLOCKS, one_pool["work"])
        assert result.verdict == "exact"
        assert result.reference_source == "shared"
        assert result.reference_path == str(path)
        assert not (one_pool["work"] / ID / "reference").exists()  # no local one was made

    def test_corrupted_reference_is_a_mismatch_at_the_right_clock(self, one_pool, tmp_path) -> None:
        samples = bytearray(_true_samples(one_pool, tmp_path, CLOCKS))
        samples[1234] ^= 0x01
        samples[2000] ^= 0x80
        _write_shared(one_pool, bytes(samples))
        result = pool.run_sim(ID, CLOCKS, one_pool["work"])
        assert result.verdict == "mismatch"
        assert result.first_diff_clock == 1234
        assert result.reference_source == "shared"
        assert "1234" in result.detail
        doc = json.loads((one_pool["work"] / ID / "result.json").read_text())
        assert (doc["verdict"], doc["first_diff_clock"]) == ("mismatch", 1234)

    def test_unusable_shared_reference_is_an_error_not_a_fallback(self, one_pool, tmp_path) -> None:
        _write_shared(one_pool, _true_samples(one_pool, tmp_path, CLOCKS), dump_sha256="0" * 64)
        with pytest.raises(verdict.ReferenceInvalid):
            pool.run_sim(ID, CLOCKS, one_pool["work"])

    def test_failed_shared_reference_is_an_error_not_a_fallback(self, one_pool) -> None:
        d = one_pool["shared"] / ID / "ideal"
        d.mkdir(parents=True)
        (d / "reference-fail.json").write_text(
            json.dumps({"id": ID, "status": "sim-unsettled", "reason": "pad"})
        )
        with pytest.raises(verdict.ReferenceInvalid, match="sim-unsettled"):
            pool.run_sim(ID, CLOCKS, one_pool["work"])

    def test_shared_reference_shorter_than_the_run_is_an_error(self, one_pool, tmp_path) -> None:
        _write_shared(one_pool, _true_samples(one_pool, tmp_path, CLOCKS - 1))
        with pytest.raises(pool.PoolError, match="fewer"):
            pool.run_sim(ID, CLOCKS, one_pool["work"])

    def test_shared_reference_under_other_inputs_is_an_error(self, one_pool, tmp_path) -> None:
        _write_shared(one_pool, _true_samples(one_pool, tmp_path, CLOCKS), ui_in=0)
        with pytest.raises(pool.PoolError, match="ui_in"):
            pool.run_sim(ID, CLOCKS, one_pool["work"])


# pool-sim sub-command -----------------------------------------------------------------


def _result(project_id: str, kind: str, diff: int | None = None) -> pool.PoolResult:
    return pool.PoolResult(
        id=project_id,
        verdict=kind,
        first_diff_clock=diff,
        detail="" if diff is None else f"first difference at clock {diff}",
        prescreen={"latches": 0, "comb_loops": 0, "derived_clocks": 0, "flagged": False},
        reference_source="shared",
        reference_path="/ref/reference.json",
        reference_sha256="0" * 64,
        clocks=1000,
        seconds=0.1,
    )


def _pool_sim(monkeypatch, tmp_path, capsys, outcomes: dict, *extra: str):
    calls = []

    def fake_run_sim(project_id, clocks, work):
        calls.append((project_id, clocks, Path(work)))
        outcome = outcomes[project_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pool, "POOL", tuple(outcomes))
    monkeypatch.setattr(pool, "run_sim", fake_run_sim)
    code = cli.main(["pool-sim", "--work", str(tmp_path / "pool"), *extra])
    out, err = capsys.readouterr()
    return code, out, err, calls


def test_pool_sim_all_exact_exits_0_with_a_table(monkeypatch, tmp_path, capsys) -> None:
    outcomes = {"a/x": _result("a/x", "exact"), "b/y": _result("b/y", "exact")}
    code, out, _, calls = _pool_sim(
        monkeypatch, tmp_path, capsys, outcomes, "--clocks", "1000", "--jobs", "2"
    )
    assert code == 0
    assert sorted(calls) == [("a/x", 1000, tmp_path / "pool"), ("b/y", 1000, tmp_path / "pool")]
    rows = [line for line in out.splitlines() if line.startswith(("a/x", "b/y"))]
    assert [row.split()[0] for row in rows] == ["a/x", "b/y"]  # pool order, not finish order
    assert all("exact" in row and "shared" in row for row in rows)
    summary = json.loads((tmp_path / "pool" / "summary.json").read_text())
    assert [r["id"] for r in summary["results"]] == ["a/x", "b/y"]
    assert summary["clocks"] == 1000


def test_pool_sim_mismatch_exits_1_and_names_the_clock(monkeypatch, tmp_path, capsys) -> None:
    outcomes = {"a/x": _result("a/x", "exact"), "b/y": _result("b/y", "mismatch", 77)}
    code, out, _, _ = _pool_sim(monkeypatch, tmp_path, capsys, outcomes)
    assert code == 1
    row = next(line for line in out.splitlines() if line.startswith("b/y"))
    assert "mismatch" in row
    assert "77" in row


def test_pool_sim_failure_is_an_error_row_and_exit_2(monkeypatch, tmp_path, capsys) -> None:
    outcomes = {"a/x": pool.PoolError("a/x: it broke"), "b/y": _result("b/y", "mismatch", 5)}
    code, out, err, _ = _pool_sim(monkeypatch, tmp_path, capsys, outcomes)
    assert code == 2
    row = next(line for line in out.splitlines() if line.startswith("a/x"))
    assert "error" in row
    assert "it broke" in err
    doc = json.loads((tmp_path / "pool" / "a" / "x" / "result.json").read_text())
    assert doc["verdict"] == "error"
    assert "it broke" in doc["detail"]


def test_pool_sim_default_clocks_is_three_frames(monkeypatch, tmp_path, capsys) -> None:
    _, _, _, calls = _pool_sim(monkeypatch, tmp_path, capsys, {"a/x": _result("a/x", "exact")})
    assert calls == [("a/x", 1_300_000, tmp_path / "pool")]
