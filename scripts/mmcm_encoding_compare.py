# SPDX-License-Identifier: Apache-2.0
"""Print every MMCME2_ADV feature that differs between the open flow and Vivado.

Reads `open.decoded.fasm` and `viv.decoded.fasm` in each directory given (written by
`scripts/mmcm_encoding_probe.py`, which decodes both bitstreams with `bit2fasm`) and prints the
MMCM site's features whose value differs, as integers (bit slices of one field are summed).
The fields of the dividers in use come first, then the lock/filter table, the power register
and the fields of unused outputs.

    uv run python scripts/mmcm_encoding_compare.py build/fracprobe/c1_m18.000_d35.750 ...
"""

import re
import sys
from pathlib import Path

from tt_harness.clock_build import mmcm_values

DIVIDER = re.compile(r"CLKOUT0_|CLKFBOUT_|CLKOUT5_CLKOUT[12]_FRACTIONAL|DIVCLK_")


def main(dirs: list[str]) -> None:
    for arg in dirs:
        d = Path(arg)
        o = mmcm_values((d / "open.decoded.fasm").read_text())
        v = mmcm_values((d / "viv.decoded.fasm").read_text())
        print("==", d.name)
        for k in sorted(set(o) | set(v), key=lambda k: (not DIVIDER.match(k), k)):
            if o.get(k, 0) != v.get(k, 0):
                print(f"{k:55s} open={o.get(k, 0):#06x} viv={v.get(k, 0):#06x}")


if __name__ == "__main__":
    main(sys.argv[1:])
