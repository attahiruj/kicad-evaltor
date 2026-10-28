from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pcbnew_bridge import OutlineGeometry

_SILK = ("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen")


@dataclass
class ReferenceVisibleParams(CheckParams):
    exclude: list[str] | None = None
    """Wildcards over references to leave out, e.g. ``["FID*", "H*"]``."""
    min_height_mm: float | None = None
    min_thickness_mm: float | None = None
    """Each None falls back to the board's ``min_text_height`` /
    ``min_text_thickness`` design rule."""


@register
class ReferenceVisibleCheck(Check[ReferenceVisibleParams]):
    """Every footprint shows its reference designator on silkscreen, readably.

    The reference must be visible, on a silkscreen layer, and at least the
    minimum text height and stroke. Read from the board file.
    """

    id = "pcb.silk.reference_visible"
    name = "Reference Designators Visible"
    description = "Checks that every footprint's reference is printed on silkscreen"
    category = CheckCategory.PCB
    Params: ClassVar[type[ReferenceVisibleParams]] = ReferenceVisibleParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        board = ctx.board
        rules = board.net_classes.rules
        min_height = (
            p.min_height_mm if p.min_height_mm is not None else rules.get("min_text_height")
        )
        min_stroke = (
            p.min_thickness_mm
            if p.min_thickness_mm is not None
            else rules.get("min_text_thickness")
        )
        patterns = p.exclude or []
        issues = []
        checked = 0
        for fp in board.footprints:
            if any(fnmatch.fnmatchcase(fp.reference, pat) for pat in patterns):
                continue
            checked += 1
            text = fp.reference_text
            if text is None or text.hidden:
                issues.append({"reference": fp.reference, "issue": "hidden"})
            elif text.layer not in _SILK:
                issues.append({"reference": fp.reference, "issue": f"on {text.layer}, not silk"})
            elif min_height is not None and text.height < min_height - 1e-6:
                issues.append(
                    {"reference": fp.reference, "issue": f"height {text.height}mm < {min_height}mm"}
                )
            elif min_stroke is not None and text.thickness < min_stroke - 1e-6:
                issues.append(
                    {
                        "reference": fp.reference,
                        "issue": f"stroke {text.thickness}mm < {min_stroke}mm",
                    }
                )
        if issues:
            return CheckResult.fail(
                self.id,
                f"{len(issues)} of {checked} reference(s) not readable on silkscreen",
                issues=issues,
            )
        return CheckResult.pass_(
            self.id, f"All {checked} references are on silkscreen", checked=checked
        )


@dataclass
class TextOffBoardParams(CheckParams):
    include_hidden: bool = False
    method: Literal["ink", "bbox"] = "ink"
    """What must stay on the board. ``"ink"`` (the default) is the printed
    glyphs, stroke width included, as KiCad plots them. ``"bbox"`` is KiCad's
    text frame box, which reaches past the letters for line spacing and
    descenders, so text sitting on the board can fail; it was the only method
    before 0.6.1."""
    tolerance_mm: float = 0.0
    """How far a text may overhang the outline and still pass."""

    def validate(self) -> None:
        if self.method not in ("ink", "bbox"):
            raise ValueError(f"method must be 'ink' or 'bbox', not {self.method!r}")
        if self.tolerance_mm < 0:
            raise ValueError("tolerance_mm must be >= 0")


@register
class TextOffBoardCheck(Check[TextOffBoardParams]):
    """No silkscreen text hangs off the edge of the board.

    Each text's overhang is how far its ink (or, with ``method="bbox"``, its
    frame box) reaches outside the board outline, in mm: 0 when it lies wholly
    on the board. A text fails when its overhang exceeds ``tolerance_mm``. The
    shapes come from pcbnew. A recording without ink (made before 0.6.1) falls
    back to the frame box for that text; ``bbox_fallback`` counts those texts.
    """

    id = "pcb.text.off_board"
    name = "Text Off Board"
    description = "Checks that silkscreen text lies inside the board outline"
    category = CheckCategory.PCB
    Params: ClassVar[type[TextOffBoardParams]] = TextOffBoardParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        if not geo.outline.closed:
            return CheckResult.fail(self.id, "Edge.Cuts does not form a closed outline")
        p = self.params
        off: list[dict[str, Any]] = []
        overhanging: list[dict[str, Any]] = []
        fallback = 0
        texts = [
            t
            for t in geo.all_texts()
            if t.layer in _SILK and (t.visible or p.include_hidden) and t.text
        ]
        for text in texts:
            if p.method == "ink" and text.ink:
                measured = "ink"
                overhang = max(
                    text_overhang(piece.points, geo.outline, piece.radius) for piece in text.ink
                )
            else:
                measured = "bbox"
                fallback += p.method == "ink"
                x0, y0, x1, y1 = text.bbox
                corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                overhang = text_overhang(corners, geo.outline)
            if overhang <= 0:
                continue
            entry = {
                "owner": text.owner or "board",
                "kind": text.kind,
                "text": text.text,
                "layer": text.layer,
                "bbox": list(text.bbox),
                "ink_bbox": [round(v, 6) for v in text.ink_bbox] if text.ink_bbox else None,
                "measured": measured,
                "overhang_mm": round(overhang, 4),
            }
            (off if overhang > p.tolerance_mm + 1e-9 else overhanging).append(entry)
        details = {
            "method": p.method,
            "tolerance_mm": p.tolerance_mm,
            "checked": len(texts),
            "within_tolerance": overhanging,
            "bbox_fallback": fallback,
        }
        if off:
            worst = max(entry["overhang_mm"] for entry in off)
            return CheckResult.fail(
                self.id,
                f"{len(off)} silkscreen text(s) off the board, up to {worst:g}mm over the edge",
                texts=off,
                **details,
            )
        return CheckResult.pass_(
            self.id, f"All {len(texts)} silkscreen texts are on the board", **details
        )


def text_overhang(
    points: Sequence[tuple[float, float]], outline: OutlineGeometry, radius: float = 0.0
) -> float:
    """How far ink reaches outside the board, in mm; 0 when it lies wholly on it.

    The ink is everything within ``radius`` of ``points``: the points along a
    round-ended stroke with half its width, or a filled outline's corners with
    radius 0. A point off the board reaches its
    distance to the nearest edge plus the radius; a point on the board, the
    radius less its distance to the edge. A cut-out counts as off the board.
    Measured at the points, so it is exact against straight board edges, and
    against a concave corner it can miss part of a stroke's middle.
    """
    reach = 0.0
    for x, y in points:
        distance = outline.distance_to_edge(x, y)
        reach = max(reach, radius + (distance if not outline.contains(x, y) else -distance))
    return reach
