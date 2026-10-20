"""KiCad-rendered ink measurements for user-authored layout assertions.

Only isolated scratch schematics are rendered. No source files are rewritten.
The SVG reader accepts KiCad's stroked vector output and rejects unsupported
features instead of silently falling back to bounding-box verdicts.
"""

from __future__ import annotations

import math
import re
import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from kicad_evaltor.geometry import BBox
from kicad_evaltor.pcbnew_bridge import _segment_distance, _segments_intersect, point_in_polygon
from kicad_evaltor.sexpr import Quoted, SExpr, child, children, dumps, head, value_of
from kicad_evaltor.utils.kicad_cli import find_kicad_cli, run_kicad_cli

Point = tuple[float, float]
_NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?"


class InkUnavailable(RuntimeError):
    """The requested ink could not be isolated or rendered faithfully."""


@dataclass(frozen=True)
class InkShape:
    points: tuple[Point, ...]
    width: float
    filled: bool = False

    @cached_property
    def bbox(self) -> BBox:
        xs, ys = zip(*self.points)
        r = self.width / 2
        return BBox(min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


@dataclass(frozen=True)
class InkMeasurement:
    distance_mm: float | None
    intersects: bool
    error_bound_mm: float
    method: str = "kicad_svg"
    # Witnessed penetration into the approximated strokes/fills. A shallow
    # intersection cannot establish a collision when it fits within the error.
    overlap_depth_mm: float = 0.0


def ink_distance(
    first: tuple[InkShape, ...], second: tuple[InkShape, ...], *, error_bound_mm: float = 0
) -> InkMeasurement:
    """Edge distance and intersection of two rendered objects; no pass threshold."""
    if not math.isfinite(error_bound_mm) or error_bound_mm < 0:
        raise ValueError("error_bound_mm must be finite and nonnegative")
    best = math.inf
    intersect = False
    depth = 0.0
    for a in first:
        for b in second:
            if a.bbox.clearance(b.bbox) > best:
                continue
            for body, other in ((a, b), (b, a)):
                if body.filled:
                    for p in other.points:
                        if point_in_polygon(*p, body.points):
                            intersect = True
                            best = 0.0
                            depth = max(
                                depth,
                                min(
                                    _segment_distance(*p, u, v)
                                    for u, v in zip(body.points, body.points[1:])
                                ),
                            )
                            if depth > error_bound_mm:
                                return InkMeasurement(
                                    0.0, True, error_bound_mm, overlap_depth_mm=depth
                                )
            for p, q in zip(a.points, a.points[1:]):
                for r, s in zip(b.points, b.points[1:]):
                    if (a.filled or b.filled) and _segments_intersect(p, q, r, s):
                        intersect = True
                        best = 0.0
                    d = (
                        0.0
                        if _segments_intersect(p, q, r, s)
                        else min(
                            _segment_distance(*p, r, s),
                            _segment_distance(*q, r, s),
                            _segment_distance(*r, p, q),
                            _segment_distance(*s, p, q),
                        )
                    )
                    gap = d - (a.width + b.width) / 2
                    intersect |= gap < 0
                    depth = max(depth, -gap)
                    best = min(best, max(0.0, gap))
                    if depth > error_bound_mm:
                        return InkMeasurement(0.0, True, error_bound_mm, overlap_depth_mm=depth)
    return InkMeasurement(
        None if best == math.inf else best, intersect, error_bound_mm, overlap_depth_mm=depth
    )


def _curve(points: list[Point], error: float, depth: int = 0) -> list[Point]:
    a, b = points[0], points[-1]
    if max((_segment_distance(*p, a, b) for p in points[1:-1]), default=0) <= error:
        return [a, b]
    if depth >= 20:
        raise InkUnavailable("Curve subdivision limit exceeded")
    layers = [points]
    while len(layers[-1]) > 1:
        layers.append(
            [((p[0] + q[0]) / 2, (p[1] + q[1]) / 2) for p, q in zip(layers[-1], layers[-1][1:])]
        )
    left = [layer[0] for layer in layers]
    right = [layer[-1] for layer in reversed(layers)]
    return _curve(left, error, depth + 1)[:-1] + _curve(right, error, depth + 1)


def _arc(start: Point, args: list[float], error: float) -> list[Point]:
    rx, ry, rotation, large, sweep, x, y = args
    end = (x, y)
    if start == end or rx == 0 or ry == 0:
        return [start, end]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rotation)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (start[0] - x) / 2, (start[1] - y) / 2
    xp, yp = c * dx + s * dy, -s * dx + c * dy
    factor = xp * xp / (rx * rx) + yp * yp / (ry * ry)
    if factor > 1:
        rx, ry = rx * math.sqrt(factor), ry * math.sqrt(factor)
    ratio = max(
        0.0,
        (rx * rx * ry * ry - rx * rx * yp * yp - ry * ry * xp * xp)
        / (rx * rx * yp * yp + ry * ry * xp * xp),
    )
    k = (-1 if bool(large) == bool(sweep) else 1) * math.sqrt(ratio)
    cxp, cyp = k * rx * yp / ry, -k * ry * xp / rx
    cx, cy = c * cxp - s * cyp + (start[0] + x) / 2, s * cxp + c * cyp + (start[1] + y) / 2
    a0 = math.atan2((yp - cyp) / ry, (xp - cxp) / rx)
    a1 = math.atan2((-yp - cyp) / ry, (-xp - cxp) / rx)
    angle = (a1 - a0) % (2 * math.pi)
    if not sweep:
        angle -= 2 * math.pi
    step = 2 * math.acos(max(-1.0, 1 - min(error / max(rx, ry), 1)))
    count = max(1, math.ceil(abs(angle) / step))
    if count > 100000:
        raise InkUnavailable("Arc subdivision limit exceeded")
    return [
        (
            cx
            + c * rx * math.cos(a0 + angle * i / count)
            - s * ry * math.sin(a0 + angle * i / count),
            cy
            + s * rx * math.cos(a0 + angle * i / count)
            + c * ry * math.sin(a0 + angle * i / count),
        )
        for i in range(count + 1)
    ]


def _paths(data: str, error: float) -> list[tuple[Point, ...]]:
    tokens = re.findall(r"[A-Za-z]|" + _NUMBER, data)
    paths: list[tuple[Point, ...]] = []
    points: list[Point] = []
    pos = (0.0, 0.0)
    command = ""
    i = 0
    counts = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Q": 4, "A": 7}
    while i < len(tokens):
        if tokens[i].isalpha():
            command = tokens[i]
            i += 1
            if command.upper() == "Z":
                if points:
                    points.append(points[0])
                    pos = points[0]
                command = ""
                continue
        op = command.upper()
        if op not in counts or i + counts[op] > len(tokens):
            raise InkUnavailable(f"Unsupported SVG path command {command!r}")
        try:
            args = [float(t) for t in tokens[i : i + counts[op]]]
        except ValueError as exc:
            raise InkUnavailable("Malformed SVG path") from exc
        i += counts[op]
        relative = command.islower()
        if op in ("M", "L", "C", "Q"):
            pairs = [
                (args[j] + (pos[0] if relative else 0), args[j + 1] + (pos[1] if relative else 0))
                for j in range(0, len(args), 2)
            ]
        if op == "M":
            if points:
                paths.append(tuple(points))
            pos = pairs[0]
            points = [pos]
            command = "l" if relative else "L"
        elif op == "L":
            pos = pairs[0]
            points.append(pos)
        elif op in ("H", "V"):
            pos = (
                (args[0] + (pos[0] if relative else 0), pos[1])
                if op == "H"
                else (pos[0], args[0] + (pos[1] if relative else 0))
            )
            points.append(pos)
        elif op in ("C", "Q"):
            points.extend(_curve([pos, *pairs], error)[1:])
            pos = pairs[-1]
        elif op == "A":
            if relative:
                args[-2] += pos[0]
                args[-1] += pos[1]
            points.extend(_arc(pos, args, error)[1:])
            pos = points[-1]
    if points:
        paths.append(tuple(points))
    return [p for p in paths if len(p) >= 2]


def svg_ink(svg: str, *, resolution_mm: float = 0.001) -> tuple[InkShape, ...]:
    """Read KiCad SVG in millimetres; unsupported SVG features are explicit errors."""
    if not math.isfinite(resolution_mm) or resolution_mm <= 0:
        raise ValueError("resolution_mm must be finite and positive")
    shapes: list[InkShape] = []

    def walk(node, inherited, transforms):
        style = dict(inherited)
        style.update(dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", node.get("style", ""))))
        for k in ("fill", "stroke", "stroke-width", "opacity", "stroke-opacity", "fill-opacity"):
            if node.get(k) is not None:
                style[k] = node.get(k)
        local = list(transforms)
        for name, body in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", node.get("transform", "")):
            numbers = [float(n) for n in re.findall(_NUMBER, body)]
            if name not in ("translate", "scale", "rotate", "matrix"):
                raise InkUnavailable(f"Unsupported SVG transform {name}")
            local.append((name, numbers))
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in ("defs", "metadata", "title", "desc") or float(style.get("opacity", "1")) == 0:
            return
        if tag == "text":
            if float(style.get("stroke-opacity", "1")) != 0:
                raise InkUnavailable("SVG contains unconverted text")
            return
        paths = []
        if tag == "path":
            paths = _paths(node.get("d", ""), resolution_mm)
        elif tag in ("polyline", "polygon"):
            nums = [float(v) for v in re.findall(_NUMBER, node.get("points", ""))]
            p = tuple(zip(nums[::2], nums[1::2]))
            paths = [(*p, p[0]) if tag == "polygon" and p else p]
        elif tag in ("circle", "ellipse"):
            cx, cy = float(node.get("cx", 0)), float(node.get("cy", 0))
            rx, ry = (
                float(node.get("rx", node.get("r", 0))),
                float(node.get("ry", node.get("r", 0))),
            )
            paths = [
                tuple(
                    _arc((cx + rx, cy), [rx, ry, 0, 0, 1, cx - rx, cy], resolution_mm)
                    + _arc((cx - rx, cy), [rx, ry, 0, 0, 1, cx + rx, cy], resolution_mm)[1:]
                )
            ]
        elif tag == "rect":
            if float(node.get("rx", 0)) or float(node.get("ry", 0)):
                raise InkUnavailable("Rounded SVG rectangles require a path representation")
            x, y, w, h = (float(node.get(k, 0)) for k in ("x", "y", "width", "height"))
            paths = [((x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y))]
        elif tag not in ("svg", "g"):
            raise InkUnavailable(f"Unsupported SVG element {tag}")
        width = (
            float(style.get("stroke-width", 0))
            if style.get("stroke", "none").lower() not in ("none", "white", "#fff", "#ffffff")
            and float(style.get("stroke-opacity", 1))
            else 0
        )
        filled = (
            style.get("fill", "black").lower() not in ("none", "white", "#fff", "#ffffff")
            and float(style.get("fill-opacity", 1)) > 0
        )
        if filled and len(paths) > 1:
            raise InkUnavailable("Compound filled SVG paths require winding-aware fill geometry")

        def transformed(p):
            x, y = p
            for name, args in reversed(local):
                if name == "translate":
                    x, y = x + args[0], y + (args[1] if len(args) > 1 else 0)
                elif name == "rotate":
                    angle = math.radians(args[0])
                    cx, cy = args[1:3] if len(args) == 3 else (0, 0)
                    x, y = (
                        cx + math.cos(angle) * (x - cx) - math.sin(angle) * (y - cy),
                        cy + math.sin(angle) * (x - cx) + math.cos(angle) * (y - cy),
                    )
                elif name == "scale":
                    sx, sy = args[0], args[-1]
                    if abs(sx) != abs(sy):
                        raise InkUnavailable("Nonuniform SVG scaling is unsupported")
                    x, y = x * sx, y * sy
                else:
                    a, b, c, d, e, f = args
                    if (a, b, c, d) != (1, 0, 0, 1):
                        raise InkUnavailable("Nontranslation SVG matrix is unsupported")
                    x, y = a * x + c * y + e, b * x + d * y + f
            return x, y

        scale = math.prod(abs(args[0]) for name, args in local if name == "scale")
        for points in paths:
            if len(points) > 1 and (width or filled):
                if filled and points[-1] != points[0]:
                    points = (*points, points[0])
                shapes.append(
                    InkShape(tuple(transformed(p) for p in points), width * scale, filled)
                )
        for sub in node:
            walk(sub, style, local)

    try:
        root = ET.fromstring(svg)
        walk(root, {"fill": "none", "stroke": "none"}, [])
    except (ValueError, IndexError, ET.ParseError) as exc:
        raise InkUnavailable(f"Invalid KiCad SVG: {exc}") from exc
    return tuple(shapes)


class InkRenderer:
    """Per-snapshot isolated rendering, cached only for this renderer's lifetime."""

    def __init__(self, schematic, *, resolution_mm: float = 0.001) -> None:
        self.schematic = schematic
        self.document = deepcopy(schematic.nodes())
        self.resolution_mm = resolution_mm
        self._cache: dict[tuple, tuple[InkShape, ...]] = {}

    def object_ink(
        self,
        *,
        reference: str | None = None,
        field: str | None = None,
        uuid: str | None = None,
        include_pins: bool = True,
    ) -> tuple[InkShape, ...]:
        """One placed symbol's body/pins, one field, or a standalone item by UUID."""
        key = (reference, field, uuid, include_pins)
        if key in self._cache:
            return self._cache[key]
        objects = [
            n
            for n in self.document
            if isinstance(n, list)
            and (
                (
                    head(n) == "symbol"
                    and child(n, "lib_id") is not None
                    and reference is not None
                    and (
                        value_of(child(n, "uuid")) == uuid
                        if uuid is not None
                        else any(
                            value_of(p, 1) == "Reference" and value_of(p, 2) == reference
                            for p in children(n, "property")
                        )
                    )
                )
                or (reference is None and uuid is not None and value_of(child(n, "uuid")) == uuid)
            )
        ]
        if len(objects) != 1:
            raise InkUnavailable("Object identity must select exactly one placed item")
        selected = deepcopy(objects[0])
        definitions: list[SExpr] = ["lib_symbols"]
        if reference is not None:
            for prop in children(selected, "property"):
                if value_of(prop, 1) == "Reference":
                    prop[2] = Quoted(reference)
            embedded_name = value_of(child(selected, "lib_name")) or value_of(
                child(selected, "lib_id")
            )
            found = [
                d
                for d in children(child(self.document, "lib_symbols"), "symbol")
                if value_of(d) == embedded_name
            ]
            if len(found) != 1:
                raise InkUnavailable(f"Missing embedded definition {embedded_name!r}")
            definitions.append(deepcopy(found[0]))
            if field is not None:
                props = [p for p in children(selected, "property") if value_of(p, 1) == field]
                if len(props) != 1:
                    raise InkUnavailable(f"Cannot isolate field {field!r}")
                # Render the instance as usual and select its stroked-text group
                # by content. This retains KiCad's rotation/mirror placement.
                content = value_of(props[0], 2)
            else:
                content = None
                for prop in children(selected, "property"):
                    prop[:] = [v for v in prop if not (isinstance(v, list) and head(v) == "hide")]
                    prop.append(["hide", "yes"])
            if not include_pins:
                for definition in children(definitions, "symbol"):
                    for sub in children(definition, "symbol"):
                        sub[:] = [v for v in sub if not (isinstance(v, list) and head(v) == "pin")]
        else:
            content = None
        keep = {"version", "generator", "generator_version", "uuid", "paper", "embedded_fonts"}
        document: list[SExpr] = [
            "kicad_sch",
            *(deepcopy(n) for n in self.document if isinstance(n, list) and head(n) in keep),
            definitions,
            selected,
        ]
        cli = self.schematic.cli_path() or find_kicad_cli()
        if cli is None:
            raise InkUnavailable("kicad-cli unavailable")
        with tempfile.TemporaryDirectory(prefix="evaltor-ink-") as tmp:
            work = Path(tmp)
            source = work / "isolated.kicad_sch"
            source.write_text(dumps(document), encoding="utf-8")
            result = run_kicad_cli(
                [
                    str(cli),
                    "sch",
                    "export",
                    "svg",
                    "-e",
                    "-n",
                    "-b",
                    "-o",
                    str(work / "svg"),
                    str(source),
                ],
                cwd=work,
                timeout=self.schematic.cli_timeout(),
            )
            produced = list((work / "svg").glob("*.svg"))
            if not result.success or len(produced) != 1:
                raise InkUnavailable(f"KiCad SVG render failed: {result.stderr or result.stdout}")
            svg = produced[0].read_text(encoding="utf-8")
        if reference is not None and field is not None:
            root = ET.fromstring(svg)
            selected_groups = []

            # Preserve inherited styles/transforms by pruning other geometry.
            def prune(node):
                if node.get("class") == "stroked-text":
                    desc = next((n.text for n in node if n.tag.endswith("}desc")), None)
                    if desc == content:
                        selected_groups.append(node)
                        return True
                    return False
                kept = [n for n in node if prune(n)]
                node[:] = kept
                return bool(kept) or node.tag.endswith("}svg")

            prune(root)
            if len(selected_groups) != 1:
                raise InkUnavailable(
                    "Compared text could not be uniquely isolated from rendered ink"
                )
            svg = ET.tostring(root, encoding="unicode")
        ink = svg_ink(svg, resolution_mm=self.resolution_mm)
        if field is not None and not ink:
            raise InkUnavailable("Compared field has no isolated ink")
        self._cache[key] = ink
        return ink


def measure_overlap(
    ctx,
    finding: dict,
    *,
    include_own_pins: bool,
    include_symbol_pins: bool = False,
    resolution_mm: float = 0.001,
) -> InkMeasurement:
    """Confirm an identified overlap candidate without changing caller thresholds.

    This works with complete overlap findings carrying UUID and sheet identity.
    It deliberately does not decide whether a distance or intersection passes.
    """
    tree = ctx.sheet_tree()
    sheet = tree.find(finding.get("sheet_path", "/")) if tree is not None else None
    schematic = tree.schematic_for(sheet) if tree is not None and sheet is not None else None
    if schematic is None:
        raise InkUnavailable("Finding's sheet could not be loaded")
    cache = schematic._ink_renderers
    if resolution_mm not in cache:
        cache[resolution_mm] = InkRenderer(schematic, resolution_mm=resolution_mm)
    renderer = cache[resolution_mm]
    own = finding.get("first_owner") is not None and finding.get("first_owner") == finding.get(
        "second_owner"
    )
    inks = []
    for label in ("first", "second"):
        if not finding.get(f"{label}_uuid"):
            raise InkUnavailable("Finding has no stable object UUID")
        kind = finding.get(f"{label}_kind")
        if kind not in ("text", "symbol"):
            raise InkUnavailable(f"Unsupported ink object kind {kind!r}")
        reference = finding.get(f"{label}_reference")
        inks.append(
            renderer.object_ink(
                reference=reference,
                field=finding.get(f"{label}_field")
                if kind == "text" and reference is not None
                else None,
                uuid=finding[f"{label}_uuid"],
                include_pins=(
                    include_symbol_pins
                    if finding.get("first_kind") == finding.get("second_kind") == "symbol"
                    else not own or include_own_pins
                ),
            )
        )
    return ink_distance(inks[0], inks[1], error_bound_mm=2 * resolution_mm)
