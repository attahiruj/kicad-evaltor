from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class ConsistencyParams(CheckParams):
    require_both: bool = True

    def validate(self) -> None:
        pass


@register
class ConsistencyCheck(Check[ConsistencyParams]):
    id = "project.schematic_pcb_consistency"
    name = "Schematic-PCB Consistency"
    description = (
        "Cross-checks that components in the schematic have corresponding footprints on the PCB"
    )
    category = CheckCategory.PROJECT
    Params: ClassVar[type[ConsistencyParams]] = ConsistencyParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic() or not ctx.has_board():
            return CheckResult.skip(self.id, "Both schematic and board required")

        params = self.params
        symbols = ctx.sheet_symbols()
        footprints = ctx.board.get_footprints()

        sch_refs = {s.reference for s in symbols}
        pcb_refs = {f.reference for f in footprints}

        in_sch_not_pcb = sch_refs - pcb_refs
        in_pcb_not_sch = pcb_refs - sch_refs

        issues = []
        if params.require_both:
            if in_sch_not_pcb:
                issues.append(f"In schematic but not PCB: {', '.join(sorted(in_sch_not_pcb))}")
            if in_pcb_not_sch:
                issues.append(f"In PCB but not schematic: {', '.join(sorted(in_pcb_not_sch))}")

        if not issues:
            return CheckResult.pass_(
                self.id,
                f"All {len(sch_refs)} schematic component(s) match PCB footprint(s)",
                schematic_count=len(sch_refs),
                pcb_count=len(pcb_refs),
            )

        return CheckResult.fail(
            self.id,
            "Schematic-PCB consistency check failed: " + "; ".join(issues),
            in_schematic_not_pcb=sorted(in_sch_not_pcb),
            in_pcb_not_schematic=sorted(in_pcb_not_sch),
            schematic_count=len(sch_refs),
            pcb_count=len(pcb_refs),
        )
