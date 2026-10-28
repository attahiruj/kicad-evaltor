"""Instance-aware identity for user-authored tests; queries do not grade designs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_evaltor.selection import Selection, select

if TYPE_CHECKING:
    from kicad_evaltor.core.context import DesignContext
    from kicad_evaltor.schematic_file import FileSymbol


@dataclass(frozen=True)
class SymbolInstance:
    project: str
    sheet_path: str
    uuid: str
    unit: int
    symbol: FileSymbol

    @property
    def identity(self) -> tuple[str, str, str, int]:
        return self.project, self.sheet_path, self.uuid, self.unit


class DesignIndex:
    """Schematic identities; netlist and PCB facts are loaded only on request."""

    def __init__(self, ctx: DesignContext) -> None:
        self.context = ctx
        tree = ctx.sheet_tree()
        self.problems = tuple(tree.problems()) if tree else ()
        self.symbols = (
            tuple(
                SymbolInstance(ctx.schematic.project_name or "", sheet.path, s.uuid, s.unit, s)
                for sheet, s in tree.iter_symbols()
            )
            if tree
            else ()
        )

    def select(
        self, reference: str = "*", *, sheet_path: str | None = None, predicate=None
    ) -> Selection[SymbolInstance]:
        source = tuple(
            s
            for s in self.symbols
            if (sheet_path is None or s.sheet_path == sheet_path)
            and (predicate is None or predicate(s))
        )
        return select(source, reference, key=lambda s: s.symbol.reference)

    def joined(self, instance: SymbolInstance) -> dict:
        """Join by active-project reference, retaining all units/placements above."""
        ref = instance.symbol.reference
        return {
            "identity": instance.identity,
            "reference": ref,
            "component": self.context.schematic.get_netlist().components.get(ref),
            "footprint": self.context.board.footprint(ref) if self.context.has_board() else None,
        }
