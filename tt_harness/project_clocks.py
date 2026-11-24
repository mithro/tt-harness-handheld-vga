# SPDX-License-Identifier: Apache-2.0
"""Clocks a project makes from its own logic: where their edges can fall, and how to time them.

Both build flows print, for the root of every such clock (the pin or net that drives clock
pins and is not itself reached by `sys` or `pclk`), the sequential cells its value comes from
through combinational logic:

    tt-harness clock root: <root>
    tt-harness clock contributor: <root> <cell> <clock> <edge> <path> <async>

`<cell>` is the primitive (FDRE, LDCE, ...), `port` for a top-level input, or `clock` for a
clock net used as data. `<clock>` is what clocks that cell: `pclk`, `sys`, `root:<root>` for
another project clock, or `none`. `<edge>` is `rising` or `falling`; `<path>` is `direct`
when the clock reaches the cell's clock pin with no logic between, else `through-logic`;
`<async>` is `async` when the cell has an asynchronous set or clear that is not tied off.

`classify` sorts each root by where its edges can occur, with pclk rising at 0 and falling
at half its period (`tt_harness.build` has the scheme):

- `register`: edges only on pclk rising edges. Every contributor is a flip-flop clocked on
  the rising edge of pclk, directly, or on the rising edge of another `register` root (a
  ripple stage). A gate of several such flip-flops is in this class too; `gate_of` counts
  them, because a gate of more than one can glitch, which no timing model covers.
- `derived`: edges on pclk rising or falling edges. Every contributor is a flip-flop on a
  traced clock, but one is directly on pclk's falling edge, on the falling edge of a
  `register` root (those edges are pclk rising edges too; the class is the conservative
  one), or on a `derived` root.
- `unclassified`: anything else (a latch, a memory, an asynchronous set or clear, a
  flip-flop that pclk reaches through logic or a gate with pclk itself as an input (the
  gate's other inputs are not examined, and one from sys moves the edge off pclk's), a cell
  clocked by sys or by nothing known, a top-level input, a loop of roots, nothing at all).
  Its edges cannot be placed, and it is not signed off. `Classified.assumption` says what
  its timing rests on: where pclk reaches the root it is timed with the `derived` model, a
  clock generated from pclk (`ASSUME_EDGES`; `ASSUME_ASYNC` when the only doubt is an
  asynchronous set or clear, which can move an edge off pclk's); where pclk does not reach
  it, as a primary clock with pclk's waveform and no insertion delay (`ASSUME_PRIMARY`).

`vivado_constraints` writes the Tcl for one build (see `tt_harness.build` for why each
command is what it is). Vivado runs this file as a program between collecting the roots and
applying the constraints, so it must import nothing outside the standard library.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RootKind = Literal["register", "derived", "unclassified"]
REGISTER: RootKind = "register"
DERIVED: RootKind = "derived"
UNCLASSIFIED: RootKind = "unclassified"

ROOT_PREFIX = "tt-harness clock root: "
CONTRIBUTOR_PREFIX = "tt-harness clock contributor: "
# Flip-flops; the `_1` forms are Yosys's falling-edge ones.
FLIP_FLOPS = frozenset(
    f"{ff}{suffix}" for ff in ("FDRE", "FDSE", "FDCE", "FDPE") for suffix in ("", "_1")
)
CLOCK_AS_DATA = "clock"

# What the timing of a root that is not signed off rests on, in fixed wording.
ASSUME_EDGES = "it was timed as a clock with edges on either pclk edge, which is an assumption"
ASSUME_ASYNC = "its edges are modelled on pclk edges, but an asynchronous set or clear can move one"
ASSUME_PRIMARY = (
    "it was timed as a primary clock with pclk's waveform and no modelled insertion delay "
    "relative to pclk and sys, so its cross-domain setup and hold figures are indicative only"
)

_CONTRIBUTOR = re.compile(
    r"^(?P<root>\S+) (?P<cell>\S+) (?P<clock>\S+) (?P<edge>rising|falling) "
    r"(?P<path>direct|through-logic) (?P<async>async|no-async)$"
)


@dataclass(frozen=True)
class Contributor:
    cell: str
    clock: str
    falling: bool
    direct: bool
    asynchronous: bool


@dataclass(frozen=True)
class Classified:
    kind: RootKind
    reason: str  # why, in fixed wording
    # Whether pclk reaches the root through its contributors, so that a clock generated
    # from pclk can be defined on it.
    from_pclk: bool
    gate_of: int  # number of contributors
    # What the timing rests on when the root is not signed off (one of the ASSUME_ texts).
    assumption: str | None = None

    @property
    def signed_off(self) -> bool:
        """False when the timing model for this root is an assumption."""
        return self.kind != UNCLASSIFIED


def parse_roots(text: str) -> dict[str, tuple[Contributor, ...]]:
    """Root -> contributors, in the order printed, from a tool's output."""
    roots: dict[str, list[Contributor]] = {}
    for line in text.splitlines():
        if line.startswith(ROOT_PREFIX):
            name = line.removeprefix(ROOT_PREFIX).strip()
            if not name or " " in name or name in roots:
                raise ValueError(f"cannot read the clock root line {line!r}")
            roots[name] = []
        elif line.startswith(CONTRIBUTOR_PREFIX):
            m = _CONTRIBUTOR.match(line.removeprefix(CONTRIBUTOR_PREFIX).strip())
            if m is None:
                raise ValueError(f"cannot read the clock contributor line {line!r}")
            if m["root"] not in roots:
                raise ValueError(f"clock contributor of an unknown root in {line!r}")
            roots[m["root"]].append(
                Contributor(
                    cell=m["cell"],
                    clock=m["clock"],
                    falling=m["edge"] == "falling",
                    direct=m["path"] == "direct",
                    asynchronous=m["async"] == "async",
                )
            )
    return {name: tuple(found) for name, found in roots.items()}


def classify(roots: dict[str, tuple[Contributor, ...]]) -> dict[str, Classified]:
    """The class of every root (see the module docstring)."""
    loop = "it is in a loop of clocks made by the project"
    done: dict[str, Classified] = {
        name: Classified(UNCLASSIFIED, loop, False, len(found), ASSUME_PRIMARY)
        for name, found in roots.items()
        if _in_loop(name, roots)
    }

    def contributor(c: Contributor) -> tuple[RootKind, str, bool]:
        """(class this contributor allows, why, pclk reaches it)."""
        edge = "falling" if c.falling else "rising"
        if c.cell == CLOCK_AS_DATA and c.clock == "pclk":
            why = "pclk itself is an input of its logic, whose other inputs are not examined"
            return UNCLASSIFIED, why, True
        if c.cell not in FLIP_FLOPS:
            what = "a top-level input" if c.cell == "port" else f"a {c.cell}, not a flip-flop,"
            return UNCLASSIFIED, f"{what} drives it", c.clock == "pclk"
        if c.asynchronous:
            # Whatever clocks it, the set or clear can move an edge; pclk reaches the root
            # when its clock is pclk or a root that pclk reaches.
            reaches = c.clock == "pclk"
            if c.clock.startswith("root:") and c.clock.removeprefix("root:") in roots:
                reaches = one(c.clock.removeprefix("root:")).from_pclk
            why = f"a {c.cell} with an asynchronous set or clear drives it"
            return UNCLASSIFIED, why, reaches
        if c.clock == "pclk":
            if not c.direct:
                why = (
                    "a flip-flop clocked by pclk through logic, whose other inputs are not "
                    "examined, drives it"
                )
                return UNCLASSIFIED, why, True
            if c.falling:
                return DERIVED, "a flip-flop on the falling edge of pclk drives it", True
            return REGISTER, "", True
        if c.clock.startswith("root:"):
            parent_name = c.clock.removeprefix("root:")
            if parent_name not in roots:
                return (
                    UNCLASSIFIED,
                    f"a flip-flop on the unknown clock {parent_name} drives it",
                    False,
                )
            parent = one(parent_name)
            if parent.kind == UNCLASSIFIED:
                why = f"a flip-flop on the unclassified clock {parent_name} drives it"
                return UNCLASSIFIED, why, parent.from_pclk
            if parent.kind == REGISTER and not c.falling:
                return REGISTER, "", parent.from_pclk
            return (
                DERIVED,
                f"a flip-flop on the {edge} edge of the {parent.kind} clock {parent_name} drives it",
                parent.from_pclk,
            )
        return UNCLASSIFIED, f"a flip-flop clocked by {c.clock} drives it", False

    def one(name: str) -> Classified:
        if name in done:
            return done[name]
        found = [contributor(c) for c in roots[name]]
        order = (UNCLASSIFIED, DERIVED, REGISTER)
        if not found:
            why = "nothing sequential drives it"
            result = Classified(UNCLASSIFIED, why, False, 0, ASSUME_PRIMARY)
        else:
            # The least certain contributor decides; its reason is the one reported.
            kind, reason, _ = min(found, key=lambda f: order.index(f[0]))
            if kind == REGISTER:
                n = len(found)
                what = "a flip-flop" if n == 1 else f"a gate of {n} flip-flops"
                reason = f"{what} clocked on pclk rising edges drives it"
            from_pclk = any(f[2] for f in found)
            assumption = None
            if kind == UNCLASSIFIED:
                doubts = [f[1] for f in found if f[0] == UNCLASSIFIED]
                if not from_pclk:
                    assumption = ASSUME_PRIMARY
                elif all("with an asynchronous set or clear" in why for why in doubts):
                    assumption = ASSUME_ASYNC
                else:
                    assumption = ASSUME_EDGES
            result = Classified(kind, reason, from_pclk, len(found), assumption)
        done[name] = result
        return result

    return {name: one(name) for name in roots}


def _in_loop(name: str, roots: dict[str, tuple[Contributor, ...]]) -> bool:
    todo = [name]
    seen: set[str] = set()
    while todo:
        for c in roots.get(todo.pop(), ()):
            if not c.clock.startswith("root:"):
                continue
            parent = c.clock.removeprefix("root:")
            if parent == name:
                return True
            if parent not in seen:
                seen.add(parent)
                todo.append(parent)
    return False


def clock_name(index: int) -> str:
    return f"project_clk{index}"


def vivado_constraints(
    roots: dict[str, tuple[Contributor, ...]],
    pclk_pin: str,
    pclk_period_ns: float,
) -> str:
    """Tcl defining every project clock and its relationships, for `tt_harness.build`.

    The script that collected the roots stored their pin objects in `tt_root(<n>)`, in the
    order printed. `pclk_pin` is the pclk BUFG's output.
    """
    classes = classify(roots)
    half = pclk_period_ns / 2
    lines = ["# Generated by tt_harness.project_clocks from clock-roots.txt."]
    kinds: list[RootKind] = []
    for index, (root, found) in enumerate(classes.items()):
        name = clock_name(index)
        pin = f"$tt_root({index})"
        lines.append(f"# {root}: {found.kind}: {found.reason}")
        if found.kind == REGISTER:
            lines.append(
                f"create_generated_clock -name {name} -edges {{1 3 5}} "
                f"-source [get_pins {pclk_pin}] {pin}"
            )
        elif found.from_pclk:
            lines.append(
                f"create_generated_clock -name {name} -divide_by 1 "
                f"-source [get_pins {pclk_pin}] {pin}"
            )
        else:
            lines.append(
                f"create_clock -name {name} -period {pclk_period_ns:.3f} "
                f"-waveform {{0.000 {half:.3f}}} {pin}"
            )
        lines.append(f'puts "tt-harness project clock: {name} on {pin} kind {found.kind}"')
        kinds.append(found.kind)

    def relate(a: str, b: str, setup_ns: float) -> None:
        pair = f"-from [get_clocks {a}] -to [get_clocks {b}]"
        lines.append(f"set_max_delay {setup_ns:.3f} {pair}")
        lines.append(f"set_min_delay 0.000 {pair}")

    for a, kind_a in enumerate(kinds):
        if kind_a != REGISTER:
            relate("pclk", clock_name(a), half)
            relate(clock_name(a), "pclk", half)
        for b, kind_b in enumerate(kinds):
            if a != b:
                both_register = kind_a == REGISTER and kind_b == REGISTER
                relate(clock_name(a), clock_name(b), pclk_period_ns if both_register else half)
    lines.append(f'puts "tt-harness: {len(kinds)} project clock(s)"')
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    """`project_clocks.py <roots file> <tcl out> <pclk pin> <pclk period ns>`."""
    facts, out, pclk_pin, pclk_period = argv[1:]
    tcl = vivado_constraints(parse_roots(Path(facts).read_text()), pclk_pin, float(pclk_period))
    Path(out).write_text(tcl)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
