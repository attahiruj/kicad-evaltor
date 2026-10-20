"""How a net is routed: the graph of its tracks and vias, for pad-to-pad length.

Whether a net is fully connected is KiCad's question, and the DRC answers it
(``pcb.routing.complete``). This module answers the one DRC does not: how long
the copper path between two particular pads is. Tracks come from the board file;
pad outlines come from pcbnew, so "this track ends on that pad" is decided
against the pad's real shape.

Nodes are track ends, one per copper layer. A via joins its position on every
layer it spans, and a track end that lands part-way along another track of the
same net (a T-junction) splits that track.
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


def _key(x: float, y: float, layer: str) -> Node:
    return (round(x, _GRID), round(y, _GRID), layer)


@dataclass
class NetGraph:
    """One net's copper as a weighted graph; weights are track lengths in mm."""

    net: str
    edges: dict[Node, list[tuple[Node, float]]] = field(default_factory=dict)

    def add(self, a: Node, b: Node, length: float) -> None:
        self.edges.setdefault(a, []).append((b, length))
        self.edges.setdefault(b, []).append((a, length))

    def shortest(self, sources: Iterable[Node], targets: Iterable[Node]) -> float | None:
        """Shortest copper distance from any source node to any target node."""
        goal = set(targets)
        best: dict[Node, float] = {}
        queue = [(0.0, node) for node in set(sources)]
        heapq.heapify(queue)
        while queue:
            distance, node = heapq.heappop(queue)
            if node in best:
                continue
            best[node] = distance
            if node in goal:
                return distance
            for neighbour, length in self.edges.get(node, ()):
                if neighbour not in best:
                    heapq.heappush(queue, (distance + length, neighbour))
        return None


def net_graph(board: FileBoard, net: str, *, include_vias: bool = False) -> NetGraph:
    """The net's tracks and vias as a graph.

    With ``include_vias`` a via costs the height between the two layers it joins
    (``FileBoard.layer_distance``), so a route that changes layer is longer by
    the stretch of barrel it uses, as in KiCad's length tuning.
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
    return graph


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
) -> float | None:
    """Length of the shortest route between two pads, or None if unrouted.

    Track length, plus via height when ``include_vias`` (None follows the
    project's setting), plus the two pads' pad-to-die lengths.
    """
    vias = board.uses_via_height if include_vias is None else include_vias
    graph = net_graph(board, net, include_vias=vias)
    sources, targets = pad_nodes(graph, start), pad_nodes(graph, end)
    if not sources or not targets:
        return None
    length = graph.shortest(sources, targets)
    return None if length is None else length + sum(die_lengths)


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
    length_mm: float | None
    net: str
    status: str
    via_height_counted: bool
    die_lengths_mm: tuple[float, float]
    method: str = "track_via_graph"
    limitations: tuple[str, ...] = ()


def measure_route(
    board: FileBoard,
    geometry: BoardGeometry,
    net: str,
    start: PadGeometry | Sequence[PadGeometry],
    end: PadGeometry | Sequence[PadGeometry],
    *,
    include_vias: bool | None = None,
    die_lengths: tuple[float, float] = (0.0, 0.0),
) -> RouteMeasurement:
    """A track/via path measurement with explicit unsupported copper cases.

    This API does not infer a routing defect when the graph has no answer.
    DRC remains the source for routing completeness. Zone copper and traversal
    through intermediate pad copper require a different route metric.
    """
    vias = board.uses_via_height if include_vias is None else include_vias
    if any(not math.isfinite(v) or v < 0 for v in die_lengths):
        raise ValueError("die_lengths must be finite and nonnegative")
    limitations = []
    if any(z.net == net and not z.rule_area for z in geometry.zones):
        limitations.append("Zone copper is excluded from track/via route lengths")
    length = pad_to_pad_length(
        board, geometry, net, start, end, include_vias=vias, die_lengths=die_lengths
    )
    if length is None:
        limitations.append("No track/via path; intermediate pad copper may conduct")
    return RouteMeasurement(
        length,
        net,
        "measured" if length is not None else "unresolved",
        vias,
        die_lengths,
        limitations=tuple(limitations),
    )
