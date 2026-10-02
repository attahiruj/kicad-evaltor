from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.units import to_mm


@dataclass
class FootprintOverlapParams(CheckParams):
    min_clearance_mm: float = 0.0
    exclude_refs: list[str] | None = None

    def validate(self) -> None:
        if self.min_clearance_mm < 0:
            raise ValueError("min_clearance_mm must be >= 0")


@register
class FootprintOverlapCheck(Check[FootprintOverlapParams]):
    id = "pcb.footprint.overlap"
    name = "Footprint Overlap"
    description = "Checks for overlapping or too-close footprints on the board"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintOverlapParams]] = FootprintOverlapParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        footprints = ctx.board.get_footprints()

        if params.exclude_refs:
            footprints = [f for f in footprints if f.reference not in params.exclude_refs]

        violations = []
        for i, fp1 in enumerate(footprints):
            bb1 = ctx.board.get_item_bounding_box(fp1)
            for fp2 in footprints[i + 1 :]:
                bb2 = ctx.board.get_item_bounding_box(fp2)

                dx = max(0, max(bb1.min_x, bb2.min_x) - min(bb1.max_x, bb2.max_x))
                dy = max(0, max(bb1.min_y, bb2.min_y) - min(bb1.max_y, bb2.max_y))

                if dx == 0 and dy == 0:
                    clearance_mm = 0.0
                else:
                    clearance_mm = to_mm(min(dx, dy))

                if clearance_mm < params.min_clearance_mm:
                    violations.append(
                        {
                            "footprint_a": fp1.reference,
                            "footprint_b": fp2.reference,
                            "clearance_mm": clearance_mm,
                            "required_mm": params.min_clearance_mm,
                        }
                    )

        if not violations:
            return CheckResult.pass_(
                self.id,
                f"No footprint overlaps found (checked {len(footprints)} footprints)",
                footprints_checked=len(footprints),
            )

        return CheckResult.fail(
            self.id,
            f"Found {len(violations)} footprint overlap(s) below minimum clearance {params.min_clearance_mm}mm",
            violations=violations,
            footprints_checked=len(footprints),
        )
