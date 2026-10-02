from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.units import to_mm


@dataclass
class PowerPourParams(CheckParams):
    net_name: str
    min_area_mm2: float | None = None

    def validate(self) -> None:
        if not self.net_name:
            raise ValueError("net_name is required")
        if self.min_area_mm2 is not None and self.min_area_mm2 <= 0:
            raise ValueError("min_area_mm2 must be > 0")


@register
class PowerPourCheck(Check[PowerPourParams]):
    id = "pcb.power.pour"
    name = "Power Pour"
    description = "Verifies that a power net has a copper pour (zone) assigned"
    category = CheckCategory.PCB
    Params: ClassVar[type[PowerPourParams]] = PowerPourParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        zones = ctx.board.get_zones()

        matching_zones = [z for z in zones if z.net_name == params.net_name]

        if not matching_zones:
            return CheckResult.fail(
                self.id,
                f"No copper pour (zone) found for net '{params.net_name}'",
                net_name=params.net_name,
            )

        if params.min_area_mm2 is not None:
            for zone in matching_zones:
                try:
                    polygons = zone.get_filled_polygons()
                    if polygons:
                        max_area_nm2 = max(p.area for p in polygons)
                        max_area_mm2 = to_mm(max_area_nm2) ** 2
                        if max_area_mm2 >= params.min_area_mm2:
                            return CheckResult.pass_(
                                self.id,
                                f"Net '{params.net_name}' has pour with area {max_area_mm2:.2f}mm² "
                                f"(min {params.min_area_mm2}mm²)",
                                net_name=params.net_name,
                                area_mm2=max_area_mm2,
                            )
                except Exception:
                    continue

            return CheckResult.fail(
                self.id,
                f"Net '{params.net_name}' has pour but area < {params.min_area_mm2}mm²",
                net_name=params.net_name,
                min_area_mm2=params.min_area_mm2,
            )

        return CheckResult.pass_(
            self.id,
            f"Net '{params.net_name}' has {len(matching_zones)} copper pour(s)",
            net_name=params.net_name,
            zone_count=len(matching_zones),
        )
