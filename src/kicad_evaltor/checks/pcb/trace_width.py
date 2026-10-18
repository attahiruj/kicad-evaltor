from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.board_file import BoardTrack
from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext

# Track widths are stored to the nanometre; compare with a little slack so a
# 0.2 mm track is not "narrower" than a 0.2 mm rule through float rounding.
_EPSILON = 1e-6


@dataclass
class TraceWidthParams(CheckParams):
    min_width_mm: float | None = None
    max_width_mm: float | None = None
    net_name: str | None = None
    reference: str | None = None
    netclass: str | None = None
    """Select the nets in this net class (as KiCad assigns them, e.g. ``"VCC"``)."""
    match_netclass: bool = False
    """Require each track to be at least its net class's track width."""

    def validate(self) -> None:
        if self.min_width_mm is not None and self.min_width_mm <= 0:
            raise ValueError("min_width_mm must be > 0")
        if (
            self.max_width_mm is not None
            and self.min_width_mm is not None
            and self.max_width_mm < self.min_width_mm
        ):
            raise ValueError("max_width_mm must be >= min_width_mm")
        if self.min_width_mm is None and self.max_width_mm is None and not self.match_netclass:
            raise ValueError("Give min_width_mm, max_width_mm or match_netclass=True")
        if not (self.net_name or self.reference or self.netclass or self.match_netclass):
            raise ValueError("Select tracks with net_name, reference or netclass")


@register
class TraceWidthCheck(Check[TraceWidthParams]):
    """Track widths on the selected nets are within limits.

    Nets are chosen by name, by the footprint whose pads they reach, or by net
    class. With ``match_netclass`` each track is held to its own net class's
    width; net classes are resolved by pcbnew, so that needs KiCad's Python.
    """

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
        tracks = board.tracks
        netclass_of: dict[str, str] = {}
        if params.netclass or params.match_netclass:
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
            assert geo is not None
            netclass_of = geo.netclass_of

        if params.net_name:
            if params.net_name not in board.nets:
                return CheckResult.fail(
                    self.id,
                    f"Net '{params.net_name}' not found on board",
                    net_name=params.net_name,
                )
            tracks = [t for t in tracks if t.net == params.net_name]
        if params.reference:
            fp = board.footprint(params.reference)
            if fp is None:
                return CheckResult.fail(
                    self.id,
                    f"Footprint '{params.reference}' not found on board",
                    reference=params.reference,
                )
            tracks = [t for t in tracks if t.net in fp.nets]
        if params.netclass:
            wanted = params.netclass
            tracks = [t for t in tracks if wanted in netclass_of.get(t.net, "").split(",")]

        violations = []
        for track in tracks:
            low = params.min_width_mm
            if params.match_netclass:
                rule = board.net_classes.effective(netclass_of.get(track.net, "Default"))
                if rule.track_width is not None:
                    low = max(low or 0.0, rule.track_width)
            if low is not None and track.width < low - _EPSILON:
                violations.append(_finding(track, f"width {track.width:g}mm < min {low:g}mm"))
            if params.max_width_mm is not None and track.width > params.max_width_mm + _EPSILON:
                violations.append(
                    _finding(track, f"width {track.width:g}mm > max {params.max_width_mm:g}mm")
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


def _finding(track: BoardTrack, issue: str) -> dict[str, object]:
    return {
        "net": track.net,
        "layer": track.layer,
        "start": list(track.start),
        "end": list(track.end),
        "width_mm": track.width,
        "issue": issue,
    }
