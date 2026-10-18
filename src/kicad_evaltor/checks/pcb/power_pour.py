from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class PowerPourParams(CheckParams):
    net_name: str
    min_area_mm2: float | None = None
    """Least filled copper area, summed over the selected layers."""
    layer: str | None = None
    """Only count fill on this copper layer, e.g. ``"B.Cu"``."""
    require_connected: bool = True
    """The pour must actually connect to at least one pad of its net."""

    def validate(self) -> None:
        if not self.net_name:
            raise ValueError("net_name is required")
        if self.min_area_mm2 is not None and self.min_area_mm2 <= 0:
            raise ValueError("min_area_mm2 must be > 0")


@register
class PowerPourCheck(Check[PowerPourParams]):
    """A net has a copper pour that is filled, big enough, and connected.

    Zones are refilled by KiCad's own filler before measuring, so the area is
    what KiCad would fill now, not what was last saved. "Connected" is pcbnew's
    connectivity: the fill touches a pad of the net, directly or by thermal
    spokes.
    """

    id = "pcb.power.pour"
    name = "Power Pour"
    description = "Verifies that a net has a filled, connected copper pour"
    category = CheckCategory.PCB
    Params: ClassVar[type[PowerPourParams]] = PowerPourParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None

        zones = [z for z in geo.zones if z.net == p.net_name and not z.rule_area]
        if p.layer:
            zones = [z for z in zones if p.layer in z.layers]
        if not zones:
            where = f" on {p.layer}" if p.layer else ""
            return CheckResult.fail(
                self.id,
                f"No copper pour (zone) found for net '{p.net_name}'{where}",
                net_name=p.net_name,
            )

        area = sum(
            value
            for z in zones
            for layer, value in z.filled_area.items()
            if p.layer is None or layer == p.layer
        )
        pads = sorted({pad for z in zones for pad in z.connected_pads})
        details = {
            "net_name": p.net_name,
            "zone_count": len(zones),
            "area_mm2": round(area, 3),
            "connected_pads": len(pads),
        }
        if p.require_connected and not pads:
            return CheckResult.fail(
                self.id, f"Pour on '{p.net_name}' connects to no pad", **details
            )
        if p.min_area_mm2 is not None and area < p.min_area_mm2:
            return CheckResult.fail(
                self.id,
                f"Net '{p.net_name}' pour fills {area:.1f}mm² < min {p.min_area_mm2}mm²",
                **details,
            )
        return CheckResult.pass_(
            self.id,
            f"Net '{p.net_name}' has {len(zones)} pour(s), {area:.1f}mm² filled",
            **details,
        )
