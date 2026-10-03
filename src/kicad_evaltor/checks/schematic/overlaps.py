"""Layout overlap checks for a schematic sheet.

These answer "is anything drawn on top of anything else", which is a different
question from ERC: ERC cares whether the circuit is valid, these care whether a
human can read it. All five share one collision engine and one scene extraction,
so the geometry is computed the same way everywhere.

Two deliberate modelling choices:

* Symbol-vs-symbol compares *body* boxes only. Pins are meant to reach toward
  each other, so pin overlap is normal and reporting it would bury the real
  defects.
* Text-vs-symbol uses the full body-and-pins box, except against the symbol the
  text belongs to. See ``TextSymbolOverlapCheck``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.collisions import CollisionItem, colliding_pairs, cross_kind
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.geometry import BBox
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import SchematicScene, extract


@dataclass
class OverlapParams(CheckParams):
    """Shared tuning for the overlap checks.

    ``margin`` is applied by growing every box before testing, so a positive
    margin reports items that are too close and a negative one ignores
    overlap smaller than that. Negative is the useful direction in practice:
    symbol outlines are sampled from a quadratic bulge and pins are modelled as
    bare lines, so both run a fraction of a millimeter large and a small
    negative margin stops that measurement noise from reading as a defect.

    ``ignore`` drops named items, which is how a project silences a placement it
    has accepted.
    """

    margin: float = 0.0
    ignore: list[str] = field(default_factory=list)

    def validate(self) -> None:
        # A margin larger than the geometry it applies to collapses boxes, which
        # the engine handles, so the only thing worth catching is a value that
        # is not a number at all.
        if isinstance(self.margin, bool) or not isinstance(self.margin, int | float):
            raise TypeError(f"margin must be a number, got {self.margin!r}")


def _scene(ctx: DesignContext) -> tuple[SchematicScene | None, CheckResult | None]:
    """Extract the sheet geometry, or explain why the check cannot run."""
    if not ctx.has_schematic():
        return None, None
    schematic = ctx.schematic
    if not isinstance(schematic, FileSchematic):
        return None, CheckResult.skip(
            "sch.layout", "Layout checks read the .kicad_sch file directly"
        )
    return extract(schematic), None


def _text_items(scene: SchematicScene) -> list[CollisionItem]:
    return [
        CollisionItem("text", t.label, t.bbox, t.owner, {t.field: t.content}) for t in scene.texts
    ]


def _symbol_items(scene: SchematicScene, *, include_pins: bool) -> list[CollisionItem]:
    items = []
    for comp in scene.components:
        box = comp.bbox if include_pins else comp.body_bbox
        if box is not None:
            items.append(
                CollisionItem("symbol", comp.reference, box, comp.reference, comp.properties)
            )
    return items


def _wire_items(scene: SchematicScene) -> list[CollisionItem]:
    return [CollisionItem("wire", w.label, w.bbox) for w in scene.wires]


def _drop_ignored(items: list[CollisionItem], ignore: list[str]) -> list[CollisionItem]:
    if not ignore:
        return items
    ignored = set(ignore)
    return [item for item in items if item.label not in ignored]


def _text_first(collisions) -> list:
    """Order each collision so the text item comes first.

    The engine emits pairs in sweep order, which depends on coordinates. Pinning
    the text to ``first`` keeps the reported pairs stable and readable.
    """
    ordered = []
    for collision in collisions:
        if collision.first.kind == "text":
            ordered.append(collision)
        else:
            ordered.append(replace(collision, first=collision.second, second=collision.first))
    return ordered


def _report(check_id: str, summaries: list[str], details: list[dict], checked: int) -> CheckResult:
    """Turn findings into a result, summarising rather than spamming."""
    if not summaries:
        return CheckResult.pass_(check_id, f"No overlaps among {checked} items", checked=checked)
    noun = "overlap" if len(summaries) == 1 else "overlaps"
    return CheckResult.fail(
        check_id,
        f"{len(summaries)} {noun} found; first: {summaries[0]}",
        count=len(summaries),
        checked=checked,
        overlaps=details[:20],
    )


class _OverlapCheck(Check[OverlapParams]):
    """Base for the box-against-box checks."""

    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[OverlapParams]] = OverlapParams

    def items(self, scene: SchematicScene) -> list[CollisionItem]:
        raise NotImplementedError

    def filter(self, collisions):
        """Narrow the raw engine output down to this check's question."""
        return collisions

    def run(self, ctx: DesignContext) -> CheckResult:
        scene, blocker = _scene(ctx)
        if blocker is not None:
            return blocker
        if scene is None:
            return CheckResult.skip(self.id, "No schematic available")

        items = _drop_ignored(self.items(scene), self.params.ignore)
        collisions = self.filter(colliding_pairs(items, margin=self.params.margin))
        return self._result(items, collisions)

    def _result(self, items: list[CollisionItem], collisions: list) -> CheckResult:
        result = _report(
            self.id,
            [c.describe() for c in collisions],
            [c.as_dict() for c in collisions],
            len(items),
        )
        result.details["margin"] = self.params.margin
        return result


@register
class TextTextOverlapCheck(_OverlapCheck):
    id = "sch.layout.text_text_overlap"
    name = "Text Over Text Overlap"
    description = "Reports text drawn on top of other text"
    category = CheckCategory.SCHEMATIC

    def items(self, scene: SchematicScene) -> list[CollisionItem]:
        return _text_items(scene)


@register
class TextSymbolOverlapCheck(_OverlapCheck):
    """Reports text drawn on top of a symbol.

    Two rules, because one does not fit both cases.

    Against *another* symbol the full body-and-pins box counts: landing on someone
    else's pin stub is a real collision.

    Against its *own* symbol only the body counts, and only a straddle is a
    defect. Pins are excluded because KiCad puts a reference directly above a
    body, which is exactly where the topmost pin stub is, so measuring pins would
    report the default placement of every part. A body that fully contains its own
    text is excluded too: a resistor is mostly empty space, and KiCad does put a
    value inside one. What is unreadable is text crossing the outline, half in and
    half out, or sitting on top of it.
    """

    id = "sch.layout.text_symbol_overlap"
    name = "Text Over Symbol Overlap"
    description = "Reports text drawn on top of a symbol"
    category = CheckCategory.SCHEMATIC

    def run(self, ctx: DesignContext) -> CheckResult:
        scene, blocker = _scene(ctx)
        if blocker is not None:
            return blocker
        if scene is None:
            return CheckResult.skip(self.id, "No schematic available")

        ignore = self.params.ignore
        texts = _drop_ignored(_text_items(scene), ignore)
        bodies = _drop_ignored(_symbol_items(scene, include_pins=False), ignore)
        pinned = _drop_ignored(_symbol_items(scene, include_pins=True), ignore)

        def against(symbols: list[CollisionItem]) -> list:
            pairs = colliding_pairs(texts + symbols, margin=self.params.margin)
            return _text_first(cross_kind(pairs, "text", "symbol"))

        def own(collision) -> bool:
            return bool(collision.first.owner) and collision.first.owner == collision.second.owner

        def straddles(collision) -> bool:
            return not collision.second.bbox.contains(collision.first.bbox)

        collisions = [c for c in against(bodies) if own(c) and straddles(c)]
        collisions += [c for c in against(pinned) if not own(c)]
        return self._result(texts + pinned, collisions)


@register
class TextWireOverlapCheck(_OverlapCheck):
    """Reports text drawn on top of a wire. Opt-in, and weaker than it looks.

    KiCad draws schematic text with an opaque background that masks whatever is
    behind it, so the ordinary case -- a net label lying on the wire it labels,
    which is how KiCad renders every net label -- is not a defect at all. On the
    demo sheet all three findings are exactly that.

    Telling that apart from the case that *is* a defect, text masking an
    unrelated net's wire, needs connectivity rather than geometry, which this
    check does not have. It is therefore kept out of the demo suite. Use it when
    you want to police text placement strictly, not as a default gate.
    """

    id = "sch.layout.text_wire_overlap"
    name = "Text Over Wire Overlap"
    description = "Reports text drawn on top of a wire (see note: KiCad masks it)"
    category = CheckCategory.SCHEMATIC

    def items(self, scene: SchematicScene) -> list[CollisionItem]:
        return _text_items(scene) + _wire_items(scene)

    def filter(self, collisions):
        return _text_first(cross_kind(collisions, "text", "wire"))


@register
class SymbolSymbolOverlapCheck(_OverlapCheck):
    id = "sch.layout.symbol_symbol_overlap"
    name = "Symbol Body Overlap"
    description = "Reports two symbol bodies drawn on top of each other"
    category = CheckCategory.SCHEMATIC

    def items(self, scene: SchematicScene) -> list[CollisionItem]:
        return _symbol_items(scene, include_pins=False)


@register
class TextOffSheetCheck(_OverlapCheck):
    id = "sch.layout.text_off_sheet"
    name = "Text Outside Sheet"
    description = "Reports text placed outside the drawing area of the sheet"
    category = CheckCategory.SCHEMATIC

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")
        schematic = ctx.schematic
        if not isinstance(schematic, FileSchematic):
            return CheckResult.skip(self.id, "Layout checks read the .kicad_sch file directly")

        scene = extract(schematic)
        width, height = schematic.sheet_size()
        sheet = BBox(0.0, 0.0, width, height)

        items = _drop_ignored(_text_items(scene), self.params.ignore)
        # Growing the sheet by the margin is how a margin reads here: text is
        # allowed to sit that far past the edge before it counts as off-sheet.
        allowed = sheet.inflate(self.params.margin)
        outside = [item for item in items if not allowed.contains(item.bbox)]
        result = _report(
            self.id,
            [f"{item.label} lies outside the sheet" for item in outside],
            [
                {
                    "label": item.label,
                    "properties": dict(item.properties),
                    "bbox": [item.bbox.min_x, item.bbox.min_y, item.bbox.max_x, item.bbox.max_y],
                }
                for item in outside
            ],
            len(items),
        )
        result.details["sheet"] = [width, height]
        result.details["margin"] = self.params.margin
        return result
