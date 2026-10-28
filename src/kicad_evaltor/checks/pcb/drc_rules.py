"""Board checks that are one DRC rule each.

KiCad already tests these exactly, so each check reads its own findings out of
the shared DRC report (see ``_common.board_drc``) rather than re-deriving them:
unrouted connections, schematic parity, footprints against their libraries, and
silkscreen over copper or over other silkscreen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import (
    board_drc,
    drc_error,
    drc_findings,
    footprints_named,
    items_of,
    parity_schematic,
)
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.findings import finding_references
from kicad_evaltor.utils.kicad_cli import violation_details


@dataclass
class DrcRuleParams(CheckParams):
    references: list[str] | None = None
    """Only count findings that name one of these footprints."""
    refill_zones: bool = True
    timeout: float | None = None

    def validate(self) -> None:
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")


class _DrcRuleCheck(Check[DrcRuleParams]):
    """Passes when the DRC report has no finding of ``types``."""

    category = CheckCategory.PCB
    Params: ClassVar[type[DrcRuleParams]] = DrcRuleParams
    types: ClassVar[tuple[str, ...]] = ()
    source: ClassVar[str | None] = None
    what: ClassVar[str] = "finding"
    needs_schematic: ClassVar[bool] = False

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        if self.needs_schematic and parity_schematic(ctx) is None:
            return CheckResult.skip(
                self.id,
                f"Schematic parity needs {ctx.board_path.with_suffix('.kicad_sch').name} "
                "beside the board; kicad-cli finds the schematic by the board's name",
            )
        params = self.params
        run = board_drc(ctx, refill_zones=params.refill_zones, timeout=params.timeout)
        if run.error is not None:
            return drc_error(self.id, run)
        found = run.of_types(*self.types, source=self.source)
        if params.references:
            wanted = set(params.references)
            found = [e for e in found if wanted & set(footprints_named(e))]
        if not found:
            return CheckResult.pass_(self.id, f"No {self.what}s", count=0)
        return CheckResult.fail(
            self.id,
            f"{len(found)} {self.what}(s)",
            count=len(found),
            findings=self.describe(found, ctx),
        )

    def describe(self, entries: list[dict[str, Any]], ctx: DesignContext) -> list[dict[str, Any]]:
        return drc_findings(entries)


_NET_IN_ITEM = re.compile(r"\[([^\]]*)\]")


@register
class RoutingCompleteCheck(_DrcRuleCheck):
    """Every connection the ratsnest asks for is routed in copper.

    Read from DRC's ``unconnected_items``, which is KiCad's own connectivity,
    zone fills included. Findings are grouped by net.
    """

    id = "pcb.routing.complete"
    name = "Routing Complete"
    description = "Checks that no connection is left unrouted"
    source = "unconnected_items"
    what = "unrouted connection"

    def describe(self, entries: list[dict[str, Any]], ctx: DesignContext) -> list[dict[str, Any]]:
        by_net: dict[str, list[list[str]]] = {}
        for entry in entries:
            items = items_of(entry)
            nets = {m.group(1) for d in items for m in [_NET_IN_ITEM.search(d)] if m}
            by_net.setdefault(", ".join(sorted(nets)) or "?", []).append(items)
        return [{"net": net, "connections": conns} for net, conns in sorted(by_net.items())]


@register
class SchematicParityCheck(_DrcRuleCheck):
    """The board matches its schematic: footprints, values, fields and nets.

    KiCad finds the schematic beside the board by project name. Findings keep
    their DRC type: ``missing_footprint``, ``extra_footprint``,
    ``footprint_symbol_mismatch``, ``net_conflict`` and so on.
    """

    id = "pcb.schematic_parity"
    name = "Schematic Parity"
    description = "Checks that the board matches its schematic"
    source = "schematic_parity"
    what = "schematic parity issue"
    needs_schematic = True

    def describe(self, entries: list[dict[str, Any]], ctx: DesignContext) -> list[dict[str, Any]]:
        footprints = _board_footprints(ctx)
        return [parity_finding(entry, footprints) for entry in entries]


PARITY_TAGS = ("board_only_mechanical", "nickname_only", "field_only")
"""The tags :func:`parity_finding` may put on a schematic parity finding."""

MOUNTING_DRILL_MM = 2.0
"""Smallest drill, in mm, of the one plated pad that still makes a footprint a
mounting hole rather than a one-pin part such as a test point."""

_FOOTPRINT_MISMATCH = re.compile(
    r"^(?P<board>.+?) doesn't match footprint given by symbol \((?P<symbol>.*)\)$"
)
_FIELD_MISMATCH = re.compile(r"^Field '(?P<field>[^']*)'")


def parity_finding(entry: dict[str, Any], footprints: dict[str, Any] | None) -> dict[str, Any]:
    """One schematic parity finding with the facts a caller filters on.

    Adds ``reference`` (the footprint, from the item or the description) and
    ``tags``, any of:

    - ``board_only_mechanical``: an ``extra_footprint`` whose board footprint
      KiCad marks board-only, or that is excluded from the BOM and has no
      numbered copper pad (logos, fiducials, unplated holes) or one pad number
      with a drill of at least :data:`MOUNTING_DRILL_MM` (a plated mounting
      hole, its via ring included). The footprint's attributes and pad numbers
      are reported too (``footprint_attributes``, ``pad_numbers``) for callers
      that draw the line elsewhere.
    - ``nickname_only``: a ``footprint_symbol_mismatch`` where board and symbol
      name the same footprint and differ only in the library nickname
      (``R_0603_1608Metric`` against ``Resistor_SMD:R_0603_1608Metric``). Both
      names are reported (``board_footprint``, ``symbol_footprint``).
    - ``field_only``: a ``footprint_symbol_field_mismatch`` (a symbol field
      such as ``LCSC`` the footprint lacks or holds differently); ``field``
      names it.
    """
    finding = violation_details(entry)
    refs = [ref for ref in finding_references(entry) if ref]
    reference = refs[0] if refs else None
    tags: list[str] = []
    kind = entry.get("type")
    description = str(entry.get("description") or "")
    if kind == "extra_footprint" and footprints is not None and reference in footprints:
        footprint = footprints[reference]
        finding["footprint_attributes"] = {
            attr: bool(getattr(footprint, attr, False))
            for attr in ("board_only", "exclude_from_bom", "exclude_from_pos_files", "dnp")
        }
        finding["pad_numbers"] = sorted(
            {str(pad.number) for pad in getattr(footprint, "pads", ()) if pad.number}
        )
        if _mechanical(footprint):
            tags.append("board_only_mechanical")
    elif kind == "footprint_symbol_mismatch" and (m := _FOOTPRINT_MISMATCH.match(description)):
        finding["board_footprint"] = m["board"]
        finding["symbol_footprint"] = m["symbol"]
        if m["board"] != m["symbol"] and _bare(m["board"]) == _bare(m["symbol"]):
            tags.append("nickname_only")
    elif kind == "footprint_symbol_field_mismatch":
        tags.append("field_only")
        if m := _FIELD_MISMATCH.match(description):
            finding["field"] = m["field"]
    finding["reference"] = reference
    finding["tags"] = tags
    return finding


def _bare(lib_id: str) -> str:
    return lib_id.split(":", 1)[-1]


def _mechanical(footprint: Any) -> bool:
    if getattr(footprint, "board_only", False):
        return True
    if not getattr(footprint, "exclude_from_bom", False):
        return False
    pads = list(getattr(footprint, "pads", ()))
    numbered = {pad.number for pad in pads if pad.number and pad.kind != "np_thru_hole"}
    if not numbered:
        return True
    return len(numbered) == 1 and any((pad.drill or 0.0) >= MOUNTING_DRILL_MM for pad in pads)


def _board_footprints(ctx: DesignContext) -> dict[str, Any] | None:
    try:
        footprints = ctx.board.get_footprints()
    except Exception:
        return None
    return {str(fp.reference): fp for fp in footprints if getattr(fp, "reference", None)}


@register
class FootprintMatchesLibraryCheck(_DrcRuleCheck):
    """Placed footprints are unchanged copies of their library footprints.

    KiCad compares each footprint with the one in its library. A footprint whose
    library cannot be found is reported too (``lib_footprint_issues``).
    """

    id = "pcb.footprint.matches_library"
    name = "Footprint Matches Library"
    description = "Checks that footprints match their library versions"
    types = ("lib_footprint_mismatch", "lib_footprint_issues")
    what = "footprint library mismatch"


@register
class SilkOverCopperCheck(_DrcRuleCheck):
    """No silkscreen is clipped by solder mask openings or the board edge."""

    id = "pcb.silk.over_copper"
    name = "Silkscreen Over Copper"
    description = "Checks that silkscreen stays off exposed copper and the board edge"
    types = ("silk_over_copper", "silk_edge_clearance")
    what = "clipped silkscreen item"


@register
class SilkOverlapCheck(_DrcRuleCheck):
    """No two silkscreen items are closer than the board's silk clearance."""

    id = "pcb.silk.overlap"
    name = "Silkscreen Overlap"
    description = "Checks that silkscreen items do not overlap"
    types = ("silk_overlap",)
    what = "silkscreen overlap"


@register
class TuningRulesCheck(_DrcRuleCheck):
    """KiCad's own length, skew and differential-pair rules hold.

    These DRC rules (``length_out_of_range``, ``skew_out_of_range``,
    ``diff_pair_gap_out_of_range``, ``diff_pair_uncoupled_length_too_long``) only
    exist when the project defines them, in a ``.kicad_dru`` custom rules file
    or a tuning profile. With none defined there is nothing for DRC to break,
    and the check passes.
    """

    id = "pcb.tuning.rules"
    name = "Length Tuning Rules"
    description = "Checks KiCad's length, skew and differential-pair DRC rules"
    types = (
        "length_out_of_range",
        "skew_out_of_range",
        "diff_pair_gap_out_of_range",
        "diff_pair_uncoupled_length_too_long",
        "missing_tuning_profile",
    )
    what = "length tuning violation"
