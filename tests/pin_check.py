# SPDX-License-Identifier: Apache-2.0
"""What the project's pins see across checks: exactly one clean sequence per accepted start.

`assert_clean` reads the (rst_n, ena, ui_in) of the project at every project edge and requires
the record to be a concatenation of clean check sequences separated by normal-value gaps:

- override block (`ena` 1 and `ui_in` the reference value, together, in one contiguous run);
  inside it `rst_n` is high, then low (at most 10 project clocks; exactly 10 when the check
  was not cut short), then high: no on/off/on of the override and no pulse of `rst_n`;
- outside blocks every sample is the slot's normal value; after a block the reset chain may
  still be low for up to 2 samples (`RESET_LAG`), nothing else is allowed;
- between two blocks at least `MIN_GAP_CLOCKS` consecutive normal samples.

Derived minimum gap (in `atspeed.py`): after the unwind edge u the reset chain is normal after
u + 2 and `rel` rises after u + 3; the project then needs the system side to see `rel`, wait
the launch delay, and then two begin edges, so a begin is at least 6 edges after u and the
override reaches the pins one edge later: the pins are normal from u + 2 to u + 7, at least 5
samples (`MIN_GAP_CLOCKS` = 5 in `atspeed.py`, which the checker requires).
"""

from __future__ import annotations

from tt_harness.core.atspeed import MIN_GAP_CLOCKS

SW_UI = 0x11  # the software's `ui_in` in the abort tests
REF_UI = 0x5A  # the check's reference `ui_in`

NORMAL = (1, 0, SW_UI)
RESET_LAG = 2
RESET_CLOCKS = 10


def assert_clean(pins, min_gap: int = MIN_GAP_CLOCKS, full_last: bool = True):
    """Check a pin record; return the list of (block length, low samples)."""
    first = pins.index(NORMAL)  # before this the pins are at their power-up values
    i, n = first, len(pins)
    blocks: list[tuple[int, int]] = []
    gap = 10**9  # normal samples seen since the last block
    lag = RESET_LAG
    while i < n:
        rst, ena, ui = pins[i]
        if ena == 0:
            if pins[i] == NORMAL:
                gap += 1
            elif blocks and gap == 0 and rst == 0 and ui == SW_UI and lag > 0:
                lag -= 1  # the reset chain still on its way back after a block
            else:
                raise AssertionError(
                    f"sample {i}: {pins[i]} is not a normal value ({pins[i - 6 : i + 3]})"
                )
            i += 1
            continue
        # an override block
        j = i
        while j < n and pins[j][1] == 1:
            j += 1
        if j == n:
            break  # still in progress at the end of the record
        block = pins[i:j]
        assert all(ui == REF_UI for _, _, ui in block), f"partial override at {i}: {block[:4]}"
        assert gap >= min_gap, f"override at sample {i} after only {gap} normal samples"
        text = "".join(str(r) for r, _, _ in block)
        low = text.count("0")
        assert "".join(sorted(set(text))) in ("0", "01", "1"), text
        k = text.find("0")
        if k >= 0:
            assert text[k : k + low] == "0" * low, f"rst_n pulsed inside a block: {text}"
            assert low <= RESET_CLOCKS, f"reset low for {low} clocks: {text}"
        blocks.append((len(block), low))
        i, gap, lag = j, 0, RESET_LAG
    if full_last and blocks:
        assert blocks[-1][1] == RESET_CLOCKS, f"the last check's reset was cut short: {blocks[-1]}"
    return blocks
