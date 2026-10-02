from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils import to_mm


@dataclass
class TraceWidthParams(CheckParams):
    min_width_mm: float
    max_width_mm: float | None = None
    net_name: str | None = None
    reference: str | None = None

    def validate(self) -> None:
        if self.min_width_mm <= 0:
            raise ValueError("min_width_mm must be > 0")
        if self.max_width_mm is not None and self.max_width_mm < self.min_width_mm:
            raise ValueError("max_width_mm must be >= min_width_mm")
        if not self.net_name and not self.reference:
            raise ValueError("At least one of net_name or reference must be provided")


@register
class TraceWidthCheck(Check[TraceWidthParams]):
    id = "pcb.trace.width"
    name = "Trace Width"
    description = "Checks that all traces on a specific net meet width constraints"
    category = CheckCategory.PCB
    Params: ClassVar[type[TraceWidthParams]] = TraceWidthParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        board = ctx.board

        tracks = board.get_tracks()

        if params.net_name:
            net = board.get_net_by_name(params.net_name)
            if net is None:
                return CheckResult.fail(
                    self.id,
                    f"Net '{params.net_name}' not found on board",
                    net_name=params.net_name,
                )
            net_code = net.net_code
            tracks = [t for t in tracks if t.net == net_code]
        elif params.reference:
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
            tracks = [t for t in tracks if t.net in net_codes]

        violations = []
        for track in tracks:
            width_mm = to_mm(track.width)
            if width_mm < params.min_width_mm:
                violations.append(
                    {
                        "track": track,
                        "width_mm": width_mm,
                        "issue": f"width {width_mm:.3f}mm < min {params.min_width_mm}mm",
                    }
                )
            if params.max_width_mm is not None and width_mm > params.max_width_mm:
                violations.append(
                    {
                        "track": track,
                        "width_mm": width_mm,
                        "issue": f"width {width_mm:.3f}mm > max {params.max_width_mm}mm",
                    }
                )

        if not violations:
            return CheckResult.pass_(
                self.id,
                f"All {len(tracks)} trace(s) meet width constraints",
                tracks_checked=len(tracks),
            )

        return CheckResult.fail(
            self.id,
            f"{len(violations)} trace(s) violate width constraints out of {len(tracks)} checked",
            violations=violations,
            tracks_checked=len(tracks),
        )
