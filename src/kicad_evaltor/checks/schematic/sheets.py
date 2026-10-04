"""Checks on the sheet tree itself: where it points, what it exposes, what repeats.

These four read the hierarchy rather than the drawing. Nothing here is about
geometry or connectivity, which is deliberate: ``kicad-cli sch erc`` already
walks the whole hierarchy and owns the electrical question, so duplicating it
would only produce a second, worse answer. What ERC does *not* say is whether the
files a design points at are all present, whether each sheet's pins have a
matching label, or whether a sheet name or page number is used twice -- and each
of those is a link in the chain between "the file parses" and "the circuit is
right".

Every check here reads ``.kicad_sch`` files directly and skips cleanly when the
context has no schematic, or has one that is not a file-backed schematic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.hierarchy import (
    CYCLE,
    MISSING,
    UNREADABLE,
    SchematicTree,
    SheetProblem,
    tree_for,
)
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.sheets import ROOT_PATH, Sheet


@dataclass
class SheetParams(CheckParams):
    """No tuning yet.

    Declared explicitly so each check's ``Params`` is its own attribute rather
    than the shared empty base, which is how the other check families are built.
    """


def _tree(ctx: DesignContext, check_id: str) -> tuple[SchematicTree | None, CheckResult | None]:
    """The sheet tree, or the reason the check cannot run."""
    if not ctx.has_schematic():
        return None, None
    schematic = ctx.schematic
    if not isinstance(schematic, FileSchematic):
        return None, CheckResult.skip(check_id, "Sheet checks read the .kicad_sch file directly")
    return tree_for(schematic), None


def _linked(tree: SchematicTree) -> list[Sheet]:
    """Every sheet a ``(sheet ...)`` block describes, i.e. not the root file.

    The root file is not a link: it is where the links are, so a check about
    links has nothing to say about it.
    """
    return [sheet for sheet in tree.sheets() if sheet.path != ROOT_PATH]


def _link_count(tree: SchematicTree) -> int:
    """How many ``(sheet ...)`` blocks the design declares.

    Not how many sheets loaded: a link that does not resolve is still a link the
    design declares, and counting only the survivors would let a report say "1 of
    1 links do not resolve".
    """
    return sum(len(tree.children(sheet)) for sheet in tree.sheets())


def _missing_reason(problem: SheetProblem) -> str:
    """Why a link could not be followed, in the words a reader wants.

    A missing file, a directory sitting where a file was expected, and a file
    that is not a schematic are three different mistakes with three different
    fixes, so they are reported as three different things rather than as one
    "could not load".
    """
    sheet = problem.sheet
    if problem.kind == MISSING:
        return "Sheetfile is empty" if not sheet.filename else "no candidate path exists"
    assert sheet.resolved is not None
    if sheet.resolved.is_dir():
        return "a directory, not a schematic file"
    return "file does not parse as a .kicad_sch"


@register
class SheetFileMissingCheck(Check[SheetParams]):
    """Reports a sheet link that does not lead to a readable schematic file.

    A design whose root names a file that was never committed, or names a
    folder, still parses: every symbol on every sheet that *was* found reads
    fine. That is what makes this worth its own check rather than an exception
    somewhere deeper -- the design looks healthy until someone opens the sheet.
    """

    id = "sch.sheet.file_missing"
    name = "Sheet File Missing"
    description = "Reports a subsheet link that does not resolve to a schematic file"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SheetParams]] = SheetParams

    def run(self, ctx: DesignContext) -> CheckResult:
        tree, blocker = _tree(ctx, self.id)
        if blocker is not None:
            return blocker
        if tree is None:
            return CheckResult.skip(self.id, "No schematic available")

        checked = _link_count(tree)
        missing = [
            {
                "sheet": problem.sheet.name,
                "filename": problem.sheet.filename,
                "candidates": [str(c) for c in problem.sheet.candidates],
                "reason": _missing_reason(problem),
            }
            for problem in tree.problems()
            if problem.kind in (MISSING, UNREADABLE)
        ]

        if not missing:
            noun = "link" if checked == 1 else "links"
            verb = "resolves" if checked == 1 else "resolve"
            return CheckResult.pass_(
                self.id,
                f"All {checked} sheet {noun} {verb}",
                count=0,
                checked=checked,
                missing=missing,
            )
        noun = "link" if len(missing) == 1 else "links"
        return CheckResult.fail(
            self.id,
            f"{len(missing)} of {checked} sheet {noun} do not resolve; first: "
            f"{missing[0]['filename'] or missing[0]['sheet']} ({missing[0]['reason']})",
            count=len(missing),
            checked=checked,
            missing=missing,
        )


@register
class SheetPinMismatchCheck(Check[SheetParams]):
    """Reports sheet pins with no matching hierarchical label, and the reverse.

    A sheet pin and a hierarchical label of the same name are the two halves of
    one connection across a sheet boundary. Name one without the other and KiCad
    shows no error at all: the connection is simply absent, and the net on one
    side quietly stops where the pin is. Comparing names is the whole check.

    Shapes are deliberately not compared. Whether a pin's electrical type agrees
    with the label it meets belongs to ERC, which walks the hierarchy and already
    reports it properly.
    """

    id = "sch.sheet.pin_mismatch"
    name = "Sheet Pin Mismatch"
    description = "Reports sheet pins and hierarchical labels that do not pair up by name"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SheetParams]] = SheetParams

    def run(self, ctx: DesignContext) -> CheckResult:
        tree, blocker = _tree(ctx, self.id)
        if blocker is not None:
            return blocker
        if tree is None:
            return CheckResult.skip(self.id, "No schematic available")

        pins_without_label: list[dict[str, Any]] = []
        labels_without_pin: list[dict[str, Any]] = []
        checked = 0

        for sheet in _linked(tree):
            child = tree.schematic_for(sheet)
            if child is None:
                continue
            checked += 1
            pins = {pin.name for pin in sheet.pins}
            labels = {label.name for label in child.hierarchical_labels()}
            pins_without_label += [
                {"sheet": sheet.name, "pin": name} for name in sorted(pins - labels)
            ]
            labels_without_pin += [
                {"sheet": sheet.name, "label": name} for name in sorted(labels - pins)
            ]

        found = len(pins_without_label) + len(labels_without_pin)
        if not found:
            noun = "sheet" if checked == 1 else "sheets"
            return CheckResult.pass_(
                self.id,
                f"Pins and labels match on all {checked} loaded {noun}",
                count=0,
                checked=checked,
                pins_without_label=pins_without_label,
                labels_without_pin=labels_without_pin,
            )
        noun = "mismatch" if found == 1 else "mismatches"
        return CheckResult.fail(
            self.id,
            f"{found} pin/label {noun}; first: {pins_without_label or labels_without_pin}",
            count=found,
            checked=checked,
            pins_without_label=pins_without_label,
            labels_without_pin=labels_without_pin,
        )


@register
class SheetCycleCheck(Check[SheetParams]):
    """Reports a sheet that links back into its own ancestors.

    KiCad will not open a hierarchy that cycles, so this is not a defect a
    reader trips over: it is a defect a *machine* trips over, and the walk in
    :mod:`kicad_evaltor.hierarchy` would otherwise recurse forever. Reporting it
    turns a hang into a finding.

    The same file instantiated under two different sheet symbols is not a cycle.
    KiCad does that deliberately, and each instance keeps its own page number and
    its own instance path.
    """

    id = "sch.sheet.cycle"
    name = "Sheet Cycle"
    description = "Reports a subsheet link that reaches one of its own ancestors"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SheetParams]] = SheetParams

    def run(self, ctx: DesignContext) -> CheckResult:
        tree, blocker = _tree(ctx, self.id)
        if blocker is not None:
            return blocker
        if tree is None:
            return CheckResult.skip(self.id, "No schematic available")

        cycles = [
            {"sheet": problem.sheet.name, "chain": list(problem.chain)}
            for problem in tree.problems()
            if problem.kind == CYCLE
        ]
        sheets = tree.sheets()
        if not cycles:
            return CheckResult.pass_(
                self.id,
                f"No cycles across {len(sheets)} sheets",
                count=0,
                checked=len(sheets),
                cycles=cycles,
            )
        noun = "cycles" if len(cycles) > 1 else "cycle"
        return CheckResult.fail(
            self.id,
            f"{len(cycles)} sheet {noun}; first: {' -> '.join(cycles[0]['chain'])}",
            count=len(cycles),
            checked=len(sheets),
            cycles=cycles,
        )


@register
class SheetNameOrPageCollisionCheck(Check[SheetParams]):
    """Reports a sheet name used twice among siblings, and a page number used twice.

    A duplicate sibling name is ambiguous wherever a name is used to mean one
    sheet -- a report, a per-sheet parameter, a pin matched by name. A duplicate
    page number is worse, because KiCad prints "page N" in a title block and two
    sheets claiming it makes the printed documentation point at the wrong one.

    A *missing* page number is only reported when the root file has a
    ``(sheet_instances ...)`` section at all. Its presence means the design came
    out of KiCad with pages being tracked, so an unnumbered instance is an
    omission; without it the file is hand-written or old, and demanding a page
    number would be inventing a requirement KiCad never asked for.
    """

    id = "sch.sheet.name_or_page_collision"
    name = "Sheet Name Or Page Collision"
    description = "Reports a sheet name or page number used by two sheets"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[SheetParams]] = SheetParams

    def run(self, ctx: DesignContext) -> CheckResult:
        tree, blocker = _tree(ctx, self.id)
        if blocker is not None:
            return blocker
        if tree is None:
            return CheckResult.skip(self.id, "No schematic available")

        sheets = tree.sheets()
        duplicate_names = _duplicate_names(tree)
        pages = _page_numbers(tree)
        duplicate_pages = [
            {"page": page, "sheets": names}
            for page, names in sorted(pages.items())
            # The blank bucket is "unnumbered", not a page number, so two of them
            # are not a collision -- that is what ``unnumbered`` reports, and only
            # when the root shows the design ever tracked pages at all.
            if page and len(names) > 1
        ]
        unnumbered = pages.get("", []) if _tracks_pages(tree) else []

        found = len(duplicate_names) + len(duplicate_pages) + len(unnumbered)
        if not found:
            return CheckResult.pass_(
                self.id,
                f"Names and pages are unique across {len(sheets)} sheets",
                checked=len(sheets),
                duplicate_names=duplicate_names,
                duplicate_pages=duplicate_pages,
                unnumbered=unnumbered,
            )
        parts = []
        if duplicate_names:
            parts.append(f"{len(duplicate_names)} duplicated sheet name(s)")
        if duplicate_pages:
            parts.append(f"{len(duplicate_pages)} duplicated page number(s)")
        if unnumbered:
            parts.append(f"{len(unnumbered)} sheet(s) with no page number")
        return CheckResult.fail(
            self.id,
            f"{len(sheets)} sheets, " + ", ".join(parts),
            count=found,
            checked=len(sheets),
            duplicate_names=duplicate_names,
            duplicate_pages=duplicate_pages,
            unnumbered=unnumbered,
        )


def _duplicate_names(tree: SchematicTree) -> list[dict[str, Any]]:
    """Sibling sheets sharing a name, reported against the parent that holds both.

    Two sheets in different subtrees may share a name: that is how a designer
    gives the same block to two branches. Only siblings are ambiguous.
    """
    found: list[dict[str, Any]] = []
    for parent in tree.sheets():
        counts = Counter(child.name for child in tree.children(parent))
        found.extend(
            {"sheet": parent.name, "name": name, "count": count}
            for name, count in sorted(counts.items())
            if count > 1
        )
    return found


def _page_numbers(tree: SchematicTree) -> dict[str, list[str]]:
    """Page number to the sheets claiming it, with ``""`` holding the unnumbered.

    A sheet records its page per project, so a multi-project design legitimately
    has several; every one of them is a claim on a page number.
    """
    pages: dict[str, list[str]] = {}
    for sheet in tree.sheets():
        for page in _pages_of(tree, sheet):
            pages.setdefault(page, []).append(sheet.name)
    return pages


def _pages_of(tree: SchematicTree, sheet: Sheet) -> tuple[str, ...]:
    """The page numbers ``sheet`` claims, which is one blank when it claims none.

    The root has no ``(sheet ...)`` block to carry an ``(instances ...)``, so its
    page is read from the root's ``(sheet_instances ...)`` instead. A root with
    no such section claims nothing rather than claiming a blank, which is what
    keeps "no page number" from being reported against every flat schematic.
    """
    if sheet.path == ROOT_PATH:
        page = tree.root.page_instances().get(ROOT_PATH)
        return (page,) if page else ()
    return tuple(sheet.pages.values()) or ("",)


def _tracks_pages(tree: SchematicTree) -> bool:
    return bool(tree.root.page_instances())
