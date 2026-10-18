from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.board_connectivity import pad_to_pad_length
from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


def length_limits(
    min_length: float | None,
    max_length: float | None,
    target: float | None,
    tolerance: float | None,
) -> tuple[float | None, float | None]:
    """The (min, max) a length must fall in; a target and tolerance give both."""
    if target is not None:
        return target - (tolerance or 0.0), target + (tolerance or 0.0)
    return min_length, max_length


def validate_limits(
    min_length: float | None,
    max_length: float | None,
    target: float | None,
    tolerance: float | None,
) -> None:
    if target is not None:
        if min_length is not None or max_length is not None:
            raise ValueError("Give target_length_mm or min/max_length_mm, not both")
        if target <= 0:
            raise ValueError("target_length_mm must be > 0")
        if tolerance is None or tolerance < 0:
            raise ValueError("target_length_mm needs tolerance_mm >= 0")
        return
    if tolerance is not None:
        raise ValueError("tolerance_mm goes with target_length_mm")
    if max_length is None and min_length is None:
        raise ValueError("Give max_length_mm, min_length_mm, or target_length_mm")
    if max_length is not None and max_length <= 0:
        raise ValueError("max_length_mm must be > 0")
    if min_length is not None and min_length < 0:
        raise ValueError("min_length_mm must be >= 0")


@dataclass
class TraceLengthParams(CheckParams):
    max_length_mm: float | None = None
    min_length_mm: float | None = None
    target_length_mm: float | None = None
    """Or a length to hit, within ``tolerance_mm`` either way."""
    tolerance_mm: float | None = None
    net: str | None = None
    """Measure the whole net."""
    from_pad: str | None = None
    """Measure the shortest route from this pad, as ``"U1.3"`` ..."""
    to_pad: str | None = None
    """... to this one. Pad outlines come from pcbnew, so this needs KiCad's Python."""
    include_vias: bool | None = None
    """Count via height. None follows the project's "use via height in length"
    setting, as KiCad does."""
    reference: str | None = None
    """No longer supported: summing every net a footprint touches measured
    nothing useful. Use ``net`` or ``from_pad``/``to_pad``."""

    def validate(self) -> None:
        if self.reference is not None:
            raise ValueError(
                "reference is no longer supported; use net=... for a net's total length "
                "or from_pad='U1.3', to_pad='J1.1' for one route"
            )
        validate_limits(
            self.min_length_mm, self.max_length_mm, self.target_length_mm, self.tolerance_mm
        )
        if bool(self.from_pad) != bool(self.to_pad):
            raise ValueError("from_pad and to_pad go together")
        if not self.net and not self.from_pad:
            raise ValueError("Give net, or from_pad and to_pad")
        for pad in (self.from_pad, self.to_pad):
            if pad and "." not in pad:
                raise ValueError(f"pad {pad!r} must be written REF.NUMBER, like 'U1.3'")


@register
class TraceLengthCheck(Check[TraceLengthParams]):
    """Routed length within limits: a whole net, or one pad-to-pad route.

    Lengths are counted the way KiCad's DRC and length tuning count them: track
    length (arcs as arcs), plus via height when the project says so, plus pad-to-
    die lengths. On the example board every net matches KiCad's DRC to 0.0001 mm.
    A pad-to-pad length is the shortest route through the net's tracks and vias.
    """

    id = "pcb.trace.length"
    name = "Trace Length"
    description = "Checks that a net's routed length, or a pad-to-pad route, is within limits"
    category = CheckCategory.PCB
    Params: ClassVar[type[TraceLengthParams]] = TraceLengthParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        board = ctx.board
        if params.from_pad and params.to_pad:
            what = f"{params.from_pad} to {params.to_pad}"
            measured = self._pad_to_pad(ctx, params.from_pad, params.to_pad)
            if isinstance(measured, CheckResult):
                return measured
            length = measured
        else:
            assert params.net is not None
            what = f"net {params.net}"
            if params.net not in board.nets:
                return CheckResult.fail(self.id, f"Net '{params.net}' not found on board")
            length = board.net_length(params.net, include_vias=params.include_vias)

        low, high = length_limits(
            params.min_length_mm,
            params.max_length_mm,
            params.target_length_mm,
            params.tolerance_mm,
        )
        details = {
            "measured": what,
            "length_mm": round(length, 4),
            "min_length_mm": None if low is None else round(low, 6),
            "max_length_mm": None if high is None else round(high, 6),
            "via_height_counted": (
                board.uses_via_height if params.include_vias is None else params.include_vias
            ),
        }
        if low is not None and length < low:
            return CheckResult.fail(self.id, f"{what}: {length:.3f}mm < min {low:g}mm", **details)
        if high is not None and length > high:
            return CheckResult.fail(self.id, f"{what}: {length:.3f}mm > max {high:g}mm", **details)
        return CheckResult.pass_(self.id, f"{what}: {length:.3f}mm within limits", **details)

    def _pad_to_pad(self, ctx: DesignContext, start: str, end: str) -> float | CheckResult:
        geo, error = geometry(ctx, self.id)
        if error is not None:
            return error
        assert geo is not None
        pads = []
        dies = []
        for name in (start, end):
            ref, number = name.split(".", 1)
            fp = geo.footprint(ref)
            group = fp.pads_numbered(number) if fp else []
            if not group:
                return CheckResult.fail(self.id, f"Pad {name} not found on board")
            pads.append(group)
            board_fp = ctx.board.footprint(ref)
            board_pad = board_fp.pad(number) if board_fp else None
            dies.append(board_pad.die_length if board_pad else 0.0)
        first, second = pads[0][0].net, pads[1][0].net
        if first != second or not first:
            return CheckResult.fail(
                self.id,
                f"{start} ({first or 'no net'}) and {end} ({second or 'no net'}) "
                "are not on the same net",
            )
        length = pad_to_pad_length(
            ctx.board,
            geo,
            first,
            pads[0],
            pads[1],
            include_vias=self.params.include_vias,
            die_lengths=(dies[0], dies[1]),
        )
        if length is None:
            return CheckResult.fail(self.id, f"No routed track joins {start} to {end}", net=first)
        return length
