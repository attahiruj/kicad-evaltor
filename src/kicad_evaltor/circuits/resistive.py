"""Linear DC analysis with explicit boundaries and zero-ohm node contraction."""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


class NetworkUnavailable(ValueError):
    """A network cannot establish the requested measurement."""


@dataclass(frozen=True)
class Resistor:
    reference: str
    a: str
    b: str
    ohms: float

    def __post_init__(self) -> None:
        if (
            not self.reference
            or not self.a
            or not self.b
            or isinstance(self.ohms, bool)
            or not math.isfinite(self.ohms)
            or self.ohms < 0
        ):
            raise ValueError(
                "A resistor needs identity, two nodes, and finite nonnegative resistance"
            )


@dataclass(frozen=True)
class DCSolution:
    voltages: dict[str, float]
    currents: dict[str, float | None]
    powers: dict[str, float]


class DCNetwork:
    def __init__(self, resistors: Iterable[Resistor]) -> None:
        self.resistors = tuple(resistors)
        refs = [r.reference for r in self.resistors]
        if len(refs) != len(set(refs)):
            raise ValueError("Resistor references must be unique")

    @classmethod
    def from_netlist(cls, netlist, references: Iterable[str]) -> DCNetwork:
        """Read only explicitly selected parts; never infer a whole circuit model."""
        from kicad_evaltor.values import parse_quantity

        resistors = []
        for ref in references:
            component = netlist.components.get(ref)
            pins = netlist.pins_of(ref)
            quantity = parse_quantity(
                component.value if component else None, dimension="resistance"
            )
            if len(pins) != 2 or quantity.value is None:
                raise NetworkUnavailable(
                    f"Cannot model {ref}: expected two connected pins and a resistance"
                )
            resistors.append(Resistor(ref, pins[0].net, pins[1].net, quantity.value))
        return cls(resistors)

    def solve(
        self, fixed: Mapping[str, float], *, injected: Mapping[str, float] | None = None
    ) -> DCSolution:
        """Ideal fixed voltages and current injections in SI units.

        A zero-ohm link has known zero dissipation but indeterminate branch
        current; this is exposed as None, not a fabricated current.
        """
        injected = injected or {}
        if not fixed or any(not math.isfinite(v) for v in (*fixed.values(), *injected.values())):
            raise NetworkUnavailable("Finite voltage boundaries are required")
        nodes = set(fixed) | set(injected) | {n for r in self.resistors for n in (r.a, r.b)}
        parent = {n: n for n in nodes}

        def root(n: str) -> str:
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        for r in self.resistors:
            if r.ohms == 0:
                parent[root(r.a)] = root(r.b)
        boundaries: dict[str, float] = {}
        for node, voltage in fixed.items():
            canonical = root(node)
            if canonical in boundaries and boundaries[canonical] != voltage:
                raise NetworkUnavailable("Conflicting voltages across a zero-ohm link")
            boundaries[canonical] = voltage
        # A Laplacian for a floating component is singular regardless of its
        # current injections. Check the topology before rounding in elimination
        # can turn its zero pivot into a small but apparently usable number.
        adjacent: dict[str, set[str]] = {root(n): set() for n in nodes}
        for r in self.resistors:
            a, b = root(r.a), root(r.b)
            adjacent[a].add(b)
            adjacent[b].add(a)
        reached = set(boundaries)
        pending = list(boundaries)
        while pending:
            for node in adjacent[pending.pop()] - reached:
                reached.add(node)
                pending.append(node)
        if floating := adjacent.keys() - reached:
            raise NetworkUnavailable(
                f"Floating resistor network: no voltage boundary for nodes {sorted(floating)}"
            )
        unknown = sorted({root(n) for n in nodes} - boundaries.keys())
        index = {n: i for i, n in enumerate(unknown)}
        size = len(unknown)
        matrix = [[0.0] * (size + 1) for _ in unknown]
        for node, amps in injected.items():
            if root(node) in index:
                matrix[index[root(node)]][-1] += amps
        for r in self.resistors:
            a, b = root(r.a), root(r.b)
            if r.ohms == 0 or a == b:
                continue
            g = 1.0 / r.ohms
            for here, there in ((a, b), (b, a)):
                if here not in index:
                    continue
                coefficients = matrix[index[here]]
                coefficients[index[here]] += g
                if there in index:
                    coefficients[index[there]] -= g
                else:
                    coefficients[-1] += g * boundaries[there]
        # Scaled partial pivoting avoids a fixed resistance-dependent cutoff.
        scales = [max((abs(v) for v in row[:-1]), default=0) for row in matrix]
        for col in range(size):

            def ratio(i):
                return abs(matrix[i][col]) / scales[i] if scales[i] else 0.0

            pivot = max(range(col, size), key=ratio)
            if ratio(pivot) <= 1e-14:
                raise NetworkUnavailable("Floating or singular resistor network")
            matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
            scales[col], scales[pivot] = scales[pivot], scales[col]
            for row in range(col + 1, size):
                factor = matrix[row][col] / matrix[col][col]
                for k in range(col, size + 1):
                    matrix[row][k] -= factor * matrix[col][k]
        solved = dict(boundaries)
        for row in reversed(range(size)):
            solved[unknown[row]] = (
                matrix[row][-1]
                - sum(matrix[row][j] * solved[unknown[j]] for j in range(row + 1, size))
            ) / matrix[row][row]
        voltages = {n: solved[root(n)] for n in nodes}
        currents = {
            r.reference: (voltages[r.a] - voltages[r.b]) / r.ohms if r.ohms else None
            for r in self.resistors
        }
        powers = {
            r.reference: (voltages[r.a] - voltages[r.b]) ** 2 / r.ohms if r.ohms else 0.0
            for r in self.resistors
        }
        if any(
            not math.isfinite(v)
            for v in (
                *voltages.values(),
                *(i for i in currents.values() if i is not None),
                *powers.values(),
            )
        ):
            raise NetworkUnavailable("Numerical overflow in resistor network")
        return DCSolution(voltages, currents, powers)

    def equivalent_resistance(self, a: str, b: str) -> float:
        if a == b:
            return 0.0
        network = self.component(a)
        if b not in {n for r in network.resistors for n in (r.a, r.b)}:
            raise NetworkUnavailable(f"No resistor path from {a} to {b}")
        result = network.solve({b: 0.0}, injected={a: 1.0})
        return result.voltages[a]

    def component(self, node: str) -> DCNetwork:
        """The electrically connected subset containing a node."""
        reached = {node}
        chosen: dict[str, Resistor] = {}
        changed = True
        while changed:
            changed = False
            for r in self.resistors:
                if r.reference not in chosen and (r.a in reached or r.b in reached):
                    chosen[r.reference] = r
                    reached.update((r.a, r.b))
                    changed = True
        return DCNetwork(chosen.values())

    def corners(
        self,
        fixed: Mapping[str, float],
        tolerances: Mapping[str, float],
        *,
        injected: Mapping[str, float] | None = None,
    ) -> Iterable[DCSolution]:
        """Enumerate independent caller-selected resistance extremes.

        Explicit exhaustive operation: no assumed tolerances or pass limits.
        """
        refs = {r.reference for r in self.resistors}
        if set(tolerances) - refs or any(
            not math.isfinite(t) or not 0 <= t < 1 for t in tolerances.values()
        ):
            raise ValueError("Tolerances must name network resistors and be fractions in [0, 1)")
        varied = tuple(tolerances)
        for signs in itertools.product((-1, 1), repeat=len(varied)):
            factors = {ref: 1 + sign * tolerances[ref] for ref, sign in zip(varied, signs)}
            network = DCNetwork(
                Resistor(r.reference, r.a, r.b, r.ohms * factors.get(r.reference, 1))
                for r in self.resistors
            )
            yield network.solve(fixed, injected=injected)
