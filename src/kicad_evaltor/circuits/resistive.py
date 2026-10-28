"""Linear DC analysis with explicit boundaries and zero-ohm node contraction."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Collection, Iterable, Mapping
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
    """Resistors between named nodes, with optional stop nodes.

    ``stops`` are nodes that :meth:`component` reaches but does not walk
    through, normally the supply and ground rails: a pull-up from one signal
    to ``+3V3`` and another from a second signal to the same rail do not make
    the two signals one resistor network. :meth:`equivalent_resistance` treats
    a stop node other than its two endpoints as open.
    """

    def __init__(self, resistors: Iterable[Resistor], *, stops: Iterable[str] = ()) -> None:
        self.resistors = tuple(resistors)
        self.stops = frozenset(stops)
        refs = [r.reference for r in self.resistors]
        if len(refs) != len(set(refs)):
            raise ValueError("Resistor references must be unique")

    @classmethod
    def from_netlist(
        cls,
        netlist,
        references: Iterable[str],
        *,
        stop_at: Callable[..., bool] | None = None,
    ) -> DCNetwork:
        """Read only explicitly selected parts; never infer a whole circuit model.

        ``stop_at`` is a net predicate such as
        :func:`kicad_evaltor.connectivity.power_rails`; every net of a selected
        resistor that it accepts becomes one of the network's ``stops``.
        """
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
        stops: set[str] = set()
        if stop_at is not None:
            for name in {n for r in resistors for n in (r.a, r.b)}:
                net = netlist.net(name)
                if net is not None and stop_at(net):
                    stops.add(name)
        return cls(resistors, stops=stops)

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
        """The resistance between nodes ``a`` and ``b`` with nothing else driven.

        Only the part of the network reached from ``a`` is solved, walking no
        further than the ``stops``. A resistor ending on a stop node other than
        ``a`` and ``b`` carries no current here, so a pull-up to an unrelated
        rail neither adds a path nor links two signals.
        """
        if a == b:
            return 0.0
        open_stops = self.stops - {a, b}
        network = self.component(a, stop_at=self.stops - {a})
        network = DCNetwork(
            (r for r in network.resistors if r.a not in open_stops and r.b not in open_stops),
            stops=network.stops,
        )
        if b not in {n for r in network.resistors for n in (r.a, r.b)}:
            raise NetworkUnavailable(f"No resistor path from {a} to {b}")
        result = network.solve({b: 0.0}, injected={a: 1.0})
        return result.voltages[a]

    def component(
        self, node: str, *, stop_at: Collection[str] | Callable[[str], bool] | None = None
    ) -> DCNetwork:
        """The electrically connected subset containing a node.

        The walk takes every resistor touching a reached node, and goes on
        from the far end unless that end is a stop: a node in ``stop_at`` (a
        collection of node names or a predicate on one), or by default in the
        network's own ``stops``. ``node`` itself is always walked from. Stop
        nodes that were reached stay in the result, as its ``stops``.
        """
        if stop_at is None:
            stop_at = self.stops
        is_stop = stop_at if callable(stop_at) else stop_at.__contains__
        reached = {node}
        expand = [node]
        stopped: set[str] = set()
        chosen: dict[str, Resistor] = {}
        while expand:
            here = expand.pop()
            for r in self.resistors:
                if r.reference in chosen or here not in (r.a, r.b):
                    continue
                chosen[r.reference] = r
                for end in (r.a, r.b):
                    if end in reached:
                        continue
                    reached.add(end)
                    if is_stop(end):
                        stopped.add(end)
                    else:
                        expand.append(end)
        return DCNetwork(chosen.values(), stops=stopped)

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
                (
                    Resistor(r.reference, r.a, r.b, r.ohms * factors.get(r.reference, 1))
                    for r in self.resistors
                ),
                stops=self.stops,
            )
            yield network.solve(fixed, injected=injected)
