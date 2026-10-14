from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.schematic_file import FileSchematic, FileSymbol
from kicad_evaltor.sexpr import SExpr
from kicad_evaltor.symbol_library import (
    SymbolLibraries,
    SymbolNotFound,
    symbol_diff,
    symbol_signature,
)


@dataclass
class SymbolMatchesLibraryParams(CheckParams):
    """Which placed symbols to compare, and against what.

    With neither ``references`` nor ``lib_id`` every placed symbol is
    compared. ``lib_id`` is a glob over the placed symbols' ids, such as
    ``"Device:*"``. ``expected_lib_id`` compares every selected symbol against
    that one library symbol instead of the one each claims to be, which is how
    a symbol copied into a project library under a new name is checked.

    The libraries are KiCad's stock folders unless ``library_dirs`` names
    others. A project's own ``sym-lib-table`` is ignored unless
    ``use_project_table`` is set, because a project library that reuses a stock
    nickname would otherwise be compared against itself.
    """

    references: list[str] = field(default_factory=list)
    lib_id: str | None = None
    expected_lib_id: str | None = None
    library_dirs: list[str] | None = None
    use_project_table: bool = False
    compare_graphics: bool = True
    include_power: bool = False

    def validate(self) -> None:
        if self.expected_lib_id is not None and ":" not in self.expected_lib_id:
            raise ValueError("expected_lib_id must be 'Library:Symbol'")


@register
class SymbolMatchesLibraryCheck(Check[SymbolMatchesLibraryParams]):
    id = "sch.symbol.matches_library"
    name = "Symbol Matches Library"
    description = "Checks placed symbols are unchanged copies of their library symbols"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SymbolMatchesLibraryParams]] = SymbolMatchesLibraryParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        libs = SymbolLibraries(
            params.library_dirs,
            project_dir=ctx.project_dir if params.use_project_table else None,
            cli_path=ctx.kicad_cli_path,
        )
        if not libs.dirs and params.library_dirs is None:
            return CheckResult.error(
                self.id, "No KiCad symbol libraries found; set KICAD10_SYMBOL_DIR or library_dirs"
            )

        selected = list(self._selected(ctx))
        if params.references:
            missing = sorted(set(params.references) - {s.reference for s, _ in selected})
            if missing:
                return CheckResult.fail(
                    self.id, f"Symbol(s) not found: {', '.join(missing)}", missing=missing
                )
        if not selected:
            return CheckResult.skip(self.id, "No symbols matched the selection")

        mismatches: dict[str, list[str]] = {}
        compared: dict[str, str] = {}
        for symbol, schematic in selected:
            target = params.expected_lib_id or symbol.lib_id
            compared[symbol.reference] = target
            problems = self._compare(libs, target, symbol, schematic)
            if problems:
                mismatches[symbol.reference] = problems

        details: dict[str, Any] = {"compared": compared, "mismatches": mismatches}
        if mismatches:
            refs = ", ".join(sorted(mismatches))
            return CheckResult.fail(
                self.id, f"{len(mismatches)} symbol(s) differ from the library: {refs}", **details
            )
        return CheckResult.pass_(self.id, f"{len(compared)} symbol(s) match the library", **details)

    def _selected(self, ctx: DesignContext) -> list[tuple[FileSymbol, FileSchematic]]:
        params = self.params
        tree = ctx.sheet_tree()
        pairs: list[tuple[FileSymbol, FileSchematic]] = []
        if tree is not None:
            for sheet, symbol in tree.iter_symbols():
                schematic = tree.schematic_for(sheet)
                if schematic is not None:
                    pairs.append((symbol, schematic))
        else:
            pairs = [(s, ctx.schematic) for s in ctx.schematic.get_symbols()]

        chosen = []
        seen: set[str] = set()
        for symbol, schematic in pairs:
            # A multi-unit part is placed once per unit; compare it once.
            if symbol.reference in seen:
                continue
            if not params.include_power and symbol.reference.startswith("#"):
                continue
            if params.references and symbol.reference not in params.references:
                continue
            if params.lib_id and not fnmatch.fnmatchcase(symbol.lib_id, params.lib_id):
                continue
            seen.add(symbol.reference)
            chosen.append((symbol, schematic))
        return chosen

    def _compare(
        self, libs: SymbolLibraries, target: str, symbol: FileSymbol, schematic: FileSchematic
    ) -> list[str]:
        embedded: list[SExpr] | None = schematic.lib_symbols().get(symbol.embedded_name)
        if embedded is None:
            return [f"no embedded copy {symbol.embedded_name!r} in {schematic.path.name}"]
        try:
            expected = libs.resolve(target)
        except (SymbolNotFound, ValueError) as exc:
            return [f"{target}: {exc}"]
        include = self.params.compare_graphics
        return symbol_diff(
            symbol_signature(expected, include_graphics=include),
            symbol_signature(embedded, include_graphics=include),
        )
