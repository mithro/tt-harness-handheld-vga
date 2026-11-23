# SPDX-License-Identifier: Apache-2.0
"""ReferenceStimulus tests (Migen simulator)."""

from __future__ import annotations

import pytest
from migen.sim import run_simulation

from tt_harness.core.stimulus import ReferenceStimulus

INDICES = [*range(0, 25), 255, 256, 65535, 2**31, 2**32 - 1]


def _outputs(stim: ReferenceStimulus) -> dict[int, dict[str, int]]:
    """Ports of `stim` for each clock_index in INDICES."""
    seen: dict[int, dict[str, int]] = {}

    def gen():
        for index in INDICES:
            yield stim.clock_index.eq(index)
            yield
            seen[index] = {
                "ena": (yield stim.ena),
                "rst_n": (yield stim.rst_n),
                "ui_in": (yield stim.ui_in),
                "uio_in": (yield stim.uio_in),
            }

    run_simulation(stim, gen())
    return seen


def test_reset_for_first_ten_clocks() -> None:
    seen = _outputs(ReferenceStimulus(ui_in=0x5A, uio_in=0xC3))
    for index, out in seen.items():
        assert out["rst_n"] == (0 if index < 10 else 1), f"clock_index {index}"


def test_constants() -> None:
    seen = _outputs(ReferenceStimulus(ui_in=0x5A, uio_in=0xC3))
    for index, out in seen.items():
        assert out["ena"] == 1, f"clock_index {index}"
        assert out["ui_in"] == 0x5A, f"clock_index {index}"
        assert out["uio_in"] == 0xC3, f"clock_index {index}"


def test_other_reset_length() -> None:
    seen = _outputs(ReferenceStimulus(ui_in=0, uio_in=0, reset_clocks=3))
    for index, out in seen.items():
        assert out["rst_n"] == (0 if index < 3 else 1), f"clock_index {index}"


def test_zero_reset_clocks_never_asserts_reset() -> None:
    seen = _outputs(ReferenceStimulus(ui_in=0xFF, uio_in=0x00, reset_clocks=0))
    for index, out in seen.items():
        assert out["rst_n"] == 1, f"clock_index {index}"
        assert out["ui_in"] == 0xFF
        assert out["uio_in"] == 0x00


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ui_in": 256, "uio_in": 0},
        {"ui_in": -1, "uio_in": 0},
        {"ui_in": 0, "uio_in": 256},
        {"ui_in": 0, "uio_in": 0, "reset_clocks": -1},
    ],
)
def test_rejects_out_of_range(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ReferenceStimulus(**kwargs)
