from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class ComponentPropertyParams(CheckParams):
    reference: str
    field: str
    expected: str
    case_sensitive: bool = False

    def validate(self) -> None:
        if not self.reference:
            raise ValueError("reference is required")
        if not self.field:
            raise ValueError("field is required")


@register
class ComponentPropertyCheck(Check[ComponentPropertyParams]):
    id = "sch.component.property"
    name = "Component Property"
    description = "Checks an arbitrary property/field of a component"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[ComponentPropertyParams]] = ComponentPropertyParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        symbols = ctx.sheet_symbols()

        for sym in symbols:
            if sym.reference == params.reference:
                user_fields = getattr(sym, "user_fields", {})
                standard_fields = getattr(sym, "fields", {})
                all_fields = {**standard_fields, **user_fields}

                actual = all_fields.get(params.field)
                if actual is None:
                    return CheckResult.fail(
                        self.id,
                        f"Component {params.reference} has no field '{params.field}'",
                        reference=params.reference,
                        field=params.field,
                    )

                if params.case_sensitive:
                    match = actual == params.expected
                else:
                    match = str(actual).lower() == str(params.expected).lower()

                if match:
                    return CheckResult.pass_(
                        self.id,
                        f"Component {params.reference} field '{params.field}' matches expected value",
                        reference=params.reference,
                        field=params.field,
                        expected=params.expected,
                        actual=actual,
                    )
                else:
                    return CheckResult.fail(
                        self.id,
                        f"Component {params.reference} field '{params.field}' mismatch: "
                        f"expected '{params.expected}', got '{actual}'",
                        reference=params.reference,
                        field=params.field,
                        expected=params.expected,
                        actual=actual,
                    )

        return CheckResult.fail(
            self.id,
            f"Component with reference '{params.reference}' not found",
            reference=params.reference,
        )
