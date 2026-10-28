"""Layout overlap checks for a schematic sheet.

These answer "is anything drawn on top of anything else", which is a different
question from ERC: ERC cares whether the circuit is valid, these care whether a
human can read it. All six share one collision engine and one scene extraction,
so the geometry is computed the same way everywhere.

Three deliberate modelling choices:

* Anything-vs-symbol compares *body* boxes only. Pins are meant to reach toward
  wires and toward each other, so pin overlap is normal and reporting it would
  bury the real defects.
* Text-vs-symbol uses the full body-and-pins box, except against the symbol the
  text belongs to. See ``TextSymbolOverlapCheck``.
* The two checks that answer a legibility question -- text against text, and text
  against a symbol -- also reject anything inside ``clearance`` of touching. The
  others report strict overlap. See ``OverlapParams``.

One sheet is one coordinate space with its own paper size, so no two sheets'
items can be compared to each other. Each check therefore runs once per selected
sheet and its findings are concatenated, with ``count`` and ``checked`` totalled
across the run. See ``OverlapParams.sheet``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.collisions import Collision, CollisionItem, colliding_pairs, cross_kind
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.geometry import BBox, Point
from kicad_evaltor.hierarchy import AmbiguousSheet, SheetTarget, select_targets, tree_for
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import (
    Component,
    PinConnection,
    SchematicScene,
    WireSegment,
)


@dataclass
class OverlapParams(CheckParams):
    """Shared tuning for the overlap checks.

    ``clearance`` is the gap a pair must leave between it, measured between the
    two boxes rather than by growing each one, so 0.2 means a 0.2mm gap and not
    0.1mm. Text that comes within it is unreadable even though nothing is drawn
    on top of anything. The checks that answer a legibility question use it; the
    ones that answer "is this drawn on that" do not.

    ``margin`` is applied by growing every box before testing, so a positive
    margin reports items that are too close and a negative one ignores
    overlap smaller than that. Negative is the useful direction in practice:
    symbol outlines are sampled from a quadratic bulge and pins are modelled as
    bare lines, so both run a fraction of a millimeter large and a small
    negative margin stops that measurement noise from reading as a defect.
    Shrinking both boxes by ``m`` shrinks their shared region by ``2m`` on each
    axis, so ``margin=-0.3`` drops exactly the overlaps under 0.6mm in width or
    height -- but only overlaps: the shrunken pair can still come within
    ``clearance`` and be reported as a near miss.

    ``min_overlap_mm`` is the plain spelling of that negative margin, and the one
    to use. A pair counts only when its boxes really overlap by at least this
    much in both width and height, measured on the boxes as drawn. That is the
    same set of overlaps ``margin=-min_overlap_mm / 2`` keeps, without the near
    misses ``clearance`` would add back, so with it set a finding is always a
    real overlap of that depth. It is applied before a pair of drawn things is
    reported, so a shallow touch between two pieces of a label cannot hide a deep
    one between two other pieces. It cannot be combined with a nonzero
    ``margin``, which would apply the threshold twice. To move a suite that
    used a negative ``margin`` ``m`` and then kept only overlaps at least ``w``
    wide and high (measured on the shrunken boxes), use
    ``min_overlap_mm = w - 2 * m``: each box shrank by ``-m`` on every side, so
    the shared region lost ``-2m`` on each axis. ``margin=-0.2`` with a 0.6mm
    filter is ``min_overlap_mm=1.0``, not 0.6. It applies to the
    box-against-box checks (text/text, text/symbol, text/wire, symbol/symbol).
    A box depth is a heuristic, not visibility: in the test design's
    ``overlaps`` variant the ``+3V3`` text shares only 0.36mm of height with
    R13's box yet its rendered ink touches R13's pin. ``method="render"`` is the test of what is visible,
    and rejects ``min_overlap_mm``: rendered ink already ignores box-only
    overlap, and its depth is not measured fully enough to threshold.

    Every box finding carries ``overlap_width_mm`` and ``overlap_height_mm``: the
    shared region of the two boxes as drawn, 0 on an axis where they only come
    near each other. ``width``/``height`` stay what they were, the region of the
    boxes after ``margin``.

    ``ignore`` drops named items, which is how a project silences a placement it
    has accepted.

    ``sheet`` chooses which sheets to look at. ``None`` is the root sheet only,
    which is what a flat design means by "the sheet"; ``"all"`` is every sheet in
    the tree; anything else is one sheet, named by ``Sheetname``, uuid instance
    path or linked-file stem. A name that matches nothing skips the check and
    lists the names that would have worked, rather than reporting no findings as
    though the design were clean.
    """

    margin: float = 0.0
    clearance: float = 0.2
    min_overlap_mm: float = 0.0
    ignore: list[str] = field(default_factory=list)
    sheet: str | None = None
    require_complete: bool = False
    method: str = "bounds"
    include_own_pins: bool = False
    include_symbol_pins: bool = False
    resolution_mm: float = 0.001

    def validate(self) -> None:
        # A margin larger than the geometry it applies to collapses boxes, which
        # the engine handles, so the only thing worth catching is a value that
        # is not a number at all.
        for name, value in (
            ("margin", self.margin),
            ("clearance", self.clearance),
            ("min_overlap_mm", self.min_overlap_mm),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{name} must be a number, got {value!r}")
        if not math.isfinite(self.min_overlap_mm) or self.min_overlap_mm < 0:
            raise ValueError("min_overlap_mm must be finite and >= 0")
        if self.min_overlap_mm and self.margin:
            raise ValueError(
                "min_overlap_mm already shrinks the boxes (it is margin=-min_overlap_mm/2); "
                "set one or the other"
            )
        if self.sheet is not None and not isinstance(self.sheet, str):
            raise TypeError(f"sheet must be a string or None, got {self.sheet!r}")
        if self.clearance < 0:
            raise ValueError("clearance must be >= 0")
        if self.method not in ("bounds", "render"):
            raise ValueError("method must be bounds or render")
        if self.resolution_mm <= 0:
            raise ValueError("resolution_mm must be positive")
        if self.method == "render" and self.margin != 0:
            raise ValueError(
                "Rendered ink uses clearance; box margins apply only to method='bounds'"
            )
        # Rendered ink already drops the box-only overlaps a minimum depth exists
        # to ignore, and its overlap depth is a witness that the strokes meet
        # (measured only until it clears the error bound), not a full depth that
        # could be compared to a threshold.
        if self.method == "render" and self.min_overlap_mm:
            raise ValueError(
                "min_overlap_mm applies to method='bounds'; rendered ink reports only "
                "strokes that actually meet"
            )


# One sheet's findings on their way into a run's totals. ``extra`` accumulates
# rather than overwrites, so a per-sheet fact such as a paper size survives the
# sum instead of the last sheet winning.
@dataclass
class _Findings:
    summaries: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)
    checked: int = 0
    extra: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, other: _Findings) -> None:
        self.summaries.extend(other.summaries)
        self.details.extend(other.details)
        self.checked += other.checked
        for key, values in other.extra.items():
            self.extra.setdefault(key, []).extend(values)


def _targets(
    ctx: DesignContext, check_id: str, selector: str | None
) -> tuple[list[SheetTarget] | None, CheckResult | None]:
    """The sheets to look at, or the reason the check cannot run at all."""
    if not ctx.has_schematic():
        return None, None
    schematic = ctx.schematic
    if not isinstance(schematic, FileSchematic):
        return None, CheckResult.skip(check_id, "Layout checks read the .kicad_sch file directly")
    tree = tree_for(schematic)
    try:
        targets, found = select_targets(tree, selector)
    except AmbiguousSheet as exc:
        return None, CheckResult.error(check_id, str(exc), reason_code="ambiguous_selection")
    if not found:
        return None, CheckResult.skip(
            check_id, f"Unknown sheet {selector!r}", sheets=[s.name for s in tree.sheets()]
        )
    return targets, None


def _over_sheets(
    ctx: DesignContext, check_id: str, selector: str | None, body, require_complete: bool = False
) -> tuple[_Findings | None, CheckResult | None]:
    """Run one sheet's worth of work for each selected sheet and total the findings."""
    targets, blocker = _targets(ctx, check_id, selector)
    if blocker is not None:
        return None, blocker
    if targets is None:
        return None, CheckResult.skip(check_id, "No schematic available")
    problems = tree_for(ctx.schematic).problems()
    paths = [t.sheet.path for t in targets]
    relevant = [
        p
        for p in problems
        if selector is None or selector.strip().lower() == "all" or p.sheet.path in paths
    ]
    if require_complete and relevant:
        return None, CheckResult.error(
            check_id,
            "Selected scope is incomplete",
            reason_code="scope_incomplete",
            problems=[
                {"kind": p.kind, "sheet_path": p.sheet.path, "detail": p.detail} for p in relevant
            ],
        )
    totals = _Findings()
    for target in targets:
        try:
            totals.add(body(target))
        except Exception as exc:
            from kicad_evaltor.ink import InkUnavailable

            if not isinstance(exc, InkUnavailable):
                raise
            return None, CheckResult.error(
                check_id, str(exc), reason_code="ink_unavailable", sheet_path=target.sheet.path
            )
    return totals, None


def _qualified(items: list[CollisionItem], prefix: str) -> list[CollisionItem]:
    """Copy ``items`` with their labels qualified by ``prefix``."""
    if not prefix:
        return items
    return [replace(item, label=f"{prefix}{item.label}") for item in items]


def _text_items(scene: SchematicScene) -> list[CollisionItem]:
    """One item per drawn piece of text: a label's flag is measured apart from its
    letters, so the empty corners of the box around both never collide."""
    return [
        CollisionItem("text", t.label, piece, t.owner, {t.field: t.content}, group=t)
        for t in scene.texts
        for piece in t.pieces
    ]


def _whole_text_items(scene: SchematicScene) -> list[CollisionItem]:
    """One item per run of text, boxing all of its pieces together."""
    return [
        CollisionItem("text", t.label, t.bbox, t.owner, {t.field: t.content}) for t in scene.texts
    ]


def _distinct(items: list[CollisionItem]) -> int:
    """How many drawn things ``items`` covers, counting a label's pieces once."""
    return len({id(item.group) if item.group is not None else id(item) for item in items})


def _rendered_collisions(
    target: SheetTarget, params: OverlapParams, first_kind: str, second_kind: str
) -> _Findings:
    from dataclasses import asdict

    from kicad_evaltor.ink import InkRenderer, InkUnavailable, ink_distance
    from kicad_evaltor.schematic_items import _component_texts

    scene = target.scene
    renderers = target.schematic._ink_renderers
    if params.resolution_mm not in renderers:
        renderers[params.resolution_mm] = InkRenderer(
            target.schematic, resolution_mm=params.resolution_mm
        )
    renderer = renderers[params.resolution_mm]
    items: list[CollisionItem] = []
    if "text" in (first_kind, second_kind):
        power_texts = [
            t
            for s in target.schematic.get_symbols()
            if s.lib_id.startswith("power:")
            for t in _component_texts(s, s.reference, include_power_value=True)
            if t.field == "Value"
        ]
        items.extend(
            CollisionItem("text", t.label, t.bbox, t.owner, group=t)
            for t in (*scene.texts, *power_texts)
        )
    if "symbol" in (first_kind, second_kind):
        items.extend(
            CollisionItem(
                "symbol",
                c.reference,
                c.bbox or BBox(c.at.x, c.at.y, c.at.x, c.at.y),
                c.reference,
                group=c,
            )
            for c in scene.components
        )
    items = _drop_ignored(items, params.ignore)
    evidence = {}

    def ink(item, own):
        obj = item.group
        if item.kind == "text":
            return renderer.object_ink(
                reference=obj.owner, field=obj.field if obj.owner else None, uuid=obj.uuid or None
            )
        return renderer.object_ink(
            reference=obj.reference,
            uuid=obj.uuid or None,
            include_pins=(
                params.include_symbol_pins
                if first_kind == second_kind == "symbol"
                else not own or params.include_own_pins
            ),
        )

    def keep(collision):
        a, b = collision.first, collision.second
        if (a.kind, b.kind) not in ((first_kind, second_kind), (second_kind, first_kind)):
            return False
        own = a.owner is not None and a.owner == b.owner
        measured = ink_distance(ink(a, own), ink(b, own), error_bound_mm=2 * params.resolution_mm)
        separation = -measured.overlap_depth_mm if measured.intersects else measured.distance_mm
        if separation is not None and abs(separation - params.clearance) <= measured.error_bound_mm:
            raise InkUnavailable(
                "Requested clearance lies within rendered measurement uncertainty; reduce resolution_mm or inspect the raw measurement"
            )
        matches = measured.intersects or (
            measured.distance_mm is not None and measured.distance_mm < params.clearance
        )
        if matches:
            evidence[(id(a), id(b))] = asdict(measured)
        return matches

    exact_items = []
    for item in items:
        shapes = ink(item, False)
        box = BBox.union_all(s.bbox for s in shapes)
        if box is not None:
            exact_items.append(replace(item, bbox=box))

    # The broad phase must also reach an item whose bound is uncertain by the
    # renderer resolution. That resolution is never an acceptance tolerance.
    pairs = colliding_pairs(
        exact_items, clearance=params.clearance + 2 * params.resolution_mm, keep=keep
    )
    details = []
    for pair in pairs:
        info = pair.as_dict()
        info.update(evidence[(id(pair.first), id(pair.second))])
        info.update(
            sheet_path=target.sheet.path,
            first_uuid=getattr(pair.first.group, "uuid", ""),
            second_uuid=getattr(pair.second.group, "uuid", ""),
        )
        details.append(info)
    return _Findings(summaries=[p.describe() for p in pairs], details=details, checked=len(items))


def _symbol_items(scene: SchematicScene, *, include_pins: bool) -> list[CollisionItem]:
    """One item per drawn piece of each symbol: every body graphic, and every pin
    line when ``include_pins``. A box around the whole symbol would reach into the
    empty space between them, where nothing is drawn to collide with."""
    items: list[CollisionItem] = []
    for comp in scene.components:
        pieces = comp.body_pieces + (comp.pin_bboxes if include_pins else ())
        items.extend(
            CollisionItem("symbol", comp.reference, piece, comp.reference, comp.properties, comp)
            for piece in pieces
        )
    return items


def _wire_items(scene: SchematicScene) -> list[CollisionItem]:
    return [CollisionItem("wire", w.label, w.bbox) for w in scene.wires]


def _drop_ignored(items: list[CollisionItem], ignore: list[str]) -> list[CollisionItem]:
    if not ignore:
        return items
    ignored = set(ignore)
    return [item for item in items if item.label not in ignored]


def _box_overlap(collision: Collision) -> tuple[float, float]:
    """Width and height the two boxes, as drawn, share; 0 where they only near."""
    a, b = collision.first.bbox, collision.second.bbox
    width = min(a.max_x, b.max_x) - max(a.min_x, b.min_x)
    height = min(a.max_y, b.max_y) - max(a.min_y, b.min_y)
    if width <= 0 or height <= 0:
        return 0.0, 0.0
    return width, height


def _deep_enough(min_overlap_mm: float, keep=None):
    """A ``keep`` filter for the engine, or ``keep`` itself when there is no minimum.

    It runs inside the engine so a shallow piece pair is never what marks two
    drawn things as reported.
    """
    if not min_overlap_mm:
        return keep

    def deep(collision: Collision) -> bool:
        width, height = _box_overlap(collision)
        if width < min_overlap_mm or height < min_overlap_mm:
            return False
        return keep is None or keep(collision)

    return deep


def _box_details(collision: Collision, sheet_path: str) -> dict[str, Any]:
    width, height = _box_overlap(collision)
    return {
        **collision.as_dict(),
        "overlap_width_mm": width,
        "overlap_height_mm": height,
        "sheet_path": sheet_path,
    }


def _kind_first(collisions, kind: str) -> list:
    """Order each collision so the given kind comes first.

    The engine emits pairs in sweep order, which depends on coordinates. Pinning
    one kind to ``first`` keeps the reported pairs stable and readable.
    """
    ordered = []
    for collision in collisions:
        if collision.first.kind == kind:
            ordered.append(collision)
        else:
            ordered.append(replace(collision, first=collision.second, second=collision.first))
    return ordered


def _text_first(collisions) -> list:
    return _kind_first(collisions, "text")


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
        overlaps=details,
    )


class _OverlapCheck(Check[OverlapParams]):
    """Base for the box-against-box checks."""

    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[OverlapParams]] = OverlapParams

    def items(self, scene: SchematicScene) -> list[CollisionItem]:
        raise NotImplementedError

    def required_clearance(self) -> float:
        """The gap a pair must leave to count, or 0 for overlap alone."""
        return 0.0

    def filter(self, collisions):
        """Narrow the raw engine output down to this check's question."""
        return collisions

    def run(self, ctx: DesignContext) -> CheckResult:
        findings, blocker = _over_sheets(
            ctx, self.id, self.params.sheet, self._run_sheet, self.params.require_complete
        )
        if blocker is not None:
            return blocker
        assert findings is not None
        result = _report(self.id, findings.summaries, findings.details, findings.checked)
        result.details["margin"] = self.params.margin
        result.details["min_overlap_mm"] = self.params.min_overlap_mm
        return result

    def _run_sheet(self, target: SheetTarget) -> _Findings:
        """This check's findings on one sheet.

        ``ignore`` is applied before the sheet prefix, so an accepted placement is
        named the same way on every sheet: ``ignore=["SDA"]`` silences ``SDA`` on
        the root and ``Main.SDA`` on a subsheet.
        """
        if self.params.method == "render":
            if self.id.endswith("text_text_overlap"):
                return _rendered_collisions(target, self.params, "text", "text")
            if self.id.endswith("symbol_symbol_overlap"):
                return _rendered_collisions(target, self.params, "symbol", "symbol")
            from kicad_evaltor.ink import InkUnavailable

            raise InkUnavailable(f"Rendered ink is not supported by {self.id}")
        items = _qualified(
            _drop_ignored(self.items(target.scene), self.params.ignore), target.prefix
        )
        collisions = self.filter(
            colliding_pairs(
                items,
                margin=self.params.margin,
                clearance=self.required_clearance(),
                keep=_deep_enough(self.params.min_overlap_mm),
            )
        )
        return _Findings(
            summaries=[c.describe() for c in collisions],
            details=[_box_details(c, target.sheet.path) for c in collisions],
            checked=_distinct(items),
        )


@register
class TextTextOverlapCheck(_OverlapCheck):
    """Reports text drawn on top of other text, or too near it to read.

    Two values in a row have no outline to hide behind, so the clearance is what
    makes this check useful: a 0.2mm gap between two runs of ink is enough to read
    both, and anything tighter is not.
    """

    id = "sch.layout.text_text_overlap"
    name = "Text Over Text Overlap"
    description = "Reports text drawn on top of other text"
    category = CheckCategory.SCHEMATIC

    def required_clearance(self) -> float:
        return self.params.clearance

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

    def required_clearance(self) -> float:
        return self.params.clearance

    def run(self, ctx: DesignContext) -> CheckResult:
        findings, blocker = _over_sheets(
            ctx, self.id, self.params.sheet, self._run_sheet, self.params.require_complete
        )
        if blocker is not None:
            return blocker
        assert findings is not None
        result = _report(self.id, findings.summaries, findings.details, findings.checked)
        result.details["margin"] = self.params.margin
        result.details["min_overlap_mm"] = self.params.min_overlap_mm
        return result

    def _run_sheet(self, target: SheetTarget) -> _Findings:
        if self.params.method == "render":
            return _rendered_collisions(target, self.params, "text", "symbol")
        ignore = self.params.ignore
        clearance = self.required_clearance()
        scene = target.scene
        texts = _qualified(_drop_ignored(_text_items(scene), ignore), target.prefix)
        bodies = _qualified(
            _drop_ignored(_symbol_items(scene, include_pins=False), ignore), target.prefix
        )
        pinned = _qualified(
            _drop_ignored(_symbol_items(scene, include_pins=True), ignore), target.prefix
        )

        def text_and_symbol(collision) -> tuple[CollisionItem, CollisionItem] | None:
            kinds = (collision.first.kind, collision.second.kind)
            if kinds == ("text", "symbol"):
                return collision.first, collision.second
            if kinds == ("symbol", "text"):
                return collision.second, collision.first
            return None

        def own_straddle(collision) -> bool:
            # Only the body counts against a symbol's own text, and only a piece
            # the text crosses: text sitting wholly inside a piece, like a value
            # inside a resistor's rectangle, is where KiCad puts it.
            pair = text_and_symbol(collision)
            if pair is None:
                return False
            text, piece = pair
            return text.owner == piece.owner and not piece.bbox.contains(text.bbox)

        def foreign(collision) -> bool:
            pair = text_and_symbol(collision)
            return pair is not None and pair[0].owner != pair[1].owner

        def against(symbols: list[CollisionItem], keep) -> list:
            pairs = colliding_pairs(
                texts + symbols,
                margin=self.params.margin,
                clearance=clearance,
                keep=_deep_enough(self.params.min_overlap_mm, keep),
            )
            return _text_first(pairs)

        collisions = against(bodies, own_straddle) + against(pinned, foreign)
        return _Findings(
            summaries=[c.describe() for c in collisions],
            details=[_box_details(c, target.sheet.path) for c in collisions],
            checked=_distinct(texts) + _distinct(pinned),
        )


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
class SymbolWireOverlapCheck(Check[OverlapParams]):
    """Reports a wire drawn under a symbol's own artwork.

    A wire that ends at a pin is a connection, not a defect, so this does not
    report the power symbol on every ground wire on the sheet. What it reports is
    a wire that leaves its own pin heading back *into* the body, or crosses a
    body with no pin under it at all. Either way the wire is drawn underneath the
    symbol and disappears into it, so nothing on the sheet shows where the net
    goes.

    ``PWR_FLAG`` is the case worth having. Its artwork is a flag on a pole, and
    laying it straight onto the wire it feeds hides that wire and leaves the flag
    looking shorted to nothing.
    """

    id = "sch.layout.symbol_wire_overlap"
    name = "Symbol Body Over Wire"
    description = "Reports a wire drawn under the symbol it starts from"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[OverlapParams]] = OverlapParams

    def run(self, ctx: DesignContext) -> CheckResult:
        findings, blocker = _over_sheets(
            ctx, self.id, self.params.sheet, self._run_sheet, self.params.require_complete
        )
        if blocker is not None:
            return blocker
        assert findings is not None
        result = _report(self.id, findings.summaries, findings.details, findings.checked)
        result.details["margin"] = self.params.margin
        return result

    def _run_sheet(self, target: SheetTarget) -> _Findings:
        ignore = set(self.params.ignore)
        wires = [w for w in target.scene.wires if w.label not in ignore]
        bodies = [
            component
            for component in target.scene.components
            if component.body_pieces and component.reference not in ignore
        ]

        # A wire is measured against each drawn piece of a body, not the box
        # around all of them, and reported at most once per symbol.
        collisions = [
            collision
            for wire in wires
            for component in bodies
            if (
                collision := next(
                    (
                        found
                        for piece in component.body_pieces
                        if (found := _buried_wire(wire, component, piece, target.prefix))
                        is not None
                    ),
                    None,
                )
            )
            is not None
        ]
        return _Findings(
            summaries=[c.describe() for c in collisions],
            details=[c.as_dict() for c in collisions],
            checked=len(wires) + len(bodies),
        )


def _buried_wire(
    wire: WireSegment, component: Component, body: BBox, prefix: str
) -> Collision | None:
    """The finding for one wire over one body, or None when the wire connects."""
    shared = body.intersection(wire.bbox)
    if shared is None:
        return None
    pin = _pin_under(wire, component)
    if pin is not None and not _runs_backwards(wire, pin):
        return None
    symbol = CollisionItem(
        "symbol", f"{prefix}{component.reference}", body, component.reference, component.properties
    )
    return Collision(symbol, CollisionItem("wire", wire.label, wire.bbox), shared)


def _pin_under(wire: WireSegment, component: Component) -> PinConnection | None:
    """The pin this wire lands on, if it lands on one.

    The tolerance is half the wire's own width, which is as far as its drawn
    line reaches from its path. A wire stopping short of a pin has not connected
    to it, and one crossing a body with no pin underneath is reported for that
    reason rather than for burying anything.
    """
    nearest: PinConnection | None = None
    nearest_distance = wire.width / 2.0
    for pin in component.pin_connections:
        distance = _point_to_segment(pin.at, wire.start, wire.end)
        if distance <= nearest_distance:
            nearest, nearest_distance = pin, distance
    return nearest


def _runs_backwards(wire: WireSegment, pin: PinConnection) -> bool:
    """True when the wire leaves this pin heading across the symbol's own body."""
    dx = wire.bbox.center[0] - pin.at[0]
    dy = wire.bbox.center[1] - pin.at[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return False
    heading = (dx / length) * pin.into_body[0] + (dy / length) * pin.into_body[1]
    # A wire arriving square to the pin is a connection, and cos(270°) is
    # -1.8e-16 rather than 0, so square has to be told apart from a hair.
    return heading > 1e-9


def _point_to_segment(point: Point, start: Point, end: Point) -> float:
    """Shortest distance from a point to a segment, ends included."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    span = dx * dx + dy * dy
    if span == 0.0:
        return math.dist(point, start)
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / span))
    return math.dist(point, (start[0] + t * dx, start[1] + t * dy))


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
    """Reports text placed outside the drawing area of the sheet it is drawn on.

    The one layout check that cannot be run off a shared scene alone: each sheet
    has its own paper size, so a subsheet's text is measured against *its* paper
    and never against the root's. ``details["sheet"]`` stays the size of the first
    sheet the run covered -- the root's, which is what a single-sheet caller has
    always read -- and ``details["sheets"]`` lists every sheet with its own size.
    """

    id = "sch.layout.text_off_sheet"
    name = "Text Outside Sheet"
    description = "Reports text placed outside the drawing area of the sheet"
    category = CheckCategory.SCHEMATIC

    def run(self, ctx: DesignContext) -> CheckResult:
        findings, blocker = _over_sheets(
            ctx, self.id, self.params.sheet, self._run_sheet, self.params.require_complete
        )
        if blocker is not None:
            return blocker
        assert findings is not None
        sizes = findings.extra["sizes"]
        result = _report(self.id, findings.summaries, findings.details, findings.checked)
        result.details["sheet"] = sizes[0]["size"] if sizes else []
        result.details["sheets"] = sizes
        result.details["margin"] = self.params.margin
        return result

    def _run_sheet(self, target: SheetTarget) -> _Findings:
        sheet = target.sheet
        # The paper size comes from the file this sheet is drawn on, which is the
        # only place it is written down.
        width, height = target.schematic.sheet_size()
        drawing_area = BBox(0.0, 0.0, width, height)

        items = _qualified(
            _drop_ignored(_whole_text_items(target.scene), self.params.ignore), target.prefix
        )
        # Growing the sheet by the margin is how a margin reads here: text is
        # allowed to sit that far past the edge before it counts as off-sheet.
        allowed = drawing_area.inflate(self.params.margin)
        outside = [item for item in items if not allowed.contains(item.bbox)]
        return _Findings(
            summaries=[f"{item.label} lies outside the sheet" for item in outside],
            details=[
                {
                    "label": item.label,
                    "sheet": sheet.name,
                    "properties": dict(item.properties),
                    "bbox": [item.bbox.min_x, item.bbox.min_y, item.bbox.max_x, item.bbox.max_y],
                }
                for item in outside
            ],
            checked=len(items),
            extra={"sizes": [{"sheet": sheet.name, "size": [width, height]}]},
        )
