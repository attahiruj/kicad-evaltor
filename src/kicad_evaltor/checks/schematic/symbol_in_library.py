from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class SymbolInLibraryParams(CheckParams):
    lib_id: str

    def validate(self) -> None:
        if not self.lib_id:
            raise ValueError("lib_id is required")


@register
class SymbolInLibraryCheck(Check[SymbolInLibraryParams]):
    id = "sch.symbol.in_library"
    name = "Symbol In Library"
    description = "Verifies a symbol library exists and contains the specified symbol"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SymbolInLibraryParams]] = SymbolInLibraryParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        result = ctx.run_kicad_cli(["sym", "list", "--format", "json", params.lib_id])

        if not result.success:
            return CheckResult.error(self.id, f"Failed to query symbol library: {result.stderr}")

        import json

        try:
            data = json.loads(result.stdout)
            symbols = data.get("symbols", [])
            found = any(s.get("name") == params.lib_id.split(":")[-1] for s in symbols)

            if found:
                return CheckResult.pass_(
                    self.id,
                    f"Symbol '{params.lib_id}' found in library",
                    lib_id=params.lib_id,
                )
            else:
                return CheckResult.fail(
                    self.id,
                    f"Symbol '{params.lib_id}' not found in library",
                    lib_id=params.lib_id,
                    available_symbols=[s.get("name") for s in symbols],
                )
        except json.JSONDecodeError:
            return CheckResult.error(self.id, "Failed to parse symbol library output")
