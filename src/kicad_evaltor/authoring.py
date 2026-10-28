"""Small, policy-free measurements for composing application-specific tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kicad_evaltor.circuits import DCNetwork, NetworkUnavailable
from kicad_evaltor.connectivity import NetFilter, never, power_rails, ref_prefix
from kicad_evaltor.netlist import AmbiguousNet, FileNetlist
from kicad_evaltor.values import parse_quantity

NetEnd = str | tuple[str, str]
"""A net by name (``"GND"``, matched across sheets) or by a pin, ``(ref, pin)``."""


@dataclass(frozen=True)
class ComponentMeasurement:
    reference: str
    value: float | None
    dimension: str
    nets: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class ResistorChain:
    """Resistors in series from one net to another.

    ``references`` in order from the first net; ``nets`` the nets passed,
    both ends included, so it is one longer than ``references``.
    """

    references: tuple[str, ...]
    nets: tuple[str, ...]
    ohms: float


@dataclass(frozen=True)
class Resistance:
    """The resistance between two nets and the chains that make it up.

    ``ohms`` combines the ``chains`` in parallel. Two ends on the same net
    read 0.0 with no chains.
    """

    a: str
    b: str
    ohms: float
    chains: tuple[ResistorChain, ...]


@dataclass(frozen=True)
class Strap:
    """A net held at a rail through resistors (or tied to it directly).

    ``rail`` is the rail with the lowest resistance to the net, ``ohms`` that
    resistance and ``chains`` the resistors that make it. ``alternatives``
    holds every other rail also within the bound, lowest first, so a
    divider between two rails is visible rather than hidden.
    """

    net: str
    rail: str
    ohms: float
    chains: tuple[ResistorChain, ...]
    alternatives: tuple[Resistance, ...] = ()


class CircuitQueries:
    def __init__(self, netlist: FileNetlist) -> None:
        self.netlist = netlist

    def pin_map(self, reference: str) -> dict[str, str]:
        return {p.pin: p.net for p in self.netlist.pins_of(reference)}

    def channel(
        self, reference: str, roles: Mapping[str, Iterable[str]]
    ) -> dict[str, tuple[str, ...]]:
        """Resolve caller-defined channel pin numbers without merging channels."""
        result = {}
        for role, numbers in roles.items():
            pins = [self.netlist.node(reference, n) for n in numbers]
            if not pins or any(p is None for p in pins):
                raise NetworkUnavailable(f"Missing {reference} pins for role {role}")
            result[role] = tuple(p.net for p in pins if p is not None)
        return result

    def components_between(
        self, a: str, b: str, references: Iterable[str], *, dimension: str
    ) -> tuple[ComponentMeasurement, ...]:
        selected = set(references)
        return tuple(
            ComponentMeasurement(
                c.ref,
                q.value,
                dimension,
                tuple(p.net for p in self.netlist.pins_of(c.ref)),
                q.reason,
            )
            for c in self.netlist.between(a, b, predicate=lambda c: c.ref in selected)
            for q in [parse_quantity(c.value, dimension=dimension)]
        )

    def capacitance(self, a: str, b: str, references: Iterable[str]) -> float:
        """Parallel capacitance of explicitly selected two-terminal parts."""
        values = self.components_between(a, b, references, dimension="capacitance")
        if any(v.value is None for v in values):
            raise NetworkUnavailable("A selected capacitor's value is unknown")
        return sum(v.value for v in values if v.value is not None)

    def pin_voltage(
        self, reference: str, pin: str, *, fixed: Mapping[str, float], resistors: Iterable[str]
    ) -> float:
        """DC voltage under caller-selected resistor and rail models.

        Does not assume a logic-high threshold, startup state, or IC behavior.
        """
        node = self.netlist.node(reference, pin)
        if node is None:
            raise NetworkUnavailable(f"Missing pin {reference}.{pin}")
        solved = DCNetwork.from_netlist(self.netlist, resistors).solve(fixed)
        if node.net not in solved.voltages:
            raise NetworkUnavailable(f"Pin {reference}.{pin} has no modeled voltage")
        return solved.voltages[node.net]

    def resistance_between(
        self,
        a: NetEnd,
        b: NetEnd,
        *,
        max_parts: int = 3,
        stop_at: NetFilter | None = power_rails,
        prefixes: Iterable[str] = ("R",),
    ) -> Resistance | None:
        """The resistance of the resistor chains joining two nets, or None.

        A chain is up to ``max_parts`` resistors in series, where every net
        between two of them joins those two parts and nothing else (no third
        pin, no test point), and is not a net ``stop_at`` accepts. Chains are
        combined in parallel, and a 0 ohm part makes the result 0.0. The two
        ends themselves are never stopped at, so asking for ``"GND"`` works
        with the default :func:`~kicad_evaltor.connectivity.power_rails`.

        A resistor is a fitted (not DNP) two-pin part whose reference letters
        are one of ``prefixes``. A resistor in a complete chain whose Value
        does not read as a resistance raises :class:`NetworkUnavailable`,
        since the result would otherwise be a guess. Unknown nets raise too.
        """
        if isinstance(max_parts, bool) or not isinstance(max_parts, int) or max_parts < 1:
            raise ValueError("max_parts must be a positive integer")
        start, end = self._net_name(a), self._net_name(b)
        if start == end:
            return Resistance(start, end, 0.0, ())
        wanted = {p.upper() for p in prefixes}
        stop = stop_at or never
        found: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

        def walk(net: str, refs: tuple[str, ...], nets: tuple[str, ...]) -> None:
            for node in self.netlist.nodes_on(net):
                if node.ref in refs or not self._is_resistor(node.ref, wanted):
                    continue
                far = {p.net for p in self.netlist.pins_of(node.ref)} - {net}
                if len(far) != 1:
                    continue
                (other,) = far
                chain = (*refs, node.ref)
                if other == end:
                    found.append((chain, (*nets, other)))
                elif (
                    len(chain) < max_parts
                    and other not in nets
                    and other != start
                    and self._series_joint(other, node.ref, wanted, stop)
                ):
                    walk(other, chain, (*nets, other))

        walk(start, (), (start,))
        if not found:
            return None
        chains = tuple(
            ResistorChain(refs, nets, sum(self._ohms(r) for r in refs)) for refs, nets in found
        )
        if any(c.ohms == 0 for c in chains):
            total = 0.0
        else:
            total = 1.0 / sum(1.0 / c.ohms for c in chains)
        return Resistance(start, end, float(f"{total:.12g}"), chains)

    def strapped(
        self,
        net: NetEnd,
        rails: Iterable[str],
        max_ohms: float,
        *,
        max_parts: int = 3,
        stop_at: NetFilter | None = power_rails,
        prefixes: Iterable[str] = ("R",),
    ) -> Strap | None:
        """Which of ``rails`` the net is held at, within ``max_ohms``, or None.

        Each rail is measured with :meth:`resistance_between`; the lowest
        resistance at or under ``max_ohms`` wins (the order of ``rails``
        breaks a tie) and the others within the bound are kept as
        ``alternatives``. A net that is itself one of the rails is strapped to
        it at 0.0. A rail name not in the netlist is skipped; the net must
        exist.
        """
        name = self._net_name(net)
        measured: list[Resistance] = []
        for rail in rails:
            try:
                rail_name = self._net_name(rail)
            except NetworkUnavailable:
                continue
            found = self.resistance_between(
                name, rail_name, max_parts=max_parts, stop_at=stop_at, prefixes=prefixes
            )
            if found is not None and found.ohms <= max_ohms:
                measured.append(found)
        if not measured:
            return None
        ordered = sorted(measured, key=lambda r: r.ohms)
        best = ordered[0]
        return Strap(name, best.b, best.ohms, best.chains, tuple(ordered[1:]))

    def _net_name(self, end: NetEnd) -> str:
        if isinstance(end, tuple):
            found = self.netlist.net_of(*end)
            if found is None:
                raise NetworkUnavailable(f"Pin {end[0]}.{end[1]} is on no net")
            return found
        if self.netlist.net(end) is not None:
            return end
        try:
            net = self.netlist.net_named(end)
        except AmbiguousNet as exc:
            raise NetworkUnavailable(str(exc)) from exc
        if net is None:
            raise NetworkUnavailable(f"No net named {end!r}")
        return net.name

    def _is_resistor(self, ref: str, prefixes: set[str]) -> bool:
        component = self.netlist.components.get(ref)
        return (
            component is not None
            and not component.dnp
            and ref_prefix(ref).upper() in prefixes
            and len(self.netlist.pins_of(ref)) == 2
        )

    def _series_joint(self, net: str, ref: str, prefixes: set[str], stop: NetFilter) -> bool:
        """Whether ``net`` joins ``ref`` to exactly one other resistor and nothing else."""
        found = self.netlist.net(net)
        if found is None or stop(found) or len(found.nodes) != 2:
            return False
        refs = [n.ref for n in found.nodes]
        return ref in refs and any(r != ref and self._is_resistor(r, prefixes) for r in refs)

    def _ohms(self, ref: str) -> float:
        component = self.netlist.components[ref]
        value = parse_quantity(component.value, dimension="resistance").value
        if value is None:
            raise NetworkUnavailable(f"Cannot read {ref} value {component.value!r} as a resistance")
        return value
