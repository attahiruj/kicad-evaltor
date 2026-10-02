from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.units import to_mm


@dataclass
class TraceLengthParams(CheckParams):
    reference: str
    max_length_mm: float
    min_length_mm: float | None = None

    def validate(self) -> None:
        if not self.reference:
            raise ValueError("reference is required")
        if self.max_length_mm <= 0:
            raise ValueError("max_length_mm must be > 0")
        if self.min_length_mm is not None and self.min_length_mm < 0:
            raise ValueError("min_length_mm must be >= 0")


@register
class TraceLengthCheck(Check[TraceLengthParams]):
    id = "pcb.trace.length"
    name = "Trace Length"
    description = "Checks that the trace length for a component's net is within limits"
    category = CheckCategory.PCB
    Params: ClassVar[type[TraceLengthParams]] = TraceLengthParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        board = ctx.board

        fp = board.get_footprint_by_reference(params.reference)
        if fp is None:
            return CheckResult.fail(
                self.id,
                f"Footprint '{params.reference}' not found on board",
                reference=params.reference,
            )

        net_codes = set()
        for pad in fp.pads:
            if pad.net:
                net_codes.add(pad.net)

        if not net_codes:
            return CheckResult.skip(
                self.id, f"Footprint '{params.reference}' has no connected nets"
            )

        total_length_nm = 0
        for net_code in net_codes:
            items = board.get_items_by_net(net_code)
            for item in items:
                if hasattr(item, "length"):
                    total_length_nm += item.length
                elif hasattr(item, "get_length"):
                    total_length_nm += item.get_length()

        total_length_mm = to_mm(total_length_nm)

        if params.min_length_mm is not None and total_length_mm < params.min_length_mm:
            return CheckResult.fail(
                self.id,
                f"Trace length {total_length_mm:.3f}mm < min {params.min_length_mm}mm for {params.reference}",
                reference=params.reference,
                length_mm=total_length_mm,
                min_length_mm=params.min_length_mm,
                max_length_mm=params.max_length_mm,
            )

        if total_length_mm > params.max_length_mm:
            return CheckResult.fail(
                self.id,
                f"Trace length {total_length_mm:.3f}mm > max {params.max_length_mm}mm for {params.reference}",
                reference=params.reference,
                length_mm=total_length_mm,
                min_length_mm=params.min_length_mm,
                max_length_mm=params.max_length_mm,
            )

        return CheckResult.pass_(
            self.id,
            f"Trace length {total_length_mm:.3f}mm within limits for {params.reference}",
            reference=params.reference,
            length_mm=total_length_mm,
            min_length_mm=params.min_length_mm,
            max_length_mm=params.max_length_mm,
        )
