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
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.geometry import Point
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import (
    DEFAULT_LINE_WIDTH,
    PinConnection,
    SchematicScene,
    WireSegment,
    extract,
)

# How close counts as "on the pin". A flag is drawn around the pin it belongs to
# and a wire's line reaches half its width either side of its path, so half the
# default line width is the distance within which the two are the same point.
_TOLERANCE = DEFAULT_LINE_WIDTH / 2.0


@dataclass
class NoConnectParams(CheckParams):
    """Tuning for the check.

    ``ignore`` drops pins by ``reference.number``, which is how a project accepts
    a pin that is deliberately left open.
    """

    ignore: list[str] = field(default_factory=list)


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
    # A label needs no wire: KiCad connects one whose anchor sits on the pin, and
    # the reserved cell always contains its own anchor.
    return any(text.bbox.inflate(_TOLERANCE).contains_point(*pin.at) for text in scene.texts)


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

        scene = extract(schematic)
        pins = [pin for comp in scene.components for pin in comp.pin_connections]
        # A pin the symbol hides is not drawn, so no wire, label or flag can reach
        # it on the sheet and there is nothing for a reader to be confused by. KiCad
        # keeps such pins to carry a symbol's reserved and NC pins, so they are most
        # of what a multi-unit sensor or MCU symbol has.
        drawn = [pin for pin in pins if not pin.hidden]
        ignore = set(self.params.ignore)

        floating = [
            [flag[0], flag[1]]
            for flag in scene.no_connects
            if not any(math.dist(flag, pin.at) <= _TOLERANCE for pin in pins)
        ]
        unmarked = [
            pin.label for pin in drawn if pin.label not in ignore and not _marked(scene, pin)
        ]

        counts = {"flags": len(scene.no_connects), "pins": len(drawn)}
        if not floating and not unmarked:
            return CheckResult.pass_(
                self.id, f"All {len(scene.no_connects)} flags sit on a pin", **counts
            )
        parts = [f"{len(floating)} {_plural(len(floating), 'flag')} marking nothing"]
        if unmarked:
            parts.append(f"{len(unmarked)} pins reached by nothing")
        return CheckResult.fail(
            self.id,
            ", ".join(parts),
            floating_flags=floating,
            unmarked_pins=unmarked,
            **counts,
        )


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else noun + "s"
