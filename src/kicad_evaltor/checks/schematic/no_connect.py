"""Floating no-connect flags, and pins nothing reaches.

A no-connect flag is a promise that a pin is unconnected on purpose. This check
finds the two ways that promise breaks:

* the flag is stranded -- drawn on a symbol's border, or in mid-air, where it
  marks nothing at all
* a pin has no wire, no label and no flag, so nothing on the sheet says whether
  it was ever meant to connect to anything

Both are geometry, not connectivity, which is what makes this worth having
alongside ERC: it needs no KiCad and no netlist, and it still says *where* the
promise was broken. Only pins the symbol draws count: a hidden pin is not on the
sheet, so nothing there can be wrong.

A pin can only be reached by something drawn on the same sheet, so the check
runs once per selected sheet and totals its findings, exactly as the layout
checks do. See ``NoConnectParams.sheet``.

A flag need not sit on the pin itself. KiCad accepts one at the far end of a
short wire drawn from the pin, and so does this check: see
``NoConnectParams.allow_wire_stub``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.drawing_topology import wire_groups
from kicad_evaltor.geometry import Point
from kicad_evaltor.hierarchy import SheetTarget, select_targets, tree_for
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import (
    DEFAULT_LINE_WIDTH,
    PinConnection,
    SchematicScene,
    WireSegment,
)

# How close counts as "on the pin". A flag is drawn around the pin it belongs to
# and a wire's line reaches half its width either side of its path, so half the
# default line width is the distance within which the two are the same point.
_TOLERANCE = DEFAULT_LINE_WIDTH / 2.0
# The kinds of text KiCad connects by their anchor.
_LABEL_KINDS = ("label", "global_label", "hierarchical_label")


@dataclass
class NoConnectParams(CheckParams):
    """Tuning for the check.

    ``ignore`` drops pins by ``reference.number``, which is how a project accepts
    a pin that is deliberately left open. It matches the unprefixed label, so one
    entry silences the same pin on every sheet.

    ``sheet`` chooses which sheets to look at: ``None`` for the root sheet only,
    ``"all"`` for every sheet in the tree, or one sheet by name, uuid instance
    path or linked-file stem.

    ``allow_wire_stub`` accepts a flag at the end of a wire whose drawn group
    reaches exactly one visible pin and carries no label: KiCad's ERC treats the
    flag as marking that pin, so this matches ERC. A stub that reaches two or
    more pins, or a label, is a different ERC error (a no-connect on a connected
    net), and its flag is still reported. Groups follow KiCad's wire rule -- a
    wire connects only at its ends or at a junction -- so a stub that merely
    touches another wire part-way along it is still a stub. ``False`` restores
    the old rule that a flag must sit on a pin.
    """

    ignore: list[str] = field(default_factory=list)
    sheet: str | None = None
    allow_wire_stub: bool = True

    def validate(self) -> None:
        if self.sheet is not None and not isinstance(self.sheet, str):
            raise TypeError(f"sheet must be a string or None, got {self.sheet!r}")
        if not isinstance(self.allow_wire_stub, bool):
            raise TypeError(f"allow_wire_stub must be a bool, got {self.allow_wire_stub!r}")


def _reached_by(point: Point, wire: WireSegment) -> bool:
    """True when the wire's drawn line comes within its own width of ``point``."""
    dx, dy = wire.end[0] - wire.start[0], wire.end[1] - wire.start[1]
    span = dx * dx + dy * dy
    if span == 0.0:
        return math.dist(point, wire.start) <= wire.width / 2.0
    t = max(
        0.0, min(1.0, ((point[0] - wire.start[0]) * dx + (point[1] - wire.start[1]) * dy) / span)
    )
    return math.dist(point, (wire.start[0] + t * dx, wire.start[1] + t * dy)) <= wire.width / 2.0


def _marked(scene: SchematicScene, pin: PinConnection) -> bool:
    """True when something on the sheet claims this pin: a flag, a wire, a label."""
    if any(math.dist(pin.at, flag) <= _TOLERANCE for flag in scene.no_connects):
        return True
    if any(_reached_by(pin.at, wire) for wire in scene.wires):
        return True
    # A label needs no wire: KiCad connects one whose anchor sits on the pin. Its
    # letters are drawn clear of that point, so the anchor is what is compared.
    return any(
        text.anchor is not None and math.dist(pin.at, text.anchor) <= _TOLERANCE
        for text in scene.texts
        if text.field in _LABEL_KINDS
    )


@register
class NoConnectFloatingCheck(Check[NoConnectParams]):
    """Reports no-connect flags that mark nothing, and drawn pins nothing reaches."""

    id = "sch.noconnect.floating"
    name = "Unmarked No-Connect"
    description = "Reports no-connect flags not on a pin, and drawn pins with nothing attached"
    category = CheckCategory.SCHEMATIC
    Params = NoConnectParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")
        schematic = ctx.schematic
        if not isinstance(schematic, FileSchematic):
            return CheckResult.skip(self.id, "Layout checks read the .kicad_sch file directly")

        tree = tree_for(schematic)
        targets, found = select_targets(tree, self.params.sheet)
        if not found:
            return CheckResult.skip(
                self.id,
                f"Unknown sheet {self.params.sheet!r}",
                sheets=[s.name for s in tree.sheets()],
            )

        ignore = set(self.params.ignore)
        flags = 0
        pins = 0
        floating: list[list[float]] = []
        unmarked: list[str] = []
        stubs: list[dict] = []
        for target in targets or ():
            sheet_floating, sheet_unmarked, sheet_stubs = self._sheet_findings(target, ignore)
            flags += len(target.scene.no_connects)
            pins += _drawn_pins(target.scene)
            floating += sheet_floating
            unmarked += sheet_unmarked
            stubs += sheet_stubs

        if not floating and not unmarked:
            where = "a pin or a wire stub" if stubs else "a pin"
            return CheckResult.pass_(
                self.id,
                f"All {flags} flags sit on {where}",
                flags=flags,
                pins=pins,
                stub_flags=stubs,
                sheets=[t.sheet.name for t in targets or ()],
            )
        parts = [f"{len(floating)} {_plural(len(floating), 'flag')} marking nothing"]
        if unmarked:
            parts.append(f"{len(unmarked)} pins reached by nothing")
        return CheckResult.fail(
            self.id,
            ", ".join(parts),
            floating_flags=floating,
            unmarked_pins=unmarked,
            stub_flags=stubs,
            flags=flags,
            pins=pins,
            sheets=[t.sheet.name for t in targets or ()],
        )

    def _sheet_findings(
        self, target: SheetTarget, ignore: set[str]
    ) -> tuple[list[list[float]], list[str], list[dict]]:
        """The floating flags, the unmarked pins and the stub flags on one sheet.

        A flag is reported by where it is drawn, not by a qualified name: it has
        no reference of its own, so ``sheets`` below is what says which sheets a
        run covered. An unmarked pin is qualified by its sheet, since its label
        already reads ``U1.4``. A flag accepted on a wire stub is listed with
        the pin it marks, so a caller can still see, or refuse, every one.
        """
        scene = target.scene
        pins = [pin for comp in scene.components for pin in comp.pin_connections]
        # A sheet block's pins take a flag too: an unused one is closed off the
        # same way an unused symbol pin is.
        sheet_pins = [(pin.x, pin.y) for sheet in target.schematic.sheets() for pin in sheet.pins]
        targets = [pin.at for pin in pins] + sheet_pins
        on_pin = [
            flag
            for flag in scene.no_connects
            if any(math.dist(flag, point) <= _TOLERANCE for point in targets)
        ]
        stub_pins = (
            _stub_pins(scene, [f for f in scene.no_connects if f not in on_pin])
            if self.params.allow_wire_stub
            else {}
        )
        floating = [
            [flag[0], flag[1]]
            for flag in scene.no_connects
            if flag not in on_pin and flag not in stub_pins
        ]
        stubs = [
            {"at": [flag[0], flag[1]], "pin": f"{target.prefix}{pin.label}"}
            for flag, pin in stub_pins.items()
        ]
        unmarked = [
            f"{target.prefix}{pin.label}"
            for pin in pins
            if not pin.hidden
            and pin.label not in ignore
            and not _marked(scene, pin)
            and not _touches_another_pin(pin, pins)
        ]
        return floating, unmarked, stubs


def _stub_pins(scene: SchematicScene, flags: list[Point]) -> dict[Point, PinConnection]:
    """The pin each flag marks through a wire stub, for flags that do.

    A flag counts when it sits on an end of a wire whose drawn group reaches
    exactly one visible pin and nothing that names a net: no label anchor and no
    power symbol. The wire itself already reaches that pin, so the pin is marked
    without further help.
    """
    if not flags:
        return {}
    groups = wire_groups(scene, coordinate_tolerance_mm=_TOLERANCE, join_mid_wire=False)
    visible: dict[int, list[PinConnection]] = {}
    for comp in scene.components:
        for pin in comp.pin_connections:
            root = groups.pins.get((pin.component, pin.number))
            if root is not None and not pin.hidden:
                visible.setdefault(root, []).append(pin)
    # Power symbols are left out of the drawn groups, so they are caught here
    # with the labels: either one puts the stub on a named net.
    named = [t.anchor for t in scene.texts if t.field in _LABEL_KINDS and t.anchor is not None]
    named += [pin.at for comp in scene.power_components for pin in comp.pin_connections]
    labelled = {
        groups.wires[i]
        for i, wire in enumerate(scene.wires)
        if any(_reached_by(point, wire) for point in named)
    }
    found: dict[Point, PinConnection] = {}
    for flag in flags:
        for i, wire in enumerate(scene.wires):
            if min(math.dist(flag, wire.start), math.dist(flag, wire.end)) > _TOLERANCE:
                continue
            root = groups.wires[i]
            if root not in labelled and len(visible.get(root, ())) == 1:
                found[flag] = visible[root][0]
                break
    return found


def _touches_another_pin(pin: PinConnection, pins: list[PinConnection]) -> bool:
    """True when another pin ends on this one: KiCad connects them with no wire.

    A power symbol placed straight onto a capacitor's pin is the usual case.
    """
    return any(
        other is not pin and not other.hidden and math.dist(pin.at, other.at) <= _TOLERANCE
        for other in pins
    )


def _drawn_pins(scene: SchematicScene) -> int:
    """How many pins this sheet actually draws.

    A pin the symbol hides is not drawn, so no wire, label or flag can reach it
    on the sheet and there is nothing for a reader to be confused by. KiCad keeps
    such pins to carry a symbol's reserved and NC pins, so they are most of what a
    multi-unit sensor or MCU symbol has.
    """
    return sum(1 for comp in scene.components for pin in comp.pin_connections if not pin.hidden)


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"
