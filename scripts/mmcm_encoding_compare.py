# SPDX-License-Identifier: Apache-2.0
import re
import sys
from pathlib import Path


def feats(p):
    out = {}
    for line in p.read_text().splitlines():
        if "MMCME2_ADV." in line and re.search(
            r"CLKOUT0_|CLKFBOUT_|CLKOUT5_CLKOUT[12]_FRACTIONAL|DIVCLK_", line
        ):
            name = line.split("MMCME2_ADV.")[1]
            m = re.match(r"(\w+?)(?:\[(\d+)(?::(\d+))?\])?(?: = \d+'b(\w+))?$", name)
            base, hi, lo, val = m.groups()
            if val is not None:
                v = int(val, 2) << int(lo)
            elif hi is not None:
                v = 1 << int(hi)
            else:
                v = 1
            out[base] = out.get(base, 0) + v
    return out


for arg in sys.argv[1:]:
    d = Path(arg)
    o = feats(d / "open.decoded.fasm")
    v = feats(d / "viv.decoded.fasm")
    print("==", d.name)
    for k in sorted(set(o) | set(v)):
        flag = "" if o.get(k) == v.get(k) else "  <-- DIFF"
        print(f"{k:55s} open={o.get(k, 0):4d} viv={v.get(k, 0):4d}{flag}")
