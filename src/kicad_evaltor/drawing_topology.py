"""Physical wire groups on one sheet, independent of label-based connectivity."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from kicad_evaltor.pcbnew_bridge import _segment_distance


@dataclass(frozen=True)
class DrawingGroups:
    pins: dict[tuple[str, str], int]
    wires: tuple[int, ...]

    def physically_joined(self, a: tuple[str, str], b: tuple[str, str]) -> bool:
        return a in self.pins and b in self.pins and self.pins[a] == self.pins[b]


def wire_groups(
    scene, *, coordinate_tolerance_mm: float = 1e-6, join_mid_wire: bool = True
) -> DrawingGroups:
    """Wire endpoints and direct pin contact join; bare crossings do not.

    An explicit junction joins crossing wires. Labels and power symbols cannot
    join disconnected drawn groups. This reports topology, not drawing quality.

    ``join_mid_wire`` decides what a wire end, or a pin, landing part-way along
    another wire with no junction there does. ``True`` (the default, kept for
    compatibility) joins it, which is how the drawing reads to a person: a T
    looks connected. KiCad does not connect it -- a wire connects only at its
    ends, or wherever a junction sits on it -- and ``False`` follows KiCad, so
    every group then lies inside one of KiCad's nets. In the test design's
    ``t_open`` variant a junction-less T puts ``U3`` pin 10 (the stem) in one
    drawn group with ``U3`` pin 1 and ``C8`` pin 1 (the bar) while KiCad's
    netlist keeps them on two nets.
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

    def at_end(point: tuple[float, float], wire) -> bool:
        return math.dist(point, wire.start) <= eps or math.dist(point, wire.end) <= eps

    lands = on if join_mid_wire else at_end
    for i, wire in enumerate(wires):
        for j, other in enumerate(wires[:i]):
            if any(lands(p, other) for p in (wire.start, wire.end)) or any(
                lands(p, wire) for p in (other.start, other.end)
            ):
                join(i, j)
    for junction in scene.junctions:
        hits = [i for i, w in enumerate(wires) if on(junction.at, w)]
        for i in hits[1:]:
            join(hits[0], i)
    junctions = [j.at for j in scene.junctions]
    for i, pin in enumerate(pins):
        # A junction on a pin connects it to any wire running through that point.
        at_junction = any(math.dist(pin.at, j) <= eps for j in junctions)
        for j, wire in enumerate(wires):
            if (on if at_junction else lands)(pin.at, wire):
                join(len(wires) + i, j)
        for j, other in enumerate(pins[:i]):
            if math.dist(pin.at, other.at) <= eps:
                join(len(wires) + i, len(wires) + j)
    return DrawingGroups(
        {(p.component, p.number): find(len(wires) + i) for i, p in enumerate(pins)},
        tuple(find(i) for i in range(len(wires))),
    )


@dataclass(frozen=True)
class LabelOnlyLink:
    """One net whose pins on a sheet sit in more than one drawn group.

    Something other than a wire -- a net label, a global or hierarchical label,
    a power symbol -- is all that joins the groups. ``groups`` lists each drawn
    group's pins on this net as ``ref.pin`` labels, sorted, so a reader can see
    which parts are wired together and which reach the net by name alone.
    """

    net: str
    groups: tuple[tuple[str, ...], ...]

    @property
    def parts(self) -> tuple[frozenset[str], ...]:
        """The references in each drawn group."""
        return tuple(frozenset(label.rsplit(".", 1)[0] for label in g) for g in self.groups)

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        """Pairs of parts on this net that no drawn group holds together.

        A part with pins in several groups is wired to whatever shares any one of
        them, so it is paired only with parts it shares no group with.
        """
        parts = self.parts
        refs = sorted(set().union(*parts))
        return tuple(
            (a, b) for a, b in combinations(refs, 2) if not any(a in g and b in g for g in parts)
        )


def label_only_links(
    scene,
    netlist,
    *,
    join_mid_wire: bool = False,
    include_hidden: bool = False,
    coordinate_tolerance_mm: float = 1e-6,
) -> tuple[LabelOnlyLink, ...]:
    """Nets whose pins on this sheet are joined only by labels or power symbols.

    A measurement, not a verdict: a decoupling capacitor tied to its rails by two
    power symbols appears here, and so does a bus run on labels on purpose.
    Which of those is a defect is the caller's decision -- for instance, flag a
    passive whose every link to the part it serves is listed here.

    ``netlist`` is KiCad's own (``schematic.get_netlist()``), so a net is
    exactly what KiCad joins. The drawn groups therefore follow KiCad's wire rule
    by default (``join_mid_wire=False``; see :func:`wire_groups`). Pins on other
    sheets are not on this ``scene`` and are not counted, and power symbols'
    pins never form a group of their own. A hidden pin cannot carry a wire, so
    ``include_hidden`` is off: KiCad joins it by name, and listing it would put
    every IC with hidden supply pins here.
    """
    groups = wire_groups(
        scene, coordinate_tolerance_mm=coordinate_tolerance_mm, join_mid_wire=join_mid_wire
    )
    by_net: dict[str, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    for component in scene.components:
        if component.is_power:
            continue
        for pin in component.pin_connections:
            if pin.hidden and not include_hidden:
                continue
            key = (pin.component, pin.number)
            net = netlist.net_of(*key)
            if net is None or key not in groups.pins:
                continue
            by_net[net][groups.pins[key]].append(pin.label)
    links = []
    for net, roots in sorted(by_net.items()):
        if len(roots) < 2:
            continue
        drawn = tuple(sorted(tuple(sorted(set(labels))) for labels in roots.values()))
        links.append(LabelOnlyLink(net, drawn))
    return tuple(links)
