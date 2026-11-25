# SPDX-License-Identifier: Apache-2.0
"""What the project's pins see across checks: exactly one well-formed block per begun check.

`assert_clean` reads the (rst_n, ena, ui_in) of the project at every project edge, from the first
sample at the slot's normal values, and requires EVERY block (not only the last) to be well
formed and the blocks to be separated by normal values:

- a block is a contiguous run of samples with `ena` 1 and `ui_in` the reference value, and its
  `rst_n` samples are `0{1..10}` (a check cut short: the design is in reset from the very
  first clock of the override, whenever the cut) or `0{10}1+` (a check not cut: reset low for
  exactly 10 project clocks, then high while counting); never high first, never a pulse,
  never on/off/on of the override;
- outside blocks every sample is the slot's normal value `NORMAL` (no lag samples: `ena`,
  `ui_in` and `rst_n` return together);
- between two blocks at least `MIN_GAP_CLOCKS` normal samples (derivation in `atspeed.py`: normal
  after the unwind edge u + 2, the next override reaches the pins after A + 2 >= u + 6: 4 samples).

`assert_clean` returns the blocks as (length, low samples) and records the smallest gap it saw in
`MIN_GAP_SEEN`.
"""

from __future__ import annotations

import re

from tt_harness.core.atspeed import MIN_GAP_CLOCKS

SW_UI = 0x11  # the software's `ui_in` in the abort tests
REF_UI = 0x5A  # the check's reference `ui_in`

NORMAL = (1, 0, SW_UI)
RESET_CLOCKS = 10
CUT = re.compile(r"0{1,10}")
UNCUT = re.compile(r"0{10}1+")
MIN_GAP_SEEN = [10**9]


def assert_clean(pins, min_gap: int = MIN_GAP_CLOCKS, full_last: bool = True):
    """Check a pin record; return the list of (block length, low samples).

    `full_last`: the last block must not be cut short (when it is closed: a block still in
    progress at the end of the record is not judged).
    """
    first = pins.index(NORMAL)  # before this the pins are at their power-up values
    i, n = first, len(pins)
    blocks: list[tuple[int, int]] = []
    gap = 10**9  # normal samples seen since the last block
    in_progress = False
    while i < n:
        if pins[i][1] == 0:
            assert pins[i] == NORMAL, (
                f"sample {i}: {pins[i]} is not a normal value ({pins[i - 6 : i + 3]})"
            )
            gap += 1
            i += 1
            continue
        j = i
        while j < n and pins[j][1] == 1:
            j += 1
        if j == n:
            in_progress = True  # not judged: the record ended inside it
            break
        block = pins[i:j]
        assert all(ui == REF_UI for _, _, ui in block), f"partial override at {i}: {block[:4]}"
        assert gap >= min_gap, f"override at sample {i} after only {gap} normal samples"
        if blocks:
            MIN_GAP_SEEN[0] = min(MIN_GAP_SEEN[0], gap)
        text = "".join(str(r) for r, _, _ in block)
        assert CUT.fullmatch(text) or UNCUT.fullmatch(text), f"malformed block at {i}: {text}"
        blocks.append((len(block), text.count("0")))
        i, gap = j, 0
    if full_last and blocks and not in_progress:
        assert blocks[-1][1] == RESET_CLOCKS, f"the last check's reset was cut short: {blocks[-1]}"
    return blocks
