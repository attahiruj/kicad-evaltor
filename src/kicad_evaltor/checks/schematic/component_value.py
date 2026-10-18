from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.values import parse_value, values_match


@dataclass
class ComponentValueParams(CheckParams):
    reference: str
    # Nullable because dataclasses do not enforce types at runtime, and
    # `validate` exists to reject a missing expectation.
    expected: str | None
    case_sensitive: bool = False
    numeric: bool = False
    """Compare as numbers, so ``4k7``, ``4.7k`` and ``4700`` all match."""
    tolerance: float = 0.0
    """With ``numeric``, how far apart the values may be, as a fraction."""

    def validate(self) -> None:
        if not self.reference:
            raise ValueError("reference is required")
        if self.expected is None:
            raise ValueError("expected value is required")
        if not 0 <= self.tolerance < 1:
            raise ValueError("tolerance must be a fraction between 0 and 1")
        if self.numeric and parse_value(self.expected) is None:
            raise ValueError(f"expected value {self.expected!r} is not a number")


@register
class ComponentValueCheck(Check[ComponentValueParams]):
    id = "sch.component.value"
    name = "Component Value"
    description = "Verifies a specific component has the expected value"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[ComponentValueParams]] = ComponentValueParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        symbols = ctx.sheet_symbols()
        # `validate` already rejected a missing expectation, but it cannot
        # narrow the type for a reader, so do it once here.
        expected = params.expected
        if expected is None:
            return CheckResult.skip(self.id, "No expected value configured")

        for sym in symbols:
            if sym.reference == params.reference:
                actual = sym.value
                if params.numeric:
                    want, have = parse_value(expected), parse_value(actual)
                    if have is None:
                        return CheckResult.fail(
                            self.id,
                            f"Component {params.reference} value '{actual}' is not a number",
                            reference=params.reference,
                            expected=params.expected,
                            actual=actual,
                        )
                    assert want is not None  # validate() rejects a non-number
                    match = values_match(have, want, params.tolerance)
                elif params.case_sensitive:
                    match = actual == expected
                else:
                    # Text folding must not erase meaningful SI prefix case.
                    have, want = parse_value(actual), parse_value(expected)
                    match = actual.lower() == expected.lower() and (
                        have is None or want is None or have == want
                    )

                if match:
                    return CheckResult.pass_(
                        self.id,
                        f"Component {params.reference} has expected value '{params.expected}'",
                        reference=params.reference,
                        expected=params.expected,
                        actual=actual,
                    )
                else:
                    return CheckResult.fail(
                        self.id,
                        f"Component {params.reference} value mismatch: expected '{params.expected}', got '{actual}'",
                        reference=params.reference,
                        expected=params.expected,
                        actual=actual,
                    )

        return CheckResult.fail(
            self.id,
            f"Component with reference '{params.reference}' not found",
            reference=params.reference,
        )
