from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext

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


@register
class TextOffBoardCheck(Check[TextOffBoardParams]):
    """No silkscreen text hangs off the edge of the board.

    Each text's box is the one KiCad draws it in (from pcbnew); a corner outside
    the outline means part of the text would be cut off or printed on nothing.
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
        off = []
        texts = [
            t
            for t in geo.all_texts()
            if t.layer in _SILK and (t.visible or self.params.include_hidden) and t.text
        ]
        for text in texts:
            x0, y0, x1, y1 = text.bbox
            corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            if not all(geo.outline.contains(x, y) for x, y in corners):
                off.append(
                    {
                        "owner": text.owner or "board",
                        "kind": text.kind,
                        "text": text.text,
                        "layer": text.layer,
                        "bbox": list(text.bbox),
                    }
                )
        if off:
            return CheckResult.fail(
                self.id, f"{len(off)} silkscreen text(s) off the board", texts=off
            )
        return CheckResult.pass_(
            self.id, f"All {len(texts)} silkscreen texts are on the board", checked=len(texts)
        )
