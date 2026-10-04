from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class FootprintAssignedParams(CheckParams):
    reference: str | None = None
    allow_none: bool = False

    def validate(self) -> None:
        pass


@register
class FootprintAssignedCheck(Check[FootprintAssignedParams]):
    id = "sch.footprint.assigned"
    name = "Footprint Assigned"
    description = "Verifies that components have footprints assigned"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[FootprintAssignedParams]] = FootprintAssignedParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        symbols = ctx.sheet_symbols()

        if params.reference:
            symbols = [s for s in symbols if s.reference == params.reference]
            if not symbols:
                return CheckResult.fail(
                    self.id,
                    f"Component with reference '{params.reference}' not found",
                    reference=params.reference,
                )

        missing = []
        for sym in symbols:
            footprint = getattr(sym, "footprint", None)
            footprint_text = getattr(footprint, "text", None) if footprint else None
            footprint_value = footprint_text.value if footprint_text else None

            if not footprint_value or footprint_value.strip() == "":
                if not params.allow_none:
                    missing.append(sym.reference)

        if not missing:
            return CheckResult.pass_(
                self.id,
                f"All {len(symbols)} component(s) have footprints assigned",
                checked=len(symbols),
            )

        return CheckResult.fail(
            self.id,
            f"{len(missing)} component(s) missing footprint assignment: {', '.join(missing)}",
            missing=missing,
            checked=len(symbols),
        )
