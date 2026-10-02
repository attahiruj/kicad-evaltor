from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class ComponentExistsParams(CheckParams):
    reference: str | None = None
    lib_id: str | None = None
    lib_id_pattern: str | None = None
    value: str | None = None

    def validate(self) -> None:
        if not any([self.reference, self.lib_id, self.lib_id_pattern, self.value]):
            raise ValueError(
                "At least one of reference, lib_id, lib_id_pattern, or value must be provided"
            )


@register
class ComponentExistsCheck(Check[ComponentExistsParams]):
    id = "sch.component.exists"
    name = "Component Exists"
    description = "Checks if a component matching the criteria exists in the schematic"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[ComponentExistsParams]] = ComponentExistsParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        symbols = ctx.schematic.get_symbols()

        matches = []
        for sym in symbols:
            if params.reference and sym.reference != params.reference:
                continue
            if params.lib_id and sym.lib_id != params.lib_id:
                continue
            if params.lib_id_pattern:
                import fnmatch

                if not fnmatch.fnmatch(sym.lib_id, params.lib_id_pattern):
                    continue
            if params.value and sym.value.lower() != params.value.lower():
                continue
            matches.append(sym)

        if matches:
            refs = [s.reference for s in matches]
            return CheckResult.pass_(
                self.id,
                f"Found {len(matches)} matching component(s): {', '.join(refs)}",
                count=len(matches),
                references=refs,
            )

        criteria = []
        if params.reference:
            criteria.append(f"reference={params.reference}")
        if params.lib_id:
            criteria.append(f"lib_id={params.lib_id}")
        if params.lib_id_pattern:
            criteria.append(f"lib_id_pattern={params.lib_id_pattern}")
        if params.value:
            criteria.append(f"value={params.value}")

        return CheckResult.fail(
            self.id,
            f"No component found matching: {', '.join(criteria)}",
            criteria=criteria,
        )
