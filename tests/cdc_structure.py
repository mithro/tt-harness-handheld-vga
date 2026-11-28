# SPDX-License-Identifier: Apache-2.0
"""Clock-domain analysis of a Migen fragment, for the structural crossing tests.

A simulator cannot see a register that samples a bus of another clock domain (its first flop is
just a flop), so the crossings are checked on the STRUCTURE: `analyse` gives every signal the
clock domain(s) it depends on and lists everything that passes from one domain to another.

Domains of a signal:

- assigned in a synchronous block: that block's domain (assigned in two: an error);
- output of a `MultiReg`: its `odomain`; output of an `AsyncClearFlop`: its domain;
- output of an `Instance`: the domain the caller gives for that instance (`instances`: instance
  name -> domain), else an error;
- assigned combinationally: the union of the domains of everything the assignment reads
  (conditions included), to a fixed point;
- assigned by nothing (a port): the domain the caller declares in `ports`, else an error.

It fails closed: a signal that is read and cannot be given a domain, an unknown special, or an
instance with outputs and no declared domain is in `errors`, never skipped.

What is reported:

- `reads`: every synchronous statement in domain D that reads a signal with another domain, as
  (D, the statement's targets, the signal read);
- `mixed`: combinational signals that depend on more than one domain;
- `multiregs`: each `MultiReg` with its stage count and whether its input is a plain register
  (a signal assigned only in one synchronous block, not combinationally, not an expression);
- `flops`: each `AsyncClearFlop` with the same for its clear, and the domains of its `d`;
- `instance_inputs`: for each instance given in `instances`, input port -> domains;
- `memory_ports`: each port of a `Memory`, with its clock domain (the domain of its read data)
  and the domains of its inputs (address, write data and enables). A memory written in one
  domain and read in another is a dual-clock block RAM: each port must be fed from its own
  domain only. A port that reads asynchronously (`async_read`) is an error: its read data
  is combinational, not block RAM's registered output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from migen import Signal
from migen.fhdl.specials import Instance, Memory, _MemoryPort
from migen.fhdl.structure import ClockSignal, Constant, ResetSignal
from migen.fhdl.tools import list_signals, list_targets
from migen.genlib.cdc import MultiReg

from tt_harness.core.cdc import AsyncClearFlop


def name(signal) -> str:
    if isinstance(signal, Signal):
        return signal.backtrace[-1][0] if signal.backtrace else repr(signal)
    return type(signal).__name__


@dataclass
class Synchroniser:
    special: MultiReg
    stages: int
    source: object
    source_is_plain_register: bool
    source_domains: frozenset[str]
    odomain: str


@dataclass
class Flop:
    special: AsyncClearFlop
    domain: str
    clear: object
    clear_is_plain_register: bool
    clear_domains: frozenset[str]
    d_domains: frozenset[str]


@dataclass
class MemoryPort:
    special: _MemoryPort
    domain: str
    input_domains: frozenset[str]


@dataclass
class Analysis:
    domains: dict = field(default_factory=dict)  # Signal -> frozenset of domain names
    reads: list = field(default_factory=list)  # (domain, frozenset(targets), source signal)
    mixed: list = field(default_factory=list)
    multiregs: list = field(default_factory=list)
    flops: list = field(default_factory=list)
    instance_inputs: dict = field(default_factory=dict)
    memory_ports: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _expr_signals(expr) -> set:
    if isinstance(expr, (Constant, ClockSignal, ResetSignal, int, bool)):
        return set()
    return list_signals(expr)


def analyse(module, ports: dict, instances: dict[str, str]) -> Analysis:
    """Analyse `module` (it is finalised). `ports`: undriven Signal -> domain. `instances`:
    instance name -> the domain of its outputs (its inputs are reported)."""
    fragment = module.get_fragment()
    out = Analysis()
    reg: dict = {}
    for domain, statements in fragment.sync.items():
        for target in list_targets(statements):
            reg.setdefault(target, set()).add(domain)
    for signal, found in reg.items():
        if len(found) > 1:
            out.errors.append(f"{name(signal)} is assigned in clock domains {sorted(found)}")
    comb_targets = list_targets(fragment.comb)
    base: dict = {s: set(d) for s, d in reg.items()}

    def special_output(signal, domain: str, what: str) -> None:
        if signal in reg or signal in comb_targets or signal in ports:
            out.errors.append(f"{name(signal)} is the output of {what} and is assigned elsewhere")
        base.setdefault(signal, set()).add(domain)

    project_like: list[tuple[Instance, str]] = []
    for special in fragment.specials:
        if isinstance(special, MultiReg):
            special_output(special.o, special.odomain, "a MultiReg")
        elif isinstance(special, AsyncClearFlop):
            special_output(special.q, special.cd, "an AsyncClearFlop")
        elif isinstance(special, _MemoryPort):
            special_output(special.dat_r, special.clock.cd, "a memory port")
        elif isinstance(special, Memory):
            # Its ports are specials of their own (above). A read that is combinational from
            # the address is not block RAM, and its data is in no clock domain.
            for port in special.ports:
                if port.async_read and port.dat_r is not None:
                    out.errors.append(
                        f"a port of memory {special.name_override} reads asynchronously"
                    )
        elif isinstance(special, Instance):
            label = special.name_override or special.of
            outputs = [i for i in special.items if isinstance(i, Instance.Output)]
            if label not in instances and special.of not in instances:
                if outputs:
                    out.errors.append(f"instance {label} ({special.of}) has no declared domain")
                continue
            domain = instances.get(label, instances.get(special.of))
            for item in outputs:
                for signal in _expr_signals(item.expr):
                    special_output(signal, domain, f"instance {label}")
            project_like.append((special, label))
        else:
            out.errors.append(f"unknown special {type(special).__name__}")
    for signal, domain in ports.items():
        if signal in base or signal in comb_targets:
            out.errors.append(f"{name(signal)} is declared a port but is driven")
        base.setdefault(signal, set()).add(domain)

    # Combinational propagation to a fixed point.
    comb_reads: dict = {}
    for statement in fragment.comb:
        targets = list_targets([statement])
        for target in targets:
            comb_reads.setdefault(target, set()).update(list_signals([statement]) - targets)
    for target in comb_targets:
        if target in reg:
            out.errors.append(f"{name(target)} is assigned in comb and in sync")
    domains: dict = {s: set(d) for s, d in base.items()}
    changed = True
    while changed:
        changed = False
        for target, sources in comb_reads.items():
            new = set()
            for source in sources:
                new |= domains.get(source, set())
            if not new <= domains.setdefault(target, set()):
                domains[target] |= new
                changed = True
    out.domains = {s: frozenset(d) for s, d in domains.items()}
    unknown_seen: set = set()

    def of(expr) -> frozenset[str]:
        found: set = set()
        for signal in _expr_signals(expr):
            if signal not in base and signal not in comb_targets and signal not in unknown_seen:
                unknown_seen.add(signal)
                out.errors.append(f"{name(signal)} is read but nothing drives or declares it")
            found |= domains.get(signal, set())
        return frozenset(found)

    for target, sources in comb_reads.items():
        of(list(sources))
        if len(domains.get(target, ())) > 1:
            out.mixed.append(target)

    def plain_register(expr) -> bool:
        return (
            isinstance(expr, Signal)
            and expr in reg
            and len(reg[expr]) == 1
            and expr not in comb_targets
        )

    for domain, statements in fragment.sync.items():
        for statement in statements:
            targets = list_targets([statement])
            for source in list_signals([statement]) - targets:
                if of(source) - {domain}:
                    out.reads.append((domain, frozenset(targets), source))
    for special in fragment.specials:
        if isinstance(special, MultiReg):
            out.multiregs.append(
                Synchroniser(
                    special,
                    special.n,
                    special.i,
                    plain_register(special.i),
                    of(special.i),
                    special.odomain,
                )
            )
        elif isinstance(special, AsyncClearFlop):
            out.flops.append(
                Flop(
                    special,
                    special.cd,
                    special.clear,
                    plain_register(special.clear),
                    of(special.clear),
                    of(special.d),
                )
            )
    for special in fragment.specials:
        if isinstance(special, _MemoryPort):
            inputs = [
                x for x in (special.adr, special.dat_w, special.we, special.re) if x is not None
            ]
            out.memory_ports.append(MemoryPort(special, special.clock.cd, of(inputs)))
    for special, label in project_like:
        if label in out.instance_inputs:
            out.errors.append(
                f"two instances are called {label}: their inputs cannot be told apart"
            )
        out.instance_inputs[label] = {
            item.name: of(item.expr) for item in special.items if isinstance(item, Instance.Input)
        }
    return out
