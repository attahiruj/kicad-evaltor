"""Physical wire groups on one sheet, independent of label-based connectivity."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kicad_evaltor.pcbnew_bridge import _segment_distance


@dataclass(frozen=True)
class DrawingGroups:
    pins: dict[tuple[str, str], int]
    wires: tuple[int, ...]

    def physically_joined(self, a: tuple[str, str], b: tuple[str, str]) -> bool:
        return a in self.pins and b in self.pins and self.pins[a] == self.pins[b]


def wire_groups(scene, *, coordinate_tolerance_mm: float = 1e-6) -> DrawingGroups:
    """Wire endpoints and direct pin contact join; bare crossings do not.

    An explicit junction joins crossing wires. Labels and power symbols cannot
    join disconnected drawn groups. This reports topology, not drawing quality.
    """
    if not math.isfinite(coordinate_tolerance_mm) or coordinate_tolerance_mm < 0:
        raise ValueError("coordinate_tolerance_mm must be finite and nonnegative")
    eps = coordinate_tolerance_mm
    wires = scene.wires
    pins = [p for c in scene.components if not c.is_power for p in c.pin_connections]
    parent = list(range(len(wires) + len(pins)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(a, b):
        parent[find(a)] = find(b)

    def on(point: tuple[float, float], wire) -> bool:
        return _segment_distance(*point, wire.start, wire.end) <= eps

    for i, wire in enumerate(wires):
        for j, other in enumerate(wires[:i]):
            if any(on(p, other) for p in (wire.start, wire.end)) or any(
                on(p, wire) for p in (other.start, other.end)
            ):
                join(i, j)
    for junction in scene.junctions:
        hits = [i for i, w in enumerate(wires) if on(junction.at, w)]
        for i in hits[1:]:
            join(hits[0], i)
    for i, pin in enumerate(pins):
        for j, wire in enumerate(wires):
            if on(pin.at, wire):
                join(len(wires) + i, j)
        for j, other in enumerate(pins[:i]):
            if math.dist(pin.at, other.at) <= eps:
                join(len(wires) + i, len(wires) + j)
    return DrawingGroups(
        {(p.component, p.number): find(len(wires) + i) for i, p in enumerate(pins)},
        tuple(find(i) for i in range(len(wires))),
    )
