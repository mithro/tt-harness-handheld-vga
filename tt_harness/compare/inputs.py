# SPDX-License-Identifier: Apache-2.0
"""Where each signal driven into the project comes from, read from the Migen fragment.

A project may use any of its inputs asynchronously (`rst_n` as an asynchronous reset is
common), so no input may be the output of combinational logic: a comparator or a gate can
glitch when its inputs change, and a glitch on an asynchronous reset clears state that a
zero-delay simulation keeps. Every project input other than its clock must therefore be a
constant or come straight from a register.

`project_inputs` classifies the source of each input of an instance:

- `constant`: a constant, directly or through plain `a.eq(b)` wires;
- `register`: a signal assigned only in a synchronous block, directly or through plain
  wires, or the output of an `AsyncClearFlop` (below);
- `combinational`: anything else that is driven: an operator, a slice, a concatenation, a
  conditional or repeated combinational assignment, a signal assigned both combinationally
  and synchronously;
- `undriven`: a signal nothing in the fragment assigns (a port of the top, or a mistake).

A `register` also reports its clock `domain`: the synchronous block (clock domain) that
assigns it, or the domain of the `AsyncClearFlop`. A register assigned in more than one
domain is reported as `combinational` (it has no one clock). The caller decides which
domain is acceptable (`tt_harness.core.slot` requires `project` for every register input).

The one special accepted as a register (`AsyncClearFlop`, an FDCE: output 0 while its
`clear` is high, else the value of `d` at the last clock edge of its domain) is accepted
for exactly its `q` output, and only when nothing else in the fragment assigns that signal.
No other special's output is accepted (a `MultiReg` output, for example, is `undriven` here,
because a synchroniser output is not a project-clock register).

Only `constant` and `register` are glitch-free (`GLITCH_FREE`); what is not recognised is
`combinational`, so a new input fails by default. This reads the source description: it
cannot see logic that synthesis inserts or duplicates. `tt_harness.build` checks the
implemented netlist for that.
"""

from __future__ import annotations

from dataclasses import dataclass

from migen import Module, Signal
from migen.fhdl.specials import Instance
from migen.fhdl.structure import Constant, _Assign
from migen.fhdl.tools import list_targets

from tt_harness.core.cdc import AsyncClearFlop

GLITCH_FREE = ("constant", "register")
CLOCK_PORT = "clk"


@dataclass(frozen=True)
class InputSource:
    port: str
    kind: str  # "constant", "register", "combinational" or "undriven"
    detail: str  # the signal or constant found, or why it is not glitch-free
    domain: str | None = None  # clock domain of a `register`; None for anything else

    @property
    def glitch_free(self) -> bool:
        return self.kind in GLITCH_FREE


def _name(signal: Signal) -> str:
    return signal.backtrace[-1][0] if signal.backtrace else repr(signal)


def project_inputs(module: Module, instance_name: str) -> dict[str, InputSource]:
    """The source of every input of the instance called `instance_name` in `module`.

    The module is finalised (its fragment is collected), so use a module made for this.
    """
    fragment = module.get_fragment()
    instances = [
        s for s in fragment.specials if isinstance(s, Instance) and s.name_override == instance_name
    ]
    if len(instances) != 1:
        raise ValueError(f"expected one instance named {instance_name!r}, found {len(instances)}")
    comb_targets = list_targets(fragment.comb)
    sync_targets = set()
    domains_of: dict[Signal, set[str]] = {}
    for domain, statements in fragment.sync.items():
        targets = list_targets(statements)
        sync_targets |= targets
        for signal in targets:
            domains_of.setdefault(signal, set()).add(domain)
    flop_outputs = {s.q: s.cd for s in fragment.specials if isinstance(s, AsyncClearFlop)}

    def plain_source(signal: Signal):
        """The right-hand side if `signal` is one unconditional `signal.eq(x)`, else None."""
        assigns = [s for s in fragment.comb if signal in list_targets([s])]
        if len(assigns) == 1 and isinstance(assigns[0], _Assign) and assigns[0].l is signal:
            return assigns[0].r
        return None

    def classify(port: str, expr) -> InputSource:
        seen: set[int] = set()
        while True:
            if isinstance(expr, Constant):
                return InputSource(port, "constant", f"constant {expr.value}")
            if not isinstance(expr, Signal):
                kind = type(expr).__name__.lstrip("_")
                return InputSource(port, "combinational", f"an expression ({kind})")
            if id(expr) in seen:
                return InputSource(port, "combinational", f"a loop through {_name(expr)}")
            seen.add(id(expr))
            in_comb, in_sync = expr in comb_targets, expr in sync_targets
            if in_comb and in_sync:
                return InputSource(
                    port, "combinational", f"{_name(expr)} is assigned in comb and in sync"
                )
            if in_sync:
                domains = domains_of[expr]
                if len(domains) != 1:
                    return InputSource(
                        port,
                        "combinational",
                        f"{_name(expr)} is assigned in clock domains {sorted(domains)}",
                    )
                return InputSource(port, "register", _name(expr), next(iter(domains)))
            if expr in flop_outputs and not in_comb:
                return InputSource(
                    port, "register", f"{_name(expr)} (AsyncClearFlop)", flop_outputs[expr]
                )
            if not in_comb:
                return InputSource(port, "undriven", f"nothing assigns {_name(expr)}")
            source = plain_source(expr)
            if source is None:
                return InputSource(
                    port, "combinational", f"{_name(expr)} is made by combinational logic"
                )
            expr = source

    out = {}
    for item in instances[0].items:
        if isinstance(item, Instance.Input):
            out[item.name] = classify(item.name, item.expr)
    return out
