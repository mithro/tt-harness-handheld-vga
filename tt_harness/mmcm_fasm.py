# SPDX-License-Identifier: Apache-2.0
"""What a decoded bitstream (FASM text from `bit2fasm`) says about its MMCM's feedback.

An MMCM locks only if its CLKFBIN pin sees its own CLKFBOUT. With CLKFBOUT wired straight
to CLKFBIN that is the tile's dedicated connection, a routing pip beside the MMCM site:

    <CMT tile>.CMT_LR_LOWER_B_MMCM_CLKFBIN.CMT_LR_LOWER_B_CLKFBOUT2IN

nextpnr-xilinx 0.8.2 instead takes CLKFBIN off the net and ties it to VCC when the MMCM's
COMPENSATION is "INTERNAL" or absent (`xilinx/pack_clocking_xc7.cc`, lines 98 to 104); the
bitstream then has `...MMCM_CLKFBIN.CMT_L_LOWER_B_CLK_IN3_INT`, a pin of the fabric
interconnect, and the MMCM never locks (measured: docs/results/2026-11-spike-b-project-clock.md).
No tool reports it, and the MMCM site's own features (`.MMCME2_ADV.`) are the same either
way, so the check has to look at the tile's routing features.

The tile and the pip names are read from the FASM itself (the tile is the one that holds the
`MMCME2_ADV` features; the pip is the one whose destination ends in `MMCM_CLKFBIN`), so the
same code serves the left and right CMT columns and both boards.
"""

from __future__ import annotations

import re

DEDICATED = "CLKFBOUT2IN"  # the source wire's name ends with this
_SITE = re.compile(r"^(\S+?)\.MMCME2_ADV\.", re.MULTILINE)


class FeedbackError(ValueError):
    """The bitstream's MMCM does not take its feedback from its own CLKFBOUT."""


def mmcm_tiles(fasm: str) -> list[str]:
    """The tiles that hold an MMCM in use, in order of first appearance."""
    return list(dict.fromkeys(_SITE.findall(fasm)))


def tile_routing(fasm: str, tile: str) -> set[str]:
    """The tile's routing features (`destination.source`), without the MMCM site's own."""
    lead = tile + "."
    return {
        line.strip()[len(lead) :]
        for line in fasm.splitlines()
        if line.startswith(lead) and ".MMCME2_ADV." not in line
    }


def feedback_source(fasm: str) -> tuple[str, str]:
    """`(tile, source wire)` of the pip into the MMCM's CLKFBIN.

    Raises `FeedbackError` unless the FASM has exactly one MMCM and exactly one such pip:
    with none, CLKFBIN is unconnected in the bitstream.
    """
    tiles = mmcm_tiles(fasm)
    if len(tiles) != 1:
        raise FeedbackError(f"expected one MMCM in the bitstream, found {len(tiles)}: {tiles}")
    (tile,) = tiles
    sources = sorted(
        source
        for destination, _, source in (f.partition(".") for f in tile_routing(fasm, tile))
        if destination.endswith("MMCM_CLKFBIN")
    )
    if len(sources) != 1:
        raise FeedbackError(
            f"expected one source for the MMCM's CLKFBIN in tile {tile}, found {sources}"
        )
    return tile, sources[0]


def check_feedback(fasm: str) -> str:
    """The feedback source's name, if it is the MMCM's own CLKFBOUT; else `FeedbackError`."""
    tile, source = feedback_source(fasm)
    if not source.endswith(DEDICATED):
        why = (
            "a pin of the fabric interconnect (nextpnr-xilinx ties it to VCC unless "
            'COMPENSATION is an explicit "ZHOLD")'
            if source.endswith("_INT")
            else "not the dedicated CLKFBOUT connection"
        )
        raise FeedbackError(
            f"the MMCM in tile {tile} takes CLKFBIN from {source}, {why}: it cannot lock"
        )
    return source
