from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class FootprintExistsParams(CheckParams):
    reference: str | None = None
    lib_id: str | None = None

    def validate(self) -> None:
        if not self.reference and not self.lib_id:
            raise ValueError("At least one of reference or lib_id must be provided")


@register
class FootprintExistsCheck(Check[FootprintExistsParams]):
    id = "pcb.footprint.exists"
    name = "Footprint Exists"
    description = "Checks if a footprint with the given reference or lib_id exists on the board"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintExistsParams]] = FootprintExistsParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        matches = [
            fp
            for fp in ctx.board.get_footprints()
            if (not params.reference or fp.reference == params.reference)
            and (not params.lib_id or fp.lib_id == params.lib_id)
        ]

        if matches:
            refs = [f.reference for f in matches]
            return CheckResult.pass_(
                self.id,
                f"Found {len(matches)} matching footprint(s): {', '.join(refs)}",
                count=len(matches),
                references=refs,
            )

        criteria = []
        if params.reference:
            criteria.append(f"reference={params.reference}")
        if params.lib_id:
            criteria.append(f"lib_id={params.lib_id}")

        return CheckResult.fail(
            self.id,
            f"No footprint found matching: {', '.join(criteria)}",
            criteria=criteria,
        )
