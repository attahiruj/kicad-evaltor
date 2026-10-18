"""Copper sizing checks: plane coverage, current capacity, vias, thermal vias."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pcbnew_bridge import point_in_polygon

_MM_PER_MIL = 0.0254


@dataclass
class PlaneCoverageParams(CheckParams):
    net: str
    layer: str
    min_fraction: float
    """Least share of the board's area the net's fill must cover on ``layer``."""

    def validate(self) -> None:
        if not 0 < self.min_fraction <= 1:
            raise ValueError("min_fraction must be in (0, 1]")


@register
class PlaneCoverageCheck(Check[PlaneCoverageParams]):
    """A net's pour covers enough of the board on one layer, e.g. a GND plane.

    Fill comes from KiCad's zone filler; board area from the outline pcbnew
    builds.
    """

    id = "pcb.plane.coverage"
    name = "Plane Coverage"
    description = "Checks how much of a layer a net's pour covers"
    category = CheckCategory.PCB
    Params: ClassVar[type[PlaneCoverageParams]] = PlaneCoverageParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        if not geo.outline.closed or geo.outline.area <= 0:
            return CheckResult.fail(self.id, "Edge.Cuts does not form a closed outline")
        filled = sum(
            z.filled_area.get(p.layer, 0.0) for z in geo.zones if z.net == p.net and not z.rule_area
        )
        fraction = filled / geo.outline.area
        details = {
            "filled_mm2": round(filled, 3),
            "board_mm2": round(geo.outline.area, 3),
            "fraction": round(fraction, 4),
        }
        if fraction < p.min_fraction:
            return CheckResult.fail(
                self.id,
                f"{p.net} covers {fraction:.0%} of {p.layer} < {p.min_fraction:.0%}",
                **details,
            )
        return CheckResult.pass_(self.id, f"{p.net} covers {fraction:.0%} of {p.layer}", **details)


def ipc2221_width(
    current_a: float, temp_rise_c: float, copper_mm: float, *, external: bool
) -> float:
    """Least track width in mm to carry ``current_a`` with ``temp_rise_c`` of heating.

    IPC-2221's fit, the same one KiCad's PCB Calculator uses:
    I = k · ΔT^0.44 · A^0.725, with A in mil² and k = 0.048 for outer layers,
    0.024 for inner ones.
    """
    k = 0.048 if external else 0.024
    area_mil2 = float((current_a / (k * temp_rise_c**0.44)) ** (1 / 0.725))
    return area_mil2 / (copper_mm / _MM_PER_MIL) * _MM_PER_MIL


@dataclass
class CurrentCapacityParams(CheckParams):
    net: str
    current_a: float
    temp_rise_c: float = 10.0
    copper_mm: float | None = None
    """Copper thickness; None reads it from the board's stackup (35 µm if unset)."""

    def validate(self) -> None:
        if self.current_a <= 0:
            raise ValueError("current_a must be > 0")
        if self.temp_rise_c <= 0:
            raise ValueError("temp_rise_c must be > 0")


@register
class CurrentCapacityCheck(Check[CurrentCapacityParams]):
    """Every track on a net is wide enough for the current it must carry.

    Widths are held to the IPC-2221 formula for each track's own layer: outer
    layers cool better than inner ones, so they may be narrower.
    """

    id = "pcb.trace.current_capacity"
    name = "Trace Current Capacity"
    description = "Checks that a net's tracks are wide enough for its current (IPC-2221)"
    category = CheckCategory.PCB
    Params: ClassVar[type[CurrentCapacityParams]] = CurrentCapacityParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        board = ctx.board
        tracks = board.tracks_on(p.net)
        if not tracks:
            return CheckResult.fail(self.id, f"Net '{p.net}' has no tracks")
        copper = board.copper_layers
        outer = {copper[0], copper[-1]} if copper else {"F.Cu", "B.Cu"}
        required: dict[str, float] = {}
        narrow = []
        for track in tracks:
            if track.layer not in required:
                thickness = p.copper_mm or board.copper_thickness(track.layer) or 0.035
                required[track.layer] = ipc2221_width(
                    p.current_a, p.temp_rise_c, thickness, external=track.layer in outer
                )
            if track.width < required[track.layer] - 1e-6:
                narrow.append(
                    {
                        "layer": track.layer,
                        "start": list(track.start),
                        "end": list(track.end),
                        "width_mm": track.width,
                        "required_mm": round(required[track.layer], 4),
                    }
                )
        details = {
            "required_width_mm": {k: round(v, 4) for k, v in required.items()},
            "tracks_checked": len(tracks),
        }
        if narrow:
            return CheckResult.fail(
                self.id,
                f"{len(narrow)} track(s) on {p.net} too narrow for {p.current_a}A",
                tracks=narrow,
                **details,
            )
        return CheckResult.pass_(
            self.id, f"All {len(tracks)} tracks on {p.net} can carry {p.current_a}A", **details
        )


@dataclass
class ViaRulesParams(CheckParams):
    min_drill_mm: float | None = None
    min_diameter_mm: float | None = None
    min_annular_ring_mm: float | None = None
    """Each None falls back to the board's design rule of the same name."""
    min_microvia_drill_mm: float | None = None
    min_microvia_diameter_mm: float | None = None
    """Microvias have rules of their own (``min_microvia_drill``,
    ``min_microvia_diameter``), as in KiCad."""
    allow_via_in_pad: bool = True
    nets: list[str] | None = None


@register
class ViaRulesCheck(Check[ViaRulesParams]):
    """Vias meet drill, diameter and annular-ring minimums, and stay out of pads.

    Minimums default to the board's own design rules (``min_through_hole_diameter``,
    ``min_via_diameter``, ``min_via_annular_width``). Spotting a via inside a pad
    needs pad outlines from pcbnew.
    """

    id = "pcb.via.rules"
    name = "Via Rules"
    description = "Checks via drill, diameter, annular ring and via-in-pad"
    category = CheckCategory.PCB
    Params: ClassVar[type[ViaRulesParams]] = ViaRulesParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        board = ctx.board
        rules = board.net_classes.rules
        min_drill = (
            p.min_drill_mm if p.min_drill_mm is not None else rules.get("min_through_hole_diameter")
        )
        min_size = (
            p.min_diameter_mm if p.min_diameter_mm is not None else rules.get("min_via_diameter")
        )
        min_ring = (
            p.min_annular_ring_mm
            if p.min_annular_ring_mm is not None
            else rules.get("min_via_annular_width")
        )
        micro_drill = (
            p.min_microvia_drill_mm
            if p.min_microvia_drill_mm is not None
            else (rules.get("min_microvia_drill"))
        )
        micro_size = (
            p.min_microvia_diameter_mm
            if p.min_microvia_diameter_mm is not None
            else rules.get("min_microvia_diameter")
        )
        vias = [v for v in board.vias if not p.nets or v.net in p.nets]
        pads = []
        if not p.allow_via_in_pad:
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
            assert geo is not None
            pads = [
                (f"{fp.reference}.{pad.number}", pad)
                for fp in geo.footprints
                for pad in fp.pads
                if pad.copper_layers and pad.polygon and pad.drill is None
            ]

        issues = []
        for via in vias:
            where = {"net": via.net, "at": [via.x, via.y], "via_type": via.via_type}
            drill_rule, size_rule = (
                (micro_drill, micro_size) if via.via_type == "micro" else (min_drill, min_size)
            )
            if drill_rule is not None and via.drill < drill_rule - 1e-6:
                issues.append({**where, "issue": f"drill {via.drill}mm < {drill_rule}mm"})
            if size_rule is not None and via.size < size_rule - 1e-6:
                issues.append({**where, "issue": f"diameter {via.size}mm < {size_rule}mm"})
            if min_ring is not None and via.annular_ring < min_ring - 1e-6:
                issues.append(
                    {**where, "issue": f"annular ring {via.annular_ring:.3f}mm < {min_ring}mm"}
                )
            for name, pad in pads:
                if point_in_polygon(via.x, via.y, pad.polygon):
                    issues.append({**where, "issue": f"via in pad {name}"})
        rules_used = {
            "min_drill_mm": min_drill,
            "min_diameter_mm": min_size,
            "min_annular_ring_mm": min_ring,
            "min_microvia_drill_mm": micro_drill,
            "min_microvia_diameter_mm": micro_size,
        }
        if issues:
            return CheckResult.fail(
                self.id, f"{len(issues)} via issue(s)", issues=issues, rules=rules_used
            )
        return CheckResult.pass_(
            self.id,
            f"All {len(vias)} vias meet the rules",
            vias_checked=len(vias),
            rules=rules_used,
        )


@dataclass
class ThermalViasParams(CheckParams):
    reference: str
    pad: str | None = None
    """The pad to check; None picks the footprint's largest copper pad, which is
    its exposed thermal pad on a QFN, SON or DPAK."""
    min_vias: int = 1
    min_drill_mm: float | None = None


@register
class ThermalViasCheck(Check[ThermalViasParams]):
    """Enough vias sit inside a part's thermal pad to carry its heat away."""

    id = "pcb.thermal.vias"
    name = "Thermal Vias"
    description = "Checks the number of vias inside a part's thermal pad"
    category = CheckCategory.PCB
    Params: ClassVar[type[ThermalViasParams]] = ThermalViasParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        fp = geo.footprint(p.reference)
        if fp is None:
            return CheckResult.fail(self.id, f"Footprint '{p.reference}' not found on board")
        candidates = [pad for pad in fp.pads if pad.copper_layers and pad.polygon]
        if p.pad is not None:
            candidates = [pad for pad in candidates if pad.number == p.pad]
        if not candidates:
            return CheckResult.fail(self.id, f"No copper pad {p.pad or ''} on {p.reference}")
        pad = max(candidates, key=lambda c: _area(c.polygon))
        inside = [
            v
            for v in ctx.board.vias
            if point_in_polygon(v.x, v.y, pad.polygon)
            and (p.min_drill_mm is None or v.drill >= p.min_drill_mm - 1e-6)
        ]
        details = {
            "pad": pad.number,
            "pad_net": pad.net,
            "vias": len(inside),
            "via_nets": sorted({v.net for v in inside}),
        }
        if len(inside) < p.min_vias:
            return CheckResult.fail(
                self.id,
                f"{p.reference} pad {pad.number} has {len(inside)} via(s) < {p.min_vias}",
                **details,
            )
        return CheckResult.pass_(
            self.id, f"{p.reference} pad {pad.number} has {len(inside)} via(s)", **details
        )


def _area(polygon: tuple[tuple[float, float], ...]) -> float:
    n = len(polygon)
    return (
        abs(
            sum(
                polygon[i][0] * polygon[(i + 1) % n][1] - polygon[(i + 1) % n][0] * polygon[i][1]
                for i in range(n)
            )
        )
        / 2
    )
