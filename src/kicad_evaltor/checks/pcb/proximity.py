from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import ClassVar

from kicad_evaltor.board_file import BoardFootprint, FileBoard
from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.connectivity import Bridge, any_of, ref_prefix, series_parts
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.netlist import MergedNetlist
from kicad_evaltor.pcbnew_bridge import (
    BoardGeometry,
    FootprintGeometry,
    PadGeometry,
    polygon_distance,
)
from kicad_evaltor.schematic_file import NetlistUnavailable

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


@dataclass
class DecouplingCoverageParams(CheckParams):
    pins: dict[str, list[str]]
    """The supply pins to cover, by part: ``{"U4": ["5", "8"], "U1": ["3"]}``."""
    return_nets: list[str]
    """The nets a decoupling capacitor returns to, e.g. ``["GND"]``."""
    max_distance_mm: float
    capacitor_references: list[str] | None = None
    """The capacitors to consider; by default every ``capacitor_prefix`` part."""
    capacitor_prefix: str = "C"
    include_dnp: bool = True
    measure_to: str = "pin"
    """``"pin"``: from the capacitor's supply pad to the supply pin it serves.
    ``"part"``: to the nearest copper pad of the part."""
    supply_nets: list[str] | None = None
    """The supplies whose capacitors are judged for stray placement. By default
    the supply nets of the ``pins``, so an RC delay or filter capacitor on a
    signal such as ``/EN`` is not taken for a stray decoupling capacitor."""
    serves: dict[str, list[str]] = field(default_factory=dict)
    """Pins a capacitor may sit next to without those pins needing one, by
    part: ``{"U1": ["5"]}`` lets a VBUS capacitor beside an ESD array's VBUS
    pin count as placed, while that pin is not required to have a capacitor."""
    bridge: dict[str, float | None] | Bridge | None = None
    """Parts that join two nets into one supply, such as 0 ohm links and
    ferrite beads, as reference prefix to the most ohms a part may read and
    still join (``None``: any value): ``{"R": 0, "FB": None}``. Same as
    :func:`~kicad_evaltor.connectivity.series_parts` per prefix; a
    :data:`~kicad_evaltor.connectivity.Bridge` callable is also taken. Nets
    are merged as :meth:`~kicad_evaltor.netlist.FileNetlist.merged` does,
    which needs the schematic's netlist."""

    def validate(self) -> None:
        if not self.pins or not all(self.pins.values()):
            raise ValueError("pins must name at least one supply pin per part")
        if not self.return_nets:
            raise ValueError("return_nets is required")
        if self.max_distance_mm <= 0:
            raise ValueError("max_distance_mm must be > 0")
        if self.measure_to not in ("pin", "part"):
            raise ValueError("measure_to must be 'pin' or 'part'")
        if self.supply_nets is not None and not self.supply_nets:
            raise ValueError("supply_nets must name at least one net, or be None")
        if not all(self.serves.values()):
            raise ValueError("serves must name at least one pin per part")
        if isinstance(self.bridge, dict):
            if not self.bridge:
                raise ValueError("bridge must name at least one reference prefix, or be None")
            for prefix, ohms in self.bridge.items():
                if not prefix or ref_prefix(prefix) != prefix:
                    raise ValueError(f"bridge prefix {prefix!r} must be letters, e.g. 'R'")
                series_parts(prefix, max_ohms=ohms)
        elif self.bridge is not None and not callable(self.bridge):
            raise ValueError("bridge must be a {prefix: max_ohms} mapping or a Bridge callable")


@register
class DecouplingCoverageCheck(Check[DecouplingCoverageParams]):
    """Decoupling both ways: every supply pin has a cap near it, every cap a pin.

    A capacitor counts for a pin when one of its pads is on the pin's supply
    net and another on a return net. ``uncovered_pins`` lists the supply pins
    with no such capacitor within ``max_distance_mm``. ``stray_caps`` lists
    the capacitors between a judged supply and a return net that are not
    within that distance of a pin (or part, with ``measure_to="part"``) on
    that supply: a covered pin or one in ``serves``. The judged supplies are
    ``supply_nets``, by default those of the covered pins, so a capacitor on
    any other net is not judged. A bulk cap the caller does not mean to judge
    is left out of ``capacitor_references``.

    With ``bridge``, nets joined through the bridging parts are one supply
    throughout: a capacitor on ``/3V3`` counts for a regulator output on
    ``/REG_3V3`` across a 0 ohm link. ``supply_nets`` and ``supply_net`` in
    the details are merged names; ``merged_nets`` lists the original nets
    each merged supply covers. Without a netlist the check is an error, not
    run unbridged. Distance is the planar gap between copper pad outlines, as
    pcbnew draws them, not the routed path.
    """

    id = "pcb.decoupling.coverage"
    name = "Decoupling Coverage"
    description = "Checks that supply pins have nearby capacitors and capacitors nearby pins"
    category = CheckCategory.PCB
    Params: ClassVar[type[DecouplingCoverageParams]] = DecouplingCoverageParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        merged: MergedNetlist | None = None
        if p.bridge is not None:
            if not ctx.has_schematic():
                return CheckResult.error(self.id, "bridge needs the schematic's netlist")
            try:
                netlist = ctx.schematic.get_netlist()
            except NetlistUnavailable as exc:
                return CheckResult.error(self.id, f"Cannot read the netlist for bridge: {exc}")
            merged = netlist.merged(self._bridge())
        names = merged.merged_name if merged is not None else {}

        def supply(net: str) -> str:
            return names.get(net, net)

        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        board = ctx.board
        returns = {supply(net) for net in p.return_nets}
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
        missing = [
            ref
            for ref in [*p.pins, *p.serves, *(p.capacitor_references or ())]
            if board.footprint(ref) is None or geo.footprint(ref) is None
        ]
        if missing:
            return CheckResult.fail(
                self.id, f"Not found on board: {', '.join(missing)}", missing=missing
            )
        absent = [
            f"{part}.{number}"
            for part, numbers in [*p.pins.items(), *p.serves.items()]
            for number in numbers
            if (fp := board.footprint(part)) is None or fp.pad(number) is None
        ]
        if absent:
            return CheckResult.fail(
                self.id, f"Pin(s) missing: {', '.join(absent)}", missing_pins=absent
            )

        # Each capacitor's supply nets (merged) and its pads on each of them.
        cap_rails: dict[str, dict[str, list[PadGeometry]]] = {}
        for cap in caps:
            cap_geo = geo.footprint(cap.reference)
            nets = {supply(net) for net in cap.nets if net}
            if cap_geo is None or not nets & returns:
                continue
            cap_rails[cap.reference] = {
                rail: [q for q in cap_geo.pads if q.net and supply(q.net) == rail]
                for rail in sorted(nets - returns)
            }

        pins: list[dict[str, object]] = []
        uncovered: list[dict[str, object]] = []
        # The pads each supply's covered and served pins offer a capacitor.
        targets: dict[str, list[tuple[str, str, list[PadGeometry]]]] = {}
        for part, numbers in [*p.pins.items(), *p.serves.items()]:
            part_file = board.footprint(part)
            part_geo = geo.footprint(part)
            assert part_file is not None
            assert part_geo is not None
            for number in numbers:
                pad = part_file.pad(number)
                assert pad is not None
                rail = supply(pad.net)
                reach = self._target_pads(part_geo, number)
                targets.setdefault(rail, []).append((part, number, reach))
        for part, numbers in p.pins.items():
            part_file = board.footprint(part)
            assert part_file is not None
            for number in numbers:
                pad = part_file.pad(number)
                assert pad is not None
                rail = supply(pad.net)
                reach = next(r for q, n, r in targets[rail] if (q, n) == (part, number))
                best: tuple[float, str] | None = None
                for ref, rails in cap_rails.items():
                    if rail not in rails:
                        continue
                    distance = _gap(rails[rail], reach)
                    if best is None or distance < best[0]:
                        best = (distance, ref)
                entry: dict[str, object] = {
                    "reference": part,
                    "pin": number,
                    "net": pad.net,
                    "supply_net": rail,
                    "nearest_capacitor": best[1] if best else None,
                    "distance_mm": _mm(best[0]) if best else None,
                }
                pins.append(entry)
                if best is None or best[0] > p.max_distance_mm:
                    uncovered.append(entry)

        judged = (
            {supply(net) for net in p.supply_nets}
            if p.supply_nets is not None
            else {str(e["supply_net"]) for e in pins}
        ) - returns
        stray: list[dict[str, object]] = []
        checked_caps = 0
        for ref, rails in cap_rails.items():
            own = sorted(set(rails) & judged)
            if not own:
                continue
            checked_caps += 1
            nearest: tuple[float, str, str] | None = None
            for rail in own:
                for part, number, reach in targets.get(rail, []):
                    distance = _gap(rails[rail], reach)
                    if nearest is None or distance < nearest[0]:
                        nearest = (distance, part, number)
            if nearest is None or nearest[0] > p.max_distance_mm:
                stray.append(
                    {
                        "reference": ref,
                        "supply_nets": own,
                        "nearest_part": nearest[1] if nearest else None,
                        "nearest_pin": f"{nearest[1]}.{nearest[2]}" if nearest else None,
                        "distance_mm": _mm(nearest[0]) if nearest else None,
                    }
                )

        details: dict[str, object] = {
            "pins": pins,
            "uncovered_pins": uncovered,
            "stray_caps": stray,
            "capacitors_checked": checked_caps,
            "supply_nets": sorted(judged),
            "max_distance_mm": p.max_distance_mm,
        }
        if merged is not None:
            details["merged_nets"] = {
                net: list(merged.raw_names[net])
                for net in sorted(judged | set(targets))
                if len(merged.raw_names.get(net, ())) > 1
            }
        issues = []
        if uncovered:
            issues.append(
                f"{len(uncovered)} supply pin(s) without a capacitor within "
                f"{p.max_distance_mm}mm: "
                + ", ".join(f"{e['reference']}.{e['pin']} ({e['net']})" for e in uncovered)
            )
        if stray:
            issues.append(
                f"{len(stray)} capacitor(s) not within {p.max_distance_mm}mm of a covered "
                "or served pin on their supply: " + ", ".join(str(e["reference"]) for e in stray)
            )
        if issues:
            return CheckResult.fail(self.id, "; ".join(issues), **details)
        return CheckResult.pass_(
            self.id,
            f"{len(pins)} supply pin(s) decoupled; {checked_caps} capacitor(s) each near one",
            **details,
        )

    def _bridge(self) -> Bridge:
        bridge = self.params.bridge
        if isinstance(bridge, dict):
            return any_of(*(series_parts(prefix, max_ohms=ohms) for prefix, ohms in bridge.items()))
        assert bridge is not None
        return bridge

    def _target_pads(self, part: FootprintGeometry, number: str) -> list[PadGeometry]:
        if self.params.measure_to == "part":
            return [pad for pad in part.pads if pad.copper_layers]
        return [pad for pad in part.pads if pad.number == number and pad.copper_layers]


def _mm(distance: float) -> float | None:
    return round(distance, 4) if math.isfinite(distance) else None
