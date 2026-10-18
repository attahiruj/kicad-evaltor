from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import board_drc, drc_error, footprints_named, geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pcbnew_bridge import polygon_distance


@dataclass
class FootprintOverlapParams(CheckParams):
    min_clearance_mm: float = 0.0
    """Extra gap required between courtyards; 0 means they may touch but not overlap."""
    exclude_refs: list[str] | None = None
    refill_zones: bool = True
    timeout: float | None = None

    def validate(self) -> None:
        if self.min_clearance_mm < 0:
            raise ValueError("min_clearance_mm must be >= 0")


@register
class FootprintOverlapCheck(Check[FootprintOverlapParams]):
    """No two footprints' courtyards overlap.

    Overlaps come from KiCad's DRC (``courtyards_overlap``), which compares the
    real courtyard shapes. A ``min_clearance_mm`` above zero also measures the
    gap between every pair of courtyards on the same side, using the outlines
    pcbnew reports.
    """

    id = "pcb.footprint.overlap"
    name = "Footprint Overlap"
    description = "Checks for overlapping or too-close footprint courtyards"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintOverlapParams]] = FootprintOverlapParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        excluded = set(params.exclude_refs or ())
        run = board_drc(ctx, refill_zones=params.refill_zones, timeout=params.timeout)
        if run.error is not None:
            return drc_error(self.id, run)

        violations: list[dict[str, object]] = []
        seen: set[frozenset[str]] = set()
        for entry in run.of_types("courtyards_overlap"):
            refs = footprints_named(entry)
            if len(refs) < 2 or excluded & set(refs):
                continue
            seen.add(frozenset(refs[:2]))
            violations.append(
                {
                    "footprint_a": refs[0],
                    "footprint_b": refs[1],
                    "clearance_mm": 0.0,
                    "required_mm": params.min_clearance_mm,
                }
            )

        checked = len([f for f in ctx.board.footprints if f.reference not in excluded])
        if params.min_clearance_mm > 0:
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
            assert geo is not None
            footprints = [f for f in geo.footprints if f.reference not in excluded and f.courtyard]
            for i, first in enumerate(footprints):
                for second in footprints[i + 1 :]:
                    if first.side != second.side:
                        continue
                    if frozenset((first.reference, second.reference)) in seen:
                        continue
                    gap = min(
                        polygon_distance(a, b) for a in first.courtyard for b in second.courtyard
                    )
                    if gap < params.min_clearance_mm:
                        violations.append(
                            {
                                "footprint_a": first.reference,
                                "footprint_b": second.reference,
                                "clearance_mm": round(gap, 4),
                                "required_mm": params.min_clearance_mm,
                            }
                        )

        if not violations:
            return CheckResult.pass_(
                self.id,
                f"No footprint overlaps found (checked {checked} footprints)",
                footprints_checked=checked,
            )
        return CheckResult.fail(
            self.id,
            f"Found {len(violations)} footprint overlap(s) below minimum clearance "
            f"{params.min_clearance_mm}mm",
            violations=violations,
            footprints_checked=checked,
        )
