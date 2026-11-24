# SPDX-License-Identifier: Apache-2.0
"""The MMCM feedback check (`tt_harness.mmcm_fasm`) and the build gate that uses it.

The fixtures in `tests/data/mmcm_feedback/` are the MMCM tile's features decoded from three
bitstreams that were run on an Arty (Task 7c): the open flow's with COMPENSATION INTERNAL,
which never locked, and Vivado's and the open flow's ZHOLD variant, which did.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tt_harness import clock_build as cb
from tt_harness import mmcm_fasm
from tt_harness.core.clocking import explicit_setting

DATA = Path(__file__).resolve().parent / "data" / "mmcm_feedback"
BAD = DATA / "arty-openxc7-internal-bad.fasm"
GOOD = [DATA / "arty-vivado-internal-good.fasm", DATA / "arty-openxc7-zhold-good.fasm"]
ARTY_TILE = "CMT_TOP_L_LOWER_B_X106Y61"
GAMEBUB_TILE = "CMT_TOP_R_LOWER_B_X8Y61"  # the Game Bub's MMCM: a right-hand CMT column


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.stem)
def test_good_bitstreams_pass(path: Path) -> None:
    fasm = path.read_text()
    assert mmcm_fasm.mmcm_tiles(fasm) == [ARTY_TILE]
    assert mmcm_fasm.feedback_source(fasm) == (ARTY_TILE, "CMT_LR_LOWER_B_CLKFBOUT2IN")
    assert mmcm_fasm.check_feedback(fasm) == "CMT_LR_LOWER_B_CLKFBOUT2IN"


def test_the_known_bad_bitstream_fails() -> None:
    fasm = BAD.read_text()
    assert mmcm_fasm.feedback_source(fasm) == (ARTY_TILE, "CMT_L_LOWER_B_CLK_IN3_INT")
    with pytest.raises(mmcm_fasm.FeedbackError, match="CLK_IN3_INT.*cannot lock"):
        mmcm_fasm.check_feedback(fasm)


def test_names_come_from_the_mmcm_s_own_tile() -> None:
    # The same features in another tile, as on the Game Bub, and a tile whose wires are
    # named for the right-hand column.
    for path, ok in ((GOOD[0], True), (BAD, False)):
        moved = path.read_text().replace(ARTY_TILE, GAMEBUB_TILE)
        moved = moved.replace("CMT_L_LOWER_B_", "CMT_R_LOWER_B_")
        assert mmcm_fasm.mmcm_tiles(moved) == [GAMEBUB_TILE]
        if ok:
            assert mmcm_fasm.check_feedback(moved).endswith("CLKFBOUT2IN")
        else:
            with pytest.raises(mmcm_fasm.FeedbackError, match=GAMEBUB_TILE):
                mmcm_fasm.check_feedback(moved)
    # A feedback pip in some other tile does not count for this MMCM.
    elsewhere = (
        BAD.read_text() + "CMT_TOP_R_LOWER_B_X8Y9.CMT_LR_LOWER_B_MMCM_CLKFBIN.X_CLKFBOUT2IN\n"
    )
    with pytest.raises(mmcm_fasm.FeedbackError):
        mmcm_fasm.check_feedback(elsewhere)


def test_a_bitstream_without_one_mmcm_or_one_feedback_pip_fails() -> None:
    good = GOOD[0].read_text()
    with pytest.raises(mmcm_fasm.FeedbackError, match="expected one MMCM"):
        mmcm_fasm.check_feedback("CLBLL_L_X2Y1.SLICEL_X0.NOCLKINV\n")
    unconnected = "\n".join(x for x in good.splitlines() if "MMCM_CLKFBIN" not in x)
    with pytest.raises(mmcm_fasm.FeedbackError, match=r"found \[\]"):
        mmcm_fasm.check_feedback(unconnected)
    both = good + f"{ARTY_TILE}.CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_L_LOWER_B_CLK_IN3_INT\n"
    with pytest.raises(mmcm_fasm.FeedbackError, match="expected one source"):
        mmcm_fasm.check_feedback(both)
    other = good.replace("CMT_LR_LOWER_B_CLKFBOUT2IN", "CMT_L_LOWER_B_CLK_FREQ_BB0")
    with pytest.raises(mmcm_fasm.FeedbackError, match="not the dedicated"):
        mmcm_fasm.check_feedback(other)


def test_tile_routing_leaves_out_the_site_features() -> None:
    routing = mmcm_fasm.tile_routing(GOOD[0].read_text(), ARTY_TILE)
    assert routing == {
        "CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_LR_LOWER_B_CLKFBOUT2IN",
        "CMT_LR_LOWER_B_MMCM_CLKIN1.CMT_L_LOWER_B_CLK_IN1_HCLK",
        "CMT_LR_LOWER_B_MMCM_CLKIN2.CMT_L_LOWER_B_CLK_IN2_INT",
    }
    bad = mmcm_fasm.tile_routing(BAD.read_text(), ARTY_TILE)
    assert bad ^ routing == {
        "CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_LR_LOWER_B_CLKFBOUT2IN",
        "CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_L_LOWER_B_CLK_IN3_INT",
    }
    # `mmcm_values`, which Task 7b compared, sees no difference there: that is how it was missed.
    assert "CLKFBIN" not in " ".join(cb.mmcm_values(BAD.read_text()))


# The build gate, with the decoder replaced by a saved decode ------------------------------------


def _job(tmp_path: Path, toolchain: str) -> cb.Build:
    job = cb.Build(
        cb.ARTY,
        toolchain,
        tmp_path,
        explicit_setting(100_000_000, 1, 9.0, 35.75),
        tmp_path / "build.log",
    )
    job.bitstream = tmp_path / "top.bit"
    job.bitstream.write_bytes(b"not a real bitstream")
    job.log.write_text("")
    return job


def _decode_as(monkeypatch, fixture: Path) -> list:
    calls = []

    def fake(bit: Path, part: str, out: Path) -> Path:
        calls.append((bit, part, out))
        shutil.copy(fixture, out)
        return out

    monkeypatch.setattr(cb, "decode_bitstream", fake)
    return calls


def test_gate_fails_the_build_of_the_known_bad_bitstream(tmp_path, monkeypatch) -> None:
    job = _job(tmp_path, "openxc7")
    calls = _decode_as(monkeypatch, BAD)
    with pytest.raises(cb.BuildError, match="arty openxc7: .*CLK_IN3_INT.*cannot lock"):
        cb.feedback_gate(job)
    assert calls == [(job.bitstream, "xc7a35tcsg324-1", tmp_path / "top.decoded.fasm")]


@pytest.mark.parametrize("toolchain,fixture", [("vivado", GOOD[0]), ("openxc7", GOOD[1])])
def test_gate_passes_a_good_bitstream(tmp_path, monkeypatch, toolchain, fixture) -> None:
    job = _job(tmp_path, toolchain)
    calls = _decode_as(monkeypatch, fixture)
    assert cb.feedback_gate(job) == "CMT_LR_LOWER_B_CLKFBOUT2IN"
    # The Vivado part of the Arty is decoded with the open part's database: the same die.
    assert calls[0][1] == "xc7a35tcsg324-1"
    assert cb.decoded_path(job).read_text() == fixture.read_text()
