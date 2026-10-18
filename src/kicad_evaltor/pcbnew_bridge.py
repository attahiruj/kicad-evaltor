"""Board geometry from KiCad's own ``pcbnew``, run under KiCad's bundled Python.

Courtyards, pad outlines, the board outline, zone fill and connectivity are
real geometry, and KiCad already computes them exactly. This module runs
``_pcbnew_dump.py`` under KiCad's interpreter (see
:mod:`kicad_evaltor.utils.kicad_python`) and reads its JSON into the
dataclasses below. One subprocess serves every check on a board, because
:class:`~kicad_evaltor.core.context.DesignContext` caches the result.

All pcbnew access goes through here, so moving to KiCad's IPC API later only
changes this module.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_evaltor.board_file import unescape
from kicad_evaltor.utils.kicad_cli import DEFAULT_CLI_TIMEOUT
from kicad_evaltor.utils.kicad_python import find_kicad_python, run_kicad_python

DUMP_SCRIPT = Path(__file__).with_name("_pcbnew_dump.py")

Point = tuple[float, float]
Polygon = tuple[Point, ...]


class GeometryUnavailable(RuntimeError):
    """KiCad's Python could not be found or did not produce the board's geometry."""

    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class PadGeometry:
    number: str
    net: str
    x: float
    y: float
    copper_layers: tuple[str, ...]
    drill: float | None
    polygon: Polygon


@dataclass(frozen=True)
class TextGeometry:
    """A text item and the box KiCad draws it in. ``owner`` is "" for board text."""

    owner: str
    kind: str
    text: str
    layer: str
    visible: bool
    height: float
    thickness: float
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class FootprintGeometry:
    reference: str
    x: float
    y: float
    rotation: float
    side: str
    bbox: tuple[float, float, float, float]
    courtyard_front: tuple[Polygon, ...]
    courtyard_back: tuple[Polygon, ...]
    pads: tuple[PadGeometry, ...]
    texts: tuple[TextGeometry, ...]

    @property
    def courtyard(self) -> tuple[Polygon, ...]:
        """The courtyard on the footprint's own side."""
        return self.courtyard_back if self.side == "back" else self.courtyard_front

    def pad(self, number: str) -> PadGeometry | None:
        return next((p for p in self.pads if p.number == number and p.copper_layers), None)

    def pads_numbered(self, number: str) -> list[PadGeometry]:
        return [p for p in self.pads if p.number == number and p.copper_layers]


@dataclass(frozen=True)
class ZoneGeometry:
    net: str
    name: str
    layers: tuple[str, ...]
    rule_area: bool
    outline_area: float
    filled_area: dict[str, float]
    connected_pads: tuple[str, ...]


@dataclass(frozen=True)
class OutlineGeometry:
    closed: bool
    area: float
    bbox: tuple[float, float, float, float] | None
    polygons: tuple[Polygon, ...]
    holes: tuple[Polygon, ...] = ()

    def contains(self, x: float, y: float) -> bool:
        if not any(point_in_polygon(x, y, p) for p in self.polygons):
            return False
        return not any(point_in_polygon(x, y, h) for h in self.holes)

    def distance_to_edge(self, x: float, y: float) -> float:
        return min(
            (distance_to_polygon_edge(x, y, p) for p in (*self.polygons, *self.holes)),
            default=math.inf,
        )


@dataclass(frozen=True)
class BoardGeometry:
    kicad_version: str
    zones_filled: bool
    outline: OutlineGeometry
    footprints: tuple[FootprintGeometry, ...]
    zones: tuple[ZoneGeometry, ...]
    netclass_of: dict[str, str]
    texts: tuple[TextGeometry, ...]
    unconnected: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def footprint(self, reference: str) -> FootprintGeometry | None:
        return next((f for f in self.footprints if f.reference == reference), None)

    def all_texts(self) -> list[TextGeometry]:
        return [*self.texts, *(t for f in self.footprints for t in f.texts)]


def dump_board(
    board: str | Path,
    *,
    python: Path | None = None,
    cli_path: str | None = None,
    fill_zones: bool = True,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> BoardGeometry:
    """The board's geometry, computed by pcbnew under KiCad's Python.

    ``fill_zones`` refills every zone with KiCad's filler first, so pour areas
    do not depend on whether the file was saved after its last fill.
    """
    return geometry_from_dict(
        dump_board_json(
            board, python=python, cli_path=cli_path, fill_zones=fill_zones, timeout=timeout
        )
    )


def dump_board_json(
    board: str | Path,
    *,
    python: Path | None = None,
    cli_path: str | None = None,
    fill_zones: bool = True,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> dict[str, Any]:
    """The dump's raw JSON, which is what test recordings store."""
    interpreter = python or find_kicad_python(cli_path)
    if interpreter is None:
        raise GeometryUnavailable(
            "KiCad's Python (with pcbnew) was not found; install KiCad or set KICAD_PYTHON"
        )
    with tempfile.TemporaryDirectory(prefix="kicad-evaltor-geometry-") as workdir:
        out = Path(workdir) / "geometry.json"
        args = [str(Path(board).resolve()), str(out)]
        if fill_zones:
            args.append("--fill-zones")
        result = run_kicad_python(interpreter, DUMP_SCRIPT, args, timeout=timeout)
        if result.timed_out:
            raise GeometryUnavailable(result.stderr, timed_out=True)
        if not result.success or not out.exists():
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise GeometryUnavailable(
                f"pcbnew could not read {board}: {detail[-1] if detail else result.returncode}"
            )
        data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return data


def geometry_from_dict(data: dict[str, Any]) -> BoardGeometry:
    """Build :class:`BoardGeometry` from the dump's JSON (or a recording of it)."""
    outline = data.get("outline", {})
    polygons = outline.get("polygons", [])
    return BoardGeometry(
        kicad_version=str(data.get("kicad_version", "")),
        zones_filled=bool(data.get("zones_filled", False)),
        outline=OutlineGeometry(
            closed=bool(outline.get("closed", False)),
            area=float(outline.get("area", 0.0)),
            bbox=_box(outline.get("bbox")),
            polygons=tuple(_polygon(p["outline"]) for p in polygons),
            holes=tuple(_polygon(h) for p in polygons for h in p.get("holes", [])),
        ),
        footprints=tuple(_footprint(f) for f in data.get("footprints", [])),
        zones=tuple(
            ZoneGeometry(
                net=unescape(z["net"]),
                name=z.get("name", ""),
                layers=tuple(z.get("layers", [])),
                rule_area=bool(z.get("rule_area", False)),
                outline_area=float(z.get("outline_area", 0.0)),
                filled_area={k: float(v) for k, v in z.get("filled_area", {}).items()},
                connected_pads=tuple(z.get("connected_pads", [])),
            )
            for z in data.get("zones", [])
        ),
        netclass_of={
            unescape(name): info.get("netclass", "") for name, info in data.get("nets", {}).items()
        },
        texts=tuple(_text(t) for t in data.get("texts", [])),
        unconnected=int(data.get("unconnected", 0)),
        raw=data,
    )


def _footprint(data: dict[str, Any]) -> FootprintGeometry:
    courtyard = data.get("courtyard", {})
    return FootprintGeometry(
        reference=data["reference"],
        x=data["x"],
        y=data["y"],
        rotation=data.get("rotation", 0.0),
        side=data.get("side", "front"),
        bbox=_box(data.get("bbox")) or (data["x"], data["y"], data["x"], data["y"]),
        courtyard_front=tuple(_polygon(p["outline"]) for p in courtyard.get("front", [])),
        courtyard_back=tuple(_polygon(p["outline"]) for p in courtyard.get("back", [])),
        pads=tuple(
            PadGeometry(
                number=p["number"],
                net=unescape(p.get("net", "")),
                x=p["x"],
                y=p["y"],
                copper_layers=tuple(p.get("copper_layers", [])),
                drill=p.get("drill"),
                polygon=_polygon(p.get("polygon", [])),
            )
            for p in data.get("pads", [])
        ),
        texts=tuple(_text(t) for t in data.get("texts", [])),
    )


def _text(data: dict[str, Any]) -> TextGeometry:
    return TextGeometry(
        owner=data.get("owner", ""),
        kind=data.get("kind", ""),
        text=data.get("text", ""),
        layer=data.get("layer", ""),
        visible=bool(data.get("visible", True)),
        height=float(data.get("height", 0.0)),
        thickness=float(data.get("thickness", 0.0)),
        bbox=_box(data.get("bbox")) or (0.0, 0.0, 0.0, 0.0),
    )


def _polygon(points: Iterable[Sequence[float]]) -> Polygon:
    return tuple((float(p[0]), float(p[1])) for p in points)


def _box(values: Sequence[float] | None) -> tuple[float, float, float, float] | None:
    if not values:
        return None
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


# Plain polygon helpers over the outlines pcbnew reports. KiCad has already done
# the hard part (arcs approximated, shapes merged); these only measure.


def point_in_polygon(x: float, y: float, polygon: Polygon) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def _segment_distance(px: float, py: float, a: Point, b: Point) -> float:
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_polygon_edge(x: float, y: float, polygon: Polygon) -> float:
    n = len(polygon)
    return min(
        (_segment_distance(x, y, polygon[i], polygon[(i + 1) % n]) for i in range(n)),
        default=math.inf,
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def polygon_distance(first: Polygon, second: Polygon) -> float:
    """Gap between two polygons in mm; 0 when they touch or overlap."""
    if not first or not second:
        return math.inf
    if point_in_polygon(*first[0], second) or point_in_polygon(*second[0], first):
        return 0.0
    n, m = len(first), len(second)
    for i in range(n):
        for j in range(m):
            if _segments_intersect(first[i], first[(i + 1) % n], second[j], second[(j + 1) % m]):
                return 0.0
    return min(
        min(distance_to_polygon_edge(x, y, second) for x, y in first),
        min(distance_to_polygon_edge(x, y, first) for x, y in second),
    )


def polygon_inside(inner: Polygon, outline: OutlineGeometry) -> bool:
    """Whether every vertex of ``inner`` lies on the board, clear of its holes."""
    return all(outline.contains(x, y) for x, y in inner)
