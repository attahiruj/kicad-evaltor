"""The board's shape and where parts sit on it: outline, on-board, placement, holes.

Outline and courtyard geometry come from pcbnew; side and rotation from the file.
"""

from __future__ import annotations

import fnmatch
import math
from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.geometry import normalize_angle
from kicad_evaltor.pcbnew_bridge import FootprintGeometry, OutlineGeometry, Polygon


@dataclass
class BoardOutlineParams(CheckParams):
    width_mm: float | None = None
    height_mm: float | None = None
    tolerance_mm: float = 0.1
    max_width_mm: float | None = None
    max_height_mm: float | None = None
    min_area_mm2: float | None = None
    max_area_mm2: float | None = None


@register
class BoardOutlineCheck(Check[BoardOutlineParams]):
    """``Edge.Cuts`` forms one closed outline, of the size asked for.

    Width and height are the outline's bounding box; area is the outline's own
    area, so rounded corners and cut-outs count. With no size given, only a
    closed outline is required.
    """

    id = "pcb.board.outline"
    name = "Board Outline"
    description = "Checks that the board outline is closed and the right size"
    category = CheckCategory.PCB
    Params: ClassVar[type[BoardOutlineParams]] = BoardOutlineParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        outline = geo.outline
        if not outline.closed or outline.bbox is None:
            return CheckResult.fail(self.id, "Edge.Cuts does not form a closed outline")

        p = self.params
        x0, y0, x1, y1 = outline.bbox
        width, height = round(x1 - x0, 4), round(y1 - y0, 4)
        details = {
            "width_mm": width,
            "height_mm": height,
            "area_mm2": round(outline.area, 3),
            "outlines": len(outline.polygons),
            "cutouts": len(outline.holes),
        }
        issues = []
        if p.width_mm is not None and abs(width - p.width_mm) > p.tolerance_mm:
            issues.append(f"width {width}mm, expected {p.width_mm}mm")
        if p.height_mm is not None and abs(height - p.height_mm) > p.tolerance_mm:
            issues.append(f"height {height}mm, expected {p.height_mm}mm")
        if p.max_width_mm is not None and width > p.max_width_mm:
            issues.append(f"width {width}mm > max {p.max_width_mm}mm")
        if p.max_height_mm is not None and height > p.max_height_mm:
            issues.append(f"height {height}mm > max {p.max_height_mm}mm")
        if p.min_area_mm2 is not None and outline.area < p.min_area_mm2:
            issues.append(f"area {outline.area:.1f}mm² < min {p.min_area_mm2}mm²")
        if p.max_area_mm2 is not None and outline.area > p.max_area_mm2:
            issues.append(f"area {outline.area:.1f}mm² > max {p.max_area_mm2}mm²")
        if issues:
            return CheckResult.fail(self.id, "Board outline: " + "; ".join(issues), **details)
        return CheckResult.pass_(self.id, f"Closed outline {width} x {height} mm", **details)


@dataclass
class FootprintOnBoardParams(CheckParams):
    exclude_refs: list[str] | None = None
    min_edge_clearance_mm: float = 0.0
    """How far inside the outline every courtyard must stay."""

    def validate(self) -> None:
        if self.min_edge_clearance_mm < 0:
            raise ValueError("min_edge_clearance_mm must be >= 0")


@register
class FootprintOnBoardCheck(Check[FootprintOnBoardParams]):
    """Every footprint's courtyard lies inside the board outline.

    A footprint with no courtyard is judged by its bounding box.
    """

    id = "pcb.footprint.on_board"
    name = "Footprint On Board"
    description = "Checks that every footprint lies inside the board outline"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintOnBoardParams]] = FootprintOnBoardParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        if not geo.outline.closed:
            return CheckResult.fail(self.id, "Edge.Cuts does not form a closed outline")

        excluded = set(self.params.exclude_refs or ())
        margin = self.params.min_edge_clearance_mm
        off = []
        checked = 0
        for fp in geo.footprints:
            if fp.reference in excluded:
                continue
            checked += 1
            points = [pt for poly in _shape(fp) for pt in poly]
            outside = [pt for pt in points if not geo.outline.contains(*pt)]
            if outside:
                off.append({"reference": fp.reference, "issue": "outside the outline"})
            elif margin > 0:
                gap = min(geo.outline.distance_to_edge(*pt) for pt in points)
                if gap < margin:
                    off.append(
                        {
                            "reference": fp.reference,
                            "issue": f"{gap:.3f}mm from the edge < {margin}mm",
                        }
                    )
        if off:
            return CheckResult.fail(
                self.id, f"{len(off)} footprint(s) not on the board", footprints=off
            )
        return CheckResult.pass_(
            self.id, f"All {checked} footprints are on the board", footprints_checked=checked
        )


@dataclass
class FootprintPlacementParams(CheckParams):
    reference: str
    side: str | None = None
    """``"front"`` or ``"back"``."""
    rotation: float | None = None
    """Degrees, counter-clockwise as KiCad shows them; 0 and 360 are the same."""
    rotation_tolerance: float = 0.5
    x_mm: float | None = None
    y_mm: float | None = None
    position_tolerance_mm: float = 0.1
    at_edge: bool = False
    """Require the courtyard to come within ``edge_distance_mm`` of the board edge,
    as a connector meant to be reached from outside the board does."""
    edge_distance_mm: float = 1.0

    def validate(self) -> None:
        if not self.reference:
            raise ValueError("reference is required")
        if self.side not in (None, "front", "back"):
            raise ValueError("side must be 'front' or 'back'")
        if (self.x_mm is None) != (self.y_mm is None):
            raise ValueError("x_mm and y_mm go together")


@register
class FootprintPlacementCheck(Check[FootprintPlacementParams]):
    id = "pcb.footprint.placement"
    name = "Footprint Placement"
    description = "Checks a footprint's side, rotation, position or closeness to the edge"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintPlacementParams]] = FootprintPlacementParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        fp = ctx.board.footprint(p.reference)
        if fp is None:
            return CheckResult.fail(self.id, f"Footprint '{p.reference}' not found on board")

        issues = []
        details: dict[str, object] = {
            "reference": fp.reference,
            "side": fp.side,
            "rotation": fp.rotation,
            "x_mm": fp.x,
            "y_mm": fp.y,
        }
        if p.side is not None and fp.side != p.side:
            issues.append(f"on the {fp.side}, expected {p.side}")
        if p.rotation is not None:
            diff = abs(normalize_angle(fp.rotation - p.rotation))
            diff = min(diff, 360 - diff)
            if diff > p.rotation_tolerance:
                issues.append(f"rotated {fp.rotation:g}°, expected {p.rotation:g}°")
        if p.x_mm is not None and p.y_mm is not None:
            off = math.hypot(fp.x - p.x_mm, fp.y - p.y_mm)
            if off > p.position_tolerance_mm:
                issues.append(f"at ({fp.x:g}, {fp.y:g}), expected ({p.x_mm:g}, {p.y_mm:g})")
        if p.at_edge:
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
            assert geo is not None
            placed = geo.footprint(fp.reference)
            if placed is None or not geo.outline.closed:
                issues.append("cannot measure the distance to the board edge")
            else:
                gap = _edge_gap(placed, geo.outline)
                details["edge_distance_mm"] = round(gap, 4)
                if gap > p.edge_distance_mm:
                    issues.append(f"{gap:.2f}mm from the board edge > {p.edge_distance_mm}mm")
        if issues:
            return CheckResult.fail(self.id, f"{fp.reference}: " + "; ".join(issues), **details)
        return CheckResult.pass_(self.id, f"{fp.reference} is placed as required", **details)


@dataclass
class MountingHolesParams(CheckParams):
    min_count: int = 1
    max_count: int | None = None
    diameter_mm: float | None = None
    """Required drill diameter, e.g. 3.2 for M3."""
    diameter_tolerance_mm: float = 0.05
    min_edge_distance_mm: float | None = None
    """Least distance from a hole's edge to the board edge."""
    match: str = "MountingHole:*"
    """Which footprints are mounting holes: a wildcard over lib_id."""


@register
class MountingHolesCheck(Check[MountingHolesParams]):
    id = "pcb.mounting_holes"
    name = "Mounting Holes"
    description = "Checks the number, size and edge distance of mounting holes"
    category = CheckCategory.PCB
    Params: ClassVar[type[MountingHolesParams]] = MountingHolesParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        holes = [fp for fp in ctx.board.footprints if fnmatch.fnmatchcase(fp.lib_id, p.match)]
        issues: list[str] = []
        found: list[dict[str, object]] = []
        geo = None
        if p.min_edge_distance_mm is not None:
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
        for fp in holes:
            drills = [pad.drill for pad in fp.pads if pad.drill]
            drill = max(drills) if drills else None
            entry: dict[str, object] = {"reference": fp.reference, "drill_mm": drill}
            if p.diameter_mm is not None and (
                drill is None or abs(drill - p.diameter_mm) > p.diameter_tolerance_mm
            ):
                issues.append(f"{fp.reference} drill {drill}mm, expected {p.diameter_mm}mm")
            if geo is not None and p.min_edge_distance_mm is not None:
                gap = geo.outline.distance_to_edge(fp.x, fp.y) - (drill or 0) / 2
                entry["edge_distance_mm"] = round(gap, 4)
                if gap < p.min_edge_distance_mm:
                    issues.append(
                        f"{fp.reference} is {gap:.2f}mm from the edge < {p.min_edge_distance_mm}mm"
                    )
            found.append(entry)
        if len(holes) < p.min_count:
            issues.append(f"{len(holes)} mounting hole(s) < min {p.min_count}")
        if p.max_count is not None and len(holes) > p.max_count:
            issues.append(f"{len(holes)} mounting hole(s) > max {p.max_count}")
        if issues:
            return CheckResult.fail(self.id, "; ".join(issues), holes=found)
        return CheckResult.pass_(self.id, f"{len(holes)} mounting hole(s) as required", holes=found)


def _shape(fp: FootprintGeometry) -> tuple[Polygon, ...]:
    if fp.courtyard:
        return fp.courtyard
    x0, y0, x1, y1 = fp.bbox
    return (((x0, y0), (x1, y0), (x1, y1), (x0, y1)),)


def _edge_gap(fp: FootprintGeometry, outline: OutlineGeometry) -> float:
    return min(outline.distance_to_edge(x, y) for poly in _shape(fp) for x, y in poly)
