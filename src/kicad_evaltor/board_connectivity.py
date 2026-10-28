"""How a net is routed: the graph of its copper, for pad-to-pad length.

Whether a net is fully connected is KiCad's question, and the DRC answers it
(``pcb.routing.complete``). This module answers the one DRC does not: how long
the copper path between two particular pads is. Tracks come from the board file;
pad outlines come from pcbnew, so "this track ends on that pad" is decided
against the pad's real shape.

Nodes are track ends, one per copper layer. A via joins its position on every
layer it spans, and a track end that lands part-way along another track of the
same net (a T-junction) splits that track.

Pad copper conducts too. A route often passes *through* a part rather than
ending at it: a USB pair routed flow-through an ESD array enters pin 1, crosses
to pin 6 under the package and leaves from pin 6, and the only copper joining
those pieces is the pads. So when the net's pads are given, every two nodes that
land on the same pad are joined, at the straight distance between the landing
points (inside a pad the signal crosses a plane of copper, and the straight line
is the shortest way across it). A plated-through pad joins its copper layers the
same way, plus the board height between them when via height is counted. KiCad's
own net length counts tracks and vias only, so a route through pad copper
measures slightly longer here than its share of KiCad's net total, by the
distances crossed inside pads.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from kicad_evaltor.board_file import BoardTrack, BoardVia, FileBoard, arc_circle
from kicad_evaltor.pcbnew_bridge import BoardGeometry, PadGeometry, point_in_polygon

Node = tuple[float, float, str]

_GRID = 4  # decimal places: KiCad stores positions to 1 nm, 0.1 µm is plenty

ROUTE_METHOD = "track_via_pad_copper_graph"
"""``RouteMeasurement.method`` when pad copper conducts (the default)."""
TRACK_VIA_METHOD = "track_via_graph"
"""``RouteMeasurement.method`` when only tracks and vias conduct."""


def _key(x: float, y: float, layer: str) -> Node:
    return (round(x, _GRID), round(y, _GRID), layer)


@dataclass
class NetGraph:
    """One net's copper as a weighted graph; weights are track lengths in mm."""

    net: str
    edges: dict[Node, list[tuple[Node, float]]] = field(default_factory=dict)
    pads_joined: dict[str, set[Node]] = field(default_factory=dict)
    """Nodes on each pad whose copper was joined, keyed ``"REF.NUMBER"``."""

    def add(self, a: Node, b: Node, length: float) -> None:
        self.edges.setdefault(a, []).append((b, length))
        self.edges.setdefault(b, []).append((a, length))

    def shortest(self, sources: Iterable[Node], targets: Iterable[Node]) -> float | None:
        """Shortest copper distance from any source node to any target node."""
        found = self.shortest_path(sources, targets)
        return None if found is None else found[0]

    def shortest_path(
        self, sources: Iterable[Node], targets: Iterable[Node]
    ) -> tuple[float, list[Node]] | None:
        """The shortest path from any source to any target: its length and nodes."""
        goal = set(targets)
        best: dict[Node, float] = {}
        came_from: dict[Node, Node | None] = {}
        queue: list[tuple[float, Node, Node | None]] = [(0.0, n, None) for n in set(sources)]
        heapq.heapify(queue)
        while queue:
            distance, node, previous = heapq.heappop(queue)
            if node in best:
                continue
            best[node] = distance
            came_from[node] = previous
            if node in goal:
                path = [node]
                while (step := came_from[path[-1]]) is not None:
                    path.append(step)
                return distance, path[::-1]
            for neighbour, length in self.edges.get(node, ()):
                if neighbour not in best:
                    heapq.heappush(queue, (distance + length, neighbour, node))
        return None

    def pieces(self) -> list[set[Node]]:
        """The graph's connected pieces: copper that touches, as this graph sees it."""
        seen: set[Node] = set()
        found = []
        for start in self.edges:
            if start in seen:
                continue
            piece = {start}
            stack = [start]
            while stack:
                for neighbour, _ in self.edges.get(stack.pop(), ()):
                    if neighbour not in piece:
                        piece.add(neighbour)
                        stack.append(neighbour)
            seen |= piece
            found.append(piece)
        return found


def net_graph(
    board: FileBoard,
    net: str,
    *,
    include_vias: bool = False,
    pads: Iterable[tuple[str, PadGeometry]] = (),
) -> NetGraph:
    """The net's tracks and vias as a graph, joined through ``pads``' copper.

    With ``include_vias`` a via costs the height between the two layers it joins
    (``FileBoard.layer_distance``), so a route that changes layer is longer by
    the stretch of barrel it uses, as in KiCad's length tuning.

    ``pads`` are ``("REF.NUMBER", pad)`` pairs, normally :func:`net_pads`. Nodes
    landing on one pad are joined at the straight distance between them (plus
    the layer height across a plated-through pad when ``include_vias``). With no
    pads, only tracks and vias conduct, as before.
    """
    graph = NetGraph(net)
    tracks = board.tracks_on(net)
    for track in tracks:
        # Where another track ends part-way along this one. KiCad joins an end
        # anywhere within the track's width, so the end need not sit on the
        # centreline: the split point and the end itself are tied together.
        landings: dict[float, list[tuple[float, float]]] = {}
        for other in tracks:
            if other is track or other.layer != track.layer:
                continue
            for end in _ends(other):
                t = _position_along(track, end)
                if t is not None:
                    landings.setdefault(t, []).append(end)
        for via in board.vias_on(net):
            if track.layer in _via_layers(board, via):
                point = (via.x, via.y)
                t = _position_along(track, point)
                if t is not None:
                    landings.setdefault(t, []).append(point)
        cuts = sorted({0.0, 1.0, *landings})
        length = track.length
        points = [_point_at(track, t) for t in cuts]
        for (t0, p0), (t1, p1) in zip(zip(cuts, points), zip(cuts[1:], points[1:])):
            graph.add(_key(*p0, track.layer), _key(*p1, track.layer), length * (t1 - t0))
        for t, ends in landings.items():
            split = _point_at(track, t)
            for end in ends:
                if _key(*split, track.layer) != _key(*end, track.layer):
                    graph.add(
                        _key(*split, track.layer), _key(*end, track.layer), math.dist(split, end)
                    )
    for via in board.vias_on(net):
        layers = _via_layers(board, via)
        for i, a in enumerate(layers):
            for b in layers[i + 1 :]:
                height = board.layer_distance(a, b) if include_vias else 0.0
                graph.add(_key(via.x, via.y, a), _key(via.x, via.y, b), height)
    for label, pad in pads:
        on_pad = pad_nodes(graph, pad)
        graph.pads_joined.setdefault(label, set()).update(on_pad)
        for i, here in enumerate(on_pad):
            for there in on_pad[i + 1 :]:
                across = math.dist(here[:2], there[:2])
                if include_vias and here[2] != there[2]:
                    across += board.layer_distance(here[2], there[2])
                graph.add(here, there, across)
    return graph


def net_pads(geometry: BoardGeometry, net: str) -> list[tuple[str, PadGeometry]]:
    """Every copper pad on ``net``, as ``("REF.NUMBER", pad)`` pairs."""
    return [
        (f"{fp.reference}.{pad.number}", pad)
        for fp in geometry.footprints
        for pad in fp.pads
        if pad.net == net and pad.copper_layers
    ]


def pad_nodes(graph: NetGraph, pads: PadGeometry | Sequence[PadGeometry]) -> list[Node]:
    """Graph nodes that lie on the pads' copper, on a layer the pad is on.

    Several pads can share one number (a split exposed pad, a connector's
    shield tabs); a route reaching any of them reaches the pin.
    """
    group = [pads] if isinstance(pads, PadGeometry) else list(pads)
    found = []
    for node in graph.edges:
        x, y, layer = node
        for pad in group:
            if layer in pad.copper_layers and (
                point_in_polygon(x, y, pad.polygon) if pad.polygon else (x, y) == (pad.x, pad.y)
            ):
                found.append(node)
                break
    return found


def pad_to_pad_length(
    board: FileBoard,
    geometry: BoardGeometry,
    net: str,
    start: PadGeometry | Sequence[PadGeometry],
    end: PadGeometry | Sequence[PadGeometry],
    *,
    include_vias: bool | None = None,
    die_lengths: tuple[float, float] = (0.0, 0.0),
    through_pads: bool = True,
) -> float | None:
    """Length of the shortest route between two pads, or None if not measured.

    Track length, plus via height when ``include_vias`` (None follows the
    project's setting), plus the two pads' pad-to-die lengths. With
    ``through_pads`` (the default) the route may cross the copper of other pads
    on the net, as a flow-through ESD array is routed; see the module docstring.

    None says only that this graph found no path. :func:`measure_route` gives
    the reason (start pad not reached, end pad not reached, or the copper
    splitting into pieces) and the pads the route crossed.
    """
    return measure_route(
        board,
        geometry,
        net,
        start,
        end,
        include_vias=include_vias,
        die_lengths=die_lengths,
        through_pads=through_pads,
    ).length_mm


def _ends(track: BoardTrack) -> tuple[tuple[float, float], tuple[float, float]]:
    return (track.start, track.end)


def _position_along(track: BoardTrack, point: tuple[float, float]) -> float | None:
    """Where ``point`` lies along a straight track, as 0..1, or None if off it.

    Only straight segments are split; an arc is joined at its ends only.
    """
    if track.mid is not None:
        circle = arc_circle(track.start, track.mid, track.end)
        if circle is None:
            return None
        x, y, radius, start_angle, sweep = circle
        if abs(math.dist(point, (x, y)) - radius) > track.width / 2:
            return None
        angle = math.atan2(point[1] - y, point[0] - x)
        progress = (
            (angle - start_angle) % (2 * math.pi)
            if sweep > 0
            else (start_angle - angle) % (2 * math.pi)
        )
        t = progress / abs(sweep)
        return t if 0 < t < 1 else None
    (ax, ay), (bx, by) = track.start, track.end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return None
    t = ((point[0] - ax) * dx + (point[1] - ay) * dy) / length2
    if not 0.0 < t < 1.0:
        return None
    off = math.hypot(point[0] - (ax + t * dx), point[1] - (ay + t * dy))
    return t if off <= track.width / 2 else None


def _point_at(track: BoardTrack, t: float) -> tuple[float, float]:
    if t == 0.0:
        return track.start
    if t == 1.0:
        return track.end
    if track.mid is not None:
        circle = arc_circle(track.start, track.mid, track.end)
        if circle is not None:
            x, y, radius, angle, sweep = circle
            return x + radius * math.cos(angle + t * sweep), y + radius * math.sin(
                angle + t * sweep
            )
    (ax, ay), (bx, by) = track.start, track.end
    return (ax + t * (bx - ax), ay + t * (by - ay))


def _via_layers(board: FileBoard, via: BoardVia) -> list[str]:
    copper = board.copper_layers
    if len(via.layers) >= 2 and via.layers[0] in copper and via.layers[-1] in copper:
        first, last = copper.index(via.layers[0]), copper.index(via.layers[-1])
        low, high = min(first, last), max(first, last)
        return copper[low : high + 1]
    return copper


@dataclass(frozen=True)
class RouteMeasurement:
    """One pad-to-pad route measured through a net's copper graph.

    ``status`` is ``"measured"`` or ``"unresolved"``. When unresolved,
    ``reason`` says why in words and ``reason_code`` as one of
    ``start_not_reached``, ``end_not_reached`` or ``split``. ``pads_crossed``
    names the pads (``"U1.1"``) whose copper the measured path runs over, other
    than the start and end pads.
    """

    length_mm: float | None
    net: str
    status: str
    via_height_counted: bool
    die_lengths_mm: tuple[float, float]
    method: str = TRACK_VIA_METHOD
    limitations: tuple[str, ...] = ()
    reason: str | None = None
    reason_code: str | None = None
    pads_crossed: tuple[str, ...] = ()
    pieces: int | None = None
    """How many separate pieces the net's copper graph has (1 when whole)."""


def measure_route(
    board: FileBoard,
    geometry: BoardGeometry,
    net: str,
    start: PadGeometry | Sequence[PadGeometry],
    end: PadGeometry | Sequence[PadGeometry],
    *,
    include_vias: bool | None = None,
    die_lengths: tuple[float, float] = (0.0, 0.0),
    through_pads: bool = True,
) -> RouteMeasurement:
    """The shortest copper route between two pads, or why there is none.

    The graph holds the net's tracks and vias and, with ``through_pads`` (the
    default; ``method`` is ``"track_via_pad_copper_graph"``), the copper of
    every pad on the net, so a route through an in-line part's pads measures.
    ``through_pads=False`` is the track/via-only graph of earlier releases.

    This API does not infer a routing defect when the graph has no answer; DRC
    remains the source for routing completeness. Zone copper is not traversed.
    """
    vias = board.uses_via_height if include_vias is None else include_vias
    if any(not math.isfinite(v) or v < 0 for v in die_lengths):
        raise ValueError("die_lengths must be finite and nonnegative")
    limitations = []
    if any(z.net == net and not z.rule_area for z in geometry.zones):
        limitations.append("Zone copper is excluded from track/via route lengths")
    if not through_pads:
        limitations.append("Pad copper is not traversed (through_pads=False)")
    pads = net_pads(geometry, net)
    graph = net_graph(board, net, include_vias=vias, pads=pads if through_pads else ())
    method = ROUTE_METHOD if through_pads else TRACK_VIA_METHOD
    sources, targets = pad_nodes(graph, start), pad_nodes(graph, end)
    pieces = len(graph.pieces())

    def unresolved(code: str, reason: str) -> RouteMeasurement:
        return RouteMeasurement(
            None,
            net,
            "unresolved",
            vias,
            die_lengths,
            method=method,
            limitations=tuple(limitations),
            reason=reason,
            reason_code=code,
            pieces=pieces,
        )

    if not sources:
        return unresolved(
            "start_not_reached", f"Start pad not reached: no track or via of {net} lands on it"
        )
    if not targets:
        return unresolved(
            "end_not_reached", f"End pad not reached: no track or via of {net} lands on it"
        )
    found = graph.shortest_path(sources, targets)
    if found is None:
        return unresolved(
            "split",
            f"The copper of {net} splits into {pieces} pieces and the start and end "
            "pads lie in different ones",
        )
    length, path = found
    ends = _as_list(start) + _as_list(end)
    on_path = set(path)
    crossed = [
        label
        for label, pad in pads
        if pad not in ends and graph.pads_joined.get(label, set()) & on_path
    ]
    return RouteMeasurement(
        length + sum(die_lengths),
        net,
        "measured",
        vias,
        die_lengths,
        method=method,
        limitations=tuple(limitations),
        pads_crossed=tuple(dict.fromkeys(crossed)),
        pieces=pieces,
    )


def _as_list(pads: PadGeometry | Sequence[PadGeometry]) -> list[PadGeometry]:
    return [pads] if isinstance(pads, PadGeometry) else list(pads)


@dataclass(frozen=True)
class PathSegment:
    """One net's stretch of a multi-net path, from one pad to the next part's pad."""

    net: str
    start: str
    """The pad(s) the stretch starts on, ``"REF.NUMBER"``, several joined by ``/``."""
    end: str
    route: RouteMeasurement
    kicad_net_length_mm: float
    """KiCad's length for the whole net (:meth:`FileBoard.net_length`), the
    number its length tuning shows: every track and via on the net, not only
    this stretch."""

    @property
    def length_mm(self) -> float | None:
        return self.route.length_mm


@dataclass(frozen=True)
class PathMeasurement:
    """A signal's pad-to-pad length across several nets joined by in-line parts.

    ``length_mm`` sums the segments' copper lengths, or is None when any segment
    is unresolved or no chain of nets exists (``reason`` says which and why).
    The in-line parts' bodies (resistor, ferrite) are not copper and add
    nothing. ``kicad_net_length_mm`` sums KiCad's whole-net lengths over the
    path's nets, the figure KiCad's length tuning shows.
    """

    start: str
    end: str
    status: str
    length_mm: float | None
    nets: tuple[str, ...]
    segments: tuple[PathSegment, ...]
    kicad_net_length_mm: float | None
    via_height_counted: bool
    die_lengths_mm: tuple[float, float]
    method: str = ROUTE_METHOD
    reason: str | None = None
    hops: tuple[str, ...] = ()
    """The in-line parts the path crosses from one net to the next, in order."""
    start_net: str | None = None
    end_net: str | None = None
    """The nets the start and end pads are on, set whenever the pads were found,
    so a caller can compare them with the schematic even when no path is."""


_Hop = tuple[str, str, str, list[str], list[str]]
"""(from net, to net, part reference, its pads on the first, its pads on the second)"""


def measure_path_length(
    board: FileBoard,
    geometry: BoardGeometry,
    start: str,
    end: str,
    *,
    through: Sequence[str] = (),
    include_vias: bool | None = None,
    die_lengths: tuple[float, float] = (0.0, 0.0),
    through_pads: bool = True,
    expect_nets: tuple[str, str] | None = None,
) -> PathMeasurement:
    """Pad-to-pad length of a signal that crosses in-line parts onto other nets.

    ``start`` and ``end`` are pads written ``"REF.NUMBER"``; ``through`` names
    the in-line parts (``["R14"]``, ``["U1", "R3"]``) the signal may cross. The
    chain of nets comes from those parts' pads: from the start pad's net, a
    listed part with a pad on that net and another on a different net leads to
    that net, until the end pad's net is reached (fewest hops wins). A listed
    part whose signal pads stay on one net, such as a flow-through ESD array,
    needs no hop: its pad copper is crossed within the segment and shows in
    that segment's ``route.pads_crossed``.

    Each segment runs from the previous pad (the start pad, or the in-line
    part's pad on that net) to the next and is measured by
    :func:`measure_route`. ``die_lengths`` are the outer pads' pad-to-die
    lengths, added to the first and last segments.

    The board alone says which part carries a reference, so on a board out of
    step with its schematic this measures whatever now sits there.
    ``expect_nets=(start net, end net)`` guards against that: when either pad
    is on another net the result is unresolved and says which, rather than a
    length of the wrong path. ``start_net``/``end_net`` are reported either way.
    """
    vias = board.uses_via_height if include_vias is None else include_vias
    found: dict[str, str] = {}

    def failed(reason: str) -> PathMeasurement:
        return PathMeasurement(
            start,
            end,
            "unresolved",
            None,
            (),
            (),
            None,
            vias,
            die_lengths,
            reason=reason,
            start_net=found.get("start"),
            end_net=found.get("end"),
        )

    first, error = _pad_group(geometry, start)
    if error:
        return failed(error)
    last, error = _pad_group(geometry, end)
    if error:
        return failed(error)
    start_net, end_net = first[0][1].net, last[0][1].net
    found.update(start=start_net, end=end_net)
    if expect_nets is not None:
        wrong = [
            f"{pad} is on {net or 'no net'}, expected {wanted}"
            for pad, net, wanted in (
                (start, start_net, expect_nets[0]),
                (end, end_net, expect_nets[1]),
            )
            if net != wanted
        ]
        if wrong:
            return failed("; ".join(wrong) + " (board out of step with the schematic?)")

    links: dict[str, list[_Hop]] = {}
    for ref in through:
        fp = geometry.footprint(ref)
        if fp is None:
            return failed(f"In-line part {ref} not found on board")
        by_net: dict[str, list[str]] = {}
        for pad in fp.pads:
            if pad.net and pad.copper_layers:
                by_net.setdefault(pad.net, []).append(pad.number)
        for a, a_pads in by_net.items():
            for b, b_pads in by_net.items():
                if a != b:
                    links.setdefault(a, []).append((a, b, ref, a_pads, b_pads))
    # Breadth-first over nets, so the chain with the fewest hops wins.
    came: dict[str, _Hop | None] = {start_net: None}
    frontier = [start_net]
    while frontier and end_net not in came:
        following = []
        for net in frontier:
            for hop in links.get(net, ()):
                if hop[1] not in came:
                    came[hop[1]] = hop
                    following.append(hop[1])
        frontier = following
    if end_net not in came:
        return failed(
            f"No chain of nets from {start} ({start_net}) to {end} ({end_net}) "
            f"through {', '.join(through) or 'no parts'}"
        )
    chain: list[_Hop] = []
    while (step := came[chain[-1][0] if chain else end_net]) is not None:
        chain.append(step)
    chain.reverse()

    nets = [start_net, *(hop[1] for hop in chain)]
    entries = [first, *(_part_pads(geometry, hop[2], hop[4]) for hop in chain)]
    exits = [*(_part_pads(geometry, hop[2], hop[3]) for hop in chain), last]
    segments = []
    for k, net in enumerate(nets):
        route = measure_route(
            board,
            geometry,
            net,
            [pad for _, pad in entries[k]],
            [pad for _, pad in exits[k]],
            include_vias=vias,
            die_lengths=(
                die_lengths[0] if k == 0 else 0.0,
                die_lengths[1] if k == len(nets) - 1 else 0.0,
            ),
            through_pads=through_pads,
        )
        segments.append(
            PathSegment(
                net,
                "/".join(label for label, _ in entries[k]),
                "/".join(label for label, _ in exits[k]),
                route,
                board.net_length(net, include_vias=vias),
            )
        )
    broken = next((s for s in segments if s.route.length_mm is None), None)
    return PathMeasurement(
        start,
        end,
        "measured" if broken is None else "unresolved",
        None if broken is not None else sum(s.route.length_mm or 0.0 for s in segments),
        tuple(nets),
        tuple(segments),
        sum(board.net_length(n, include_vias=vias) for n in dict.fromkeys(nets)),
        vias,
        die_lengths,
        method=ROUTE_METHOD if through_pads else TRACK_VIA_METHOD,
        reason=None
        if broken is None
        else f"{broken.net} ({broken.start} to {broken.end}): {broken.route.reason}",
        hops=tuple(hop[2] for hop in chain),
        start_net=start_net,
        end_net=end_net,
    )


def _pad_group(
    geometry: BoardGeometry, name: str
) -> tuple[list[tuple[str, PadGeometry]], str | None]:
    """The copper pads written ``"REF.NUMBER"``, or why there are none."""
    if "." not in name:
        return [], f"Pad {name!r} must be written REF.NUMBER, like 'U1.3'"
    ref, number = name.split(".", 1)
    fp = geometry.footprint(ref)
    group = fp.pads_numbered(number) if fp else []
    if not group:
        return [], f"Pad {name} not found on board"
    if not group[0].net:
        return [], f"Pad {name} has no net"
    return [(name, pad) for pad in group], None


def _part_pads(
    geometry: BoardGeometry, ref: str, numbers: list[str]
) -> list[tuple[str, PadGeometry]]:
    fp = geometry.footprint(ref)
    pads = fp.pads if fp else ()
    return [(f"{ref}.{p.number}", p) for p in pads if p.number in numbers and p.copper_layers]
