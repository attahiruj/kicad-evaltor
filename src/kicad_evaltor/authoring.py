"""Small, policy-free measurements for composing application-specific tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kicad_evaltor.circuits import DCNetwork, NetworkUnavailable
from kicad_evaltor.netlist import FileNetlist
from kicad_evaltor.values import parse_quantity


@dataclass(frozen=True)
class ComponentMeasurement:
    reference: str
    value: float | None
    dimension: str
    nets: tuple[str, ...]
    reason: str = ""


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
