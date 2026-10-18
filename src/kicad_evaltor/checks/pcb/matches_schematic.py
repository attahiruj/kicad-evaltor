from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import (
    Check,
    CheckCategory,
    CheckParams,
    CheckResult,
    empty_selection,
)
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.schematic_file import NetlistUnavailable


@dataclass
class FootprintMatchesSchematicParams(CheckParams):
    on_empty: str = "skip"
    """What to report when nothing is selected: ``"skip"`` (default), ``"fail"``
    or ``"pass"``. A selection that matches nothing never passes by accident."""
    references: list[str] | None = None
    check_pins: bool = True
    """Also require every symbol pin number to have a pad on the footprint. Pin
    numbers come from kicad-cli's netlist export."""

    def validate(self) -> None:
        if self.on_empty not in ("pass", "fail", "skip"):
            raise ValueError("on_empty must be pass, fail or skip")


@register
class FootprintMatchesSchematicCheck(Check[FootprintMatchesSchematicParams]):
    """Each footprint is the one its symbol names, with a pad for every pin.

    ``pcb.schematic_parity`` (KiCad's DRC) reports a footprint whose library
    name differs from its symbol's Footprint field. This check asks the same
    question without running DRC and adds one DRC does not: that the footprint
    has a pad for each of the symbol's pin numbers, so no pin is left unplaced.
    """

    id = "pcb.footprint.matches_schematic"
    name = "Footprint Matches Schematic"
    description = "Checks that each footprint is the one its symbol names, with every pin"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintMatchesSchematicParams]] = FootprintMatchesSchematicParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board() or not ctx.has_schematic():
            return CheckResult.skip(self.id, "Both schematic and board required")
        p = self.params
        wanted = set(p.references) if p.references else None
        board = ctx.board
        symbols = {
            s.reference: s
            for s in ctx.sheet_symbols()
            if s.on_board
            and not s.reference.startswith("#")
            and (wanted is None or s.reference in wanted)
        }
        if wanted is not None:
            unknown = sorted(wanted - set(symbols))
            if unknown:
                # A part the caller named that is not in the design is a finding,
                # not an empty selection.
                return CheckResult.fail(
                    self.id,
                    f"No placed symbol for {', '.join(unknown)}",
                    missing_references=unknown,
                )
        if not symbols:
            return empty_selection(self.id, p.on_empty, checked=0)
        netlist = None
        if p.check_pins:
            try:
                netlist = ctx.schematic.get_netlist()
            except NetlistUnavailable as exc:
                return CheckResult.error(self.id, f"Cannot read symbol pins: {exc}")

        mismatches = []
        for ref, symbol in sorted(symbols.items()):
            fp = board.footprint(ref)
            if fp is None:
                mismatches.append({"reference": ref, "issue": "no footprint on the board"})
                continue
            expected = symbol.footprint.text.value
            if expected and expected != fp.lib_id:
                mismatches.append(
                    {"reference": ref, "issue": f"footprint {fp.lib_id}, symbol names {expected}"}
                )
            if netlist is not None:
                pins = {node.pin for node in netlist.pins_of(ref)}
                pads = {pad.number for pad in fp.pads if pad.number}
                missing = sorted(pins - pads)
                if missing:
                    mismatches.append(
                        {"reference": ref, "issue": f"no pad for pin(s) {', '.join(missing)}"}
                    )
        if mismatches:
            return CheckResult.fail(
                self.id,
                f"{len(mismatches)} footprint(s) do not match the schematic",
                mismatches=mismatches,
            )
        return CheckResult.pass_(
            self.id, f"All {len(symbols)} footprints match their symbols", checked=len(symbols)
        )
