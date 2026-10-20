from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.board_file import BoardFootprint, FileBoard
from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pcbnew_bridge import BoardGeometry, PadGeometry, polygon_distance

_GROUND_WORDS = {"GROUND", "EARTH", "CHASSIS", "0V"}


def is_ground(net: str) -> bool:
    """Whether a net is a ground: GND, AGND, PGND, GNDA, VSS, VSSA, 0V...

    The name is split into words at punctuation, so ``/AGND`` and ``PGND_1``
    count and ``+10V`` or ``VCC_10V`` do not.
    """
    for word in re.split(r"[^A-Z0-9]+", net.upper()):
        if word in _GROUND_WORDS or "GND" in word or word.startswith("VSS"):
            return True
    return False


@dataclass
class FootprintNearParams(CheckParams):
    reference: str | None = None
    """The part that must be close, e.g. ``"C5"``."""
    target: str | None = None
    """What it must be close to: a footprint (``"U1"``) or one pad (``"U1.13"``)."""
    max_distance_mm: float = 2.0
    decoupling: str | None = None
    """Instead: an IC reference whose every power-input pin must have a
    capacitor between that pin's net and ground within ``max_distance_mm``."""
    capacitor_prefix: str = "C"
    return_nets: list[str] | None = None
    capacitor_references: list[str] | None = None
    include_dnp: bool = True

    def validate(self) -> None:
        if self.max_distance_mm <= 0:
            raise ValueError("max_distance_mm must be > 0")
        if self.decoupling:
            if self.reference or self.target:
                raise ValueError("decoupling replaces reference and target")
        elif not (self.reference and self.target):
            raise ValueError("Give reference and target, or decoupling")


@register
class FootprintNearCheck(Check[FootprintNearParams]):
    """A part sits within a distance of another part or pad.

    Distance is the planar gap between copper pad outlines, as pcbnew draws
    them. This measures placement, not routed path length or loop inductance.

    The ``decoupling`` mode works out the pairs itself. Each pad of the IC whose
    pin type is ``power_in`` (read from the board, which carries the schematic's
    pin types) needs a capacitor with one pad on that pin's net and the other on
    a ground net, placed within the distance of that pin.
    """

    id = "pcb.footprint.near"
    name = "Footprint Near"
    description = "Checks that a part is placed close to another part or pin"
    category = CheckCategory.PCB
    Params: ClassVar[type[FootprintNearParams]] = FootprintNearParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        if self.params.decoupling:
            return self._decoupling(ctx.board, geo, self.params.decoupling)
        return self._near(geo)

    def _near(self, geo: BoardGeometry) -> CheckResult:
        p = self.params
        assert p.reference is not None
        assert p.target is not None
        part = geo.footprint(p.reference)
        if part is None:
            return CheckResult.fail(self.id, f"Footprint '{p.reference}' not found on board")
        ref, _, number = p.target.partition(".")
        target = geo.footprint(ref)
        if target is None:
            return CheckResult.fail(self.id, f"Footprint '{ref}' not found on board")
        targets = (
            target.pads_numbered(number)
            if number
            else [pad for pad in target.pads if pad.copper_layers]
        )
        if not targets:
            return CheckResult.fail(self.id, f"Pad {p.target} not found on board")
        distance = _gap([pad for pad in part.pads if pad.copper_layers], targets)
        details = {"distance_mm": round(distance, 4), "max_distance_mm": p.max_distance_mm}
        if distance > p.max_distance_mm:
            return CheckResult.fail(
                self.id,
                f"{p.reference} is {distance:.2f}mm from {p.target} > {p.max_distance_mm}mm",
                **details,
            )
        return CheckResult.pass_(
            self.id, f"{p.reference} is {distance:.2f}mm from {p.target}", **details
        )

    def _decoupling(self, board: FileBoard, geo: BoardGeometry, ic_ref: str) -> CheckResult:
        p = self.params
        ic = board.footprint(ic_ref)
        ic_geo = geo.footprint(ic_ref)
        if ic is None or ic_geo is None:
            return CheckResult.fail(self.id, f"Footprint '{ic_ref}' not found on board")

        def ground(net: str) -> bool:
            return net in p.return_nets if p.return_nets is not None else is_ground(net)

        power_nets = sorted(
            {pad.net for pad in ic.pads if pad.pin_type == "power_in" and pad.net}
            - {net for net in (pad.net for pad in ic.pads) if ground(net)}
        )
        if not power_nets:
            return CheckResult.skip(self.id, f"{ic_ref} has no power-input pins on the board")
        caps = [
            fp
            for fp in board.footprints
            if (
                fp.reference in p.capacitor_references
                if p.capacitor_references is not None
                else _is_cap(fp, p.capacitor_prefix)
            )
            and (p.include_dnp or not fp.dnp)
        ]
        pins = []
        failing = []
        required = {
            pad.number for pad in ic.pads if pad.pin_type == "power_in" and pad.net in power_nets
        }
        for number in sorted(required):
            ic_pads = [pad for pad in ic_geo.pads if pad.number == number and pad.copper_layers]
            if not ic_pads:
                return CheckResult.error(self.id, f"Missing geometry for {ic_ref}.{number}")
            net = ic_pads[0].net
            candidates = [c for c in caps if net in c.nets and any(ground(n) for n in c.nets)]
            best: tuple[float, str] | None = None
            for cap in candidates:
                cap_geo = geo.footprint(cap.reference)
                if cap_geo is None:
                    continue
                distance = _gap([pad for pad in cap_geo.pads if pad.net == net], ic_pads)
                if best is None or distance < best[0]:
                    best = (distance, cap.reference)
            entry: dict[str, object] = {
                "net": net,
                "pins": sorted({pad.number for pad in ic_pads}),
                "nearest_capacitor": best[1] if best else None,
                "distance_mm": round(best[0], 4) if best else None,
            }
            pins.append(entry)
            if best is None or best[0] > p.max_distance_mm:
                failing.append(f"{ic_ref}.{number} ({net})")
        if failing:
            return CheckResult.fail(
                self.id,
                f"{ic_ref}: no decoupling capacitor within {p.max_distance_mm}mm on "
                + ", ".join(failing),
                power_pins=pins,
            )
        return CheckResult.pass_(
            self.id, f"{ic_ref}: every power input is decoupled", power_pins=pins
        )


def _is_cap(fp: BoardFootprint, prefix: str) -> bool:
    rest = fp.reference[len(prefix) :]
    return fp.reference.startswith(prefix) and rest[:1].isdigit()


def _gap(first: list[PadGeometry], second: list[PadGeometry]) -> float:
    return min(
        (
            polygon_distance(a.polygon, b.polygon)
            for a in first
            for b in second
            if a.polygon and b.polygon
        ),
        default=float("inf"),
    )
