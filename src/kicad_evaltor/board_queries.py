"""Placement measurements; user tests choose parts, domains, and limits."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from kicad_evaltor.board_file import arc_circle
from kicad_evaltor.pcbnew_bridge import (
    BoardGeometry,
    OutlineGeometry,
    Polygon,
    distance_to_polygon_edge,
    polygon_distance,
)
from kicad_evaltor.sexpr import SExpr, child, children, parse, value_of
from kicad_evaltor.values import parse_quantity


def pad_distance(
    geometry: BoardGeometry, reference: str, target: str, *, net: str | None = None
) -> float | None:
    """Planar edge gap, not a copper route or a decoupling-quality verdict."""
    ref, _, number = target.partition(".")
    a, b = geometry.footprint(reference), geometry.footprint(ref)
    if a is None or b is None:
        return None
    first = [p for p in a.pads if p.copper_layers and (net is None or p.net == net)]
    second = b.pads_numbered(number) if number else [p for p in b.pads if p.copper_layers]
    return min(
        (
            polygon_distance(p.polygon, q.polygon)
            for p in first
            for q in second
            if p.polygon and q.polygon
        ),
        default=None,
    )


@dataclass(frozen=True)
class DecouplingCandidate:
    ic_reference: str
    pin: str
    supply_net: str
    capacitor: str
    return_nets: tuple[str, ...]
    capacitance_f: float | None
    distance_mm: float | None
    dnp: bool


def decoupling_candidates(
    board,
    geometry: BoardGeometry,
    reference: str,
    *,
    capacitor_references: Iterable[str],
    return_nets: Iterable[str],
    power_pins: Iterable[str],
) -> tuple[DecouplingCandidate, ...]:
    """Enumerate candidates under explicit identity and return-domain selection.

    No distance, capacitance, DNP, or one-capacitor-per-pin policy is applied.
    """
    ic = board.footprint(reference)
    if ic is None:
        raise ValueError(f"Footprint {reference!r} missing")
    returns = set(return_nets)
    capacitor_references = tuple(capacitor_references)
    results = []
    for number in power_pins:
        pad = ic.pad(number)
        if pad is None:
            raise ValueError(f"Pin {reference}.{number} missing")
        for ref in capacitor_references:
            cap = board.footprint(ref)
            if cap is None:
                raise ValueError(f"Selected capacitor {ref!r} missing")
            grounded = tuple(sorted(cap.nets & returns))
            if pad.net not in cap.nets or not grounded:
                continue
            quantity = parse_quantity(cap.value, dimension="capacitance")
            results.append(
                DecouplingCandidate(
                    reference,
                    number,
                    pad.net,
                    ref,
                    grounded,
                    quantity.value,
                    pad_distance(geometry, ref, f"{reference}.{number}", net=pad.net),
                    cap.dnp,
                )
            )
    return tuple(results)


# ------------------------------------------------------------------ corners


@dataclass(frozen=True)
class BoardCorner:
    """One corner of the board outline and the radius it is rounded to.

    ``source`` says where the radius came from: ``"arc"`` is an ``Edge.Cuts``
    arc read from the board file (exact), ``"fit"`` is a circle fitted to the
    outline points of a corner drawn as short segments, and ``"vertex"`` is a
    sharp corner, radius 0. ``x``/``y`` is the corner's middle: the vertex, or
    the point halfway along the arc. ``turn_deg`` is how far the outline turns
    there, so a square corner is 90. ``convex`` is False for an inside corner,
    such as the far end of a notch.
    """

    x: float
    y: float
    radius_mm: float
    turn_deg: float
    convex: bool
    source: str


@dataclass(frozen=True)
class EdgeArc:
    """An arc drawn on ``Edge.Cuts`` at board level, as the file stores it."""

    center: tuple[float, float]
    radius: float
    sweep_deg: float
    start: tuple[float, float]
    mid: tuple[float, float]
    end: tuple[float, float]


def edge_cut_arcs(board_path: str | Path) -> tuple[EdgeArc, ...]:
    """The board-level ``gr_arc`` shapes on ``Edge.Cuts``, read from the file.

    Arcs inside footprints are not included; corners they draw are measured by
    fitting, like any corner drawn as segments.
    """
    document = parse(Path(board_path).read_text(encoding="utf-8", errors="replace"))
    arcs = []
    for node in children(document, "gr_arc"):
        if value_of(child(node, "layer")) != "Edge.Cuts":
            continue
        start, mid, end = (_xy(child(node, key)) for key in ("start", "mid", "end"))
        if start is None or mid is None or end is None:
            continue
        circle = arc_circle(start, mid, end)
        if circle is None:
            continue
        cx, cy, radius, _, sweep = circle
        arcs.append(EdgeArc((cx, cy), radius, abs(math.degrees(sweep)), start, mid, end))
    return tuple(arcs)


def board_corners(
    outline: OutlineGeometry,
    arcs: Iterable[EdgeArc] = (),
    *,
    min_turn_deg: float = 1.0,
) -> tuple[BoardCorner, ...]:
    """Every corner of the board's outer outline, with its radius.

    pcbnew hands the outline over as polygons, its arcs already cut into short
    chords. Consecutive vertices whose neighbouring point triples lie on one
    circle are one rounded corner, and a circle fitted through all of them
    gives its radius. When that circle is one of the file's ``Edge.Cuts``
    ``arcs`` (see :func:`edge_cut_arcs`), the arc's own radius is used instead,
    so a drawn 5 mm corner reads 5, not the chords' approximation of it. A
    vertex on no such run where the outline turns by at least ``min_turn_deg``
    is a sharp corner. An outline that is one circle all round has no corners.
    Cut-outs are not board corners and are left out.
    """
    arcs = tuple(arcs)
    corners: list[BoardCorner] = []
    for polygon in outline.polygons:
        corners.extend(_polygon_corners(polygon, arcs, min_turn_deg))
    return tuple(corners)


_Point = tuple[float, float]
_Circle = tuple[_Point, float]

_CHORD_RATIO = 4.0
"""How much longer one chord of a segment-approximated arc may be than its
neighbours. KiCad cuts an arc into equal chords with a half-length one at each
end; a straight side between two corners is far longer than either."""


def _polygon_corners(
    polygon: Polygon, arcs: tuple[EdgeArc, ...], min_turn_deg: float
) -> list[BoardCorner]:
    points = _simplified(polygon)
    n = len(points)
    if n < 3:
        return []
    orientation = math.copysign(1.0, _signed_area(points))
    turns = [_turn(points[i - 1], points[i], points[(i + 1) % n]) for i in range(n)]
    circles = [
        _circumcircle(points[i - 1], points[i], points[(i + 1) % n])
        if abs(turns[i]) > math.radians(0.05)
        else None
        for i in range(n)
    ]
    edges = [math.dist(points[i], points[(i + 1) % n]) for i in range(n)]
    # linked[i]: vertices i and i+1 lie on one circle with both their
    # neighbours, and the three edges around them are chords of similar length.
    # The second test matters: the two ends of a straight side between two
    # equal rounded corners also lie on one (huge) circle with their
    # neighbours, since any isosceles trapezoid is cyclic.
    linked = [
        _same_circle(circles[i], circles[(i + 1) % n])
        and max(edges[i - 1], edges[i], edges[(i + 1) % n])
        <= _CHORD_RATIO * min(edges[i - 1], edges[i], edges[(i + 1) % n])
        for i in range(n)
    ]
    if all(linked):
        return []

    # Runs of linked vertices, starting after an unlinked edge so that no run
    # wraps across the polygon's first vertex.
    first = next(i for i in range(n) if not linked[i - 1])
    runs: list[list[int]] = []
    for step in range(n):
        i = (first + step) % n
        if runs and linked[(i - 1) % n]:
            runs[-1].append(i)
        else:
            runs.append([i])

    corners: list[BoardCorner] = []
    used: set[int] = set()
    matched: set[int] = set()
    for run in runs:
        if len(run) < 2:
            continue
        guess = circles[run[0]]
        assert guess is not None
        # The arc's end points sit where it meets the straight edges; their own
        # triples reach off the arc, but they are on its circle.
        before, after = (run[0] - 1) % n, (run[-1] + 1) % n
        members = (
            ([before] if _on_circle(points[before], guess) else [])
            + run
            + ([after] if _on_circle(points[after], guess) else [])
        )
        fitted = _fit_circle([points[k] for k in members])
        if fitted is None:
            continue
        used.update(members)
        center, radius = fitted
        source = "fit"
        turn = sum(turns[k] for k in members)
        for index, arc in enumerate(arcs):
            if index not in matched and _same_circle((arc.center, arc.radius), fitted):
                matched.add(index)
                center, radius, source = arc.center, arc.radius, "arc"
                turn = math.copysign(math.radians(arc.sweep_deg), turn)
                break
        start, end = points[members[0]], points[members[-1]]
        mx, my = _arc_middle(center, radius, start, end, points[run[len(run) // 2]])
        corners.append(
            BoardCorner(
                x=round(mx, 4),
                y=round(my, 4),
                radius_mm=round(radius, 4),
                turn_deg=round(abs(math.degrees(turn)), 2),
                convex=turn * orientation > 0,
                source=source,
            )
        )

    # An arc too short to be cut into several chords shows up as one or two
    # sharp-looking vertices; the file's arc says what they really are.
    for index, arc in enumerate(arcs):
        if index in matched or not _near_polygon(arc.mid, points):
            continue
        circle = (arc.center, arc.radius)
        on_arc = [i for i in range(n) if i not in used and _on_circle(points[i], circle)]
        if not on_arc:
            continue
        used.update(on_arc)
        corners.append(
            BoardCorner(
                x=round(arc.mid[0], 4),
                y=round(arc.mid[1], 4),
                radius_mm=round(arc.radius, 4),
                turn_deg=round(arc.sweep_deg, 2),
                convex=_cross(arc.start, arc.mid, arc.end) * orientation > 0,
                source="arc",
            )
        )

    for i in range(n):
        if i in used or abs(turns[i]) < math.radians(min_turn_deg):
            continue
        corners.append(
            BoardCorner(
                x=round(points[i][0], 4),
                y=round(points[i][1], 4),
                radius_mm=0.0,
                turn_deg=round(abs(math.degrees(turns[i])), 2),
                convex=turns[i] * orientation > 0,
                source="vertex",
            )
        )
    return corners


def _xy(node: list[SExpr] | None) -> _Point | None:
    if node is None or len(node) < 3:
        return None
    try:
        return float(str(node[1])), float(str(node[2]))
    except ValueError:
        return None


def _simplified(polygon: Polygon) -> list[_Point]:
    """The polygon without repeated points or a closing copy of the first."""
    points: list[_Point] = []
    for point in polygon:
        if not points or math.dist(points[-1], point) > 1e-6:
            points.append((float(point[0]), float(point[1])))
    if len(points) > 1 and math.dist(points[0], points[-1]) <= 1e-6:
        points.pop()
    return points


def _signed_area(points: list[_Point]) -> float:
    return sum(
        points[i - 1][0] * points[i][1] - points[i][0] * points[i - 1][1]
        for i in range(len(points))
    )


def _cross(a: _Point, b: _Point, c: _Point) -> float:
    return (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])


def _turn(a: _Point, b: _Point, c: _Point) -> float:
    """The signed angle the path a -> b -> c turns through at b, in radians."""
    first = math.atan2(b[1] - a[1], b[0] - a[0])
    second = math.atan2(c[1] - b[1], c[0] - b[0])
    return (second - first + math.pi) % (2 * math.pi) - math.pi


def _circumcircle(a: _Point, b: _Point, c: _Point) -> _Circle | None:
    circle = arc_circle(a, b, c)
    return None if circle is None else ((circle[0], circle[1]), circle[2])


def _tolerance(radius: float) -> float:
    # KiCad cuts arcs into chords that stray up to 0.005 mm from the arc, and
    # board files round to 1 nm; this absorbs both at any radius.
    return 0.02 + 0.02 * radius


def _same_circle(first: _Circle | None, second: _Circle | None) -> bool:
    if first is None or second is None:
        return False
    tolerance = _tolerance(max(first[1], second[1]))
    return abs(first[1] - second[1]) <= tolerance and math.dist(first[0], second[0]) <= tolerance


def _on_circle(point: _Point, circle: _Circle) -> bool:
    return abs(math.dist(point, circle[0]) - circle[1]) <= _tolerance(circle[1])


def _near_polygon(point: _Point, points: list[_Point]) -> bool:
    return distance_to_polygon_edge(point[0], point[1], tuple(points)) <= 0.05


def _fit_circle(points: list[_Point]) -> _Circle | None:
    """Least-squares circle through points (Kasa's method), centred for precision."""
    if len(points) < 3:
        return None
    ox = sum(p[0] for p in points) / len(points)
    oy = sum(p[1] for p in points) / len(points)
    suu = suv = svv = suuu = svvv = suvv = svuu = 0.0
    for x, y in points:
        u, v = x - ox, y - oy
        suu += u * u
        suv += u * v
        svv += v * v
        suuu += u * u * u
        svvv += v * v * v
        suvv += u * v * v
        svuu += v * u * u
    det = suu * svv - suv * suv
    if abs(det) < 1e-18:
        return None
    rhs_u = (suuu + suvv) / 2
    rhs_v = (svvv + svuu) / 2
    uc = (rhs_u * svv - rhs_v * suv) / det
    vc = (suu * rhs_v - suv * rhs_u) / det
    radius = math.sqrt(uc * uc + vc * vc + (suu + svv) / len(points))
    return (ox + uc, oy + vc), radius


def _arc_middle(
    center: _Point, radius: float, start: _Point, end: _Point, inside: _Point
) -> _Point:
    """The point halfway along the arc from start to end that passes ``inside``."""
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    ai = math.atan2(inside[1] - center[1], inside[0] - center[0])
    sweep = (a1 - a0) % (2 * math.pi)
    if (ai - a0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    angle = a0 + sweep / 2
    return center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle)
