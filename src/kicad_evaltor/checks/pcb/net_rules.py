"""Per-net routing rules read from the board file: vias, layers, length matching.

These need no KiCad: tracks and vias are plain values in ``.kicad_pcb``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext


@dataclass
class NetViasParams(CheckParams):
    net: str
    max_vias: int | None = None
    min_vias: int | None = None

    def validate(self) -> None:
        if not self.net:
            raise ValueError("net is required")
        if self.max_vias is None and self.min_vias is None:
            raise ValueError("Give max_vias, min_vias or both")


@register
class NetViasCheck(Check[NetViasParams]):
    id = "pcb.net.vias"
    name = "Net Via Count"
    description = "Checks how many vias a net uses"
    category = CheckCategory.PCB
    Params: ClassVar[type[NetViasParams]] = NetViasParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        params = self.params
        if params.net not in ctx.board.nets:
            return CheckResult.fail(self.id, f"Net '{params.net}' not found on board")
        count = len(ctx.board.vias_on(params.net))
        details = {"net": params.net, "vias": count}
        if params.max_vias is not None and count > params.max_vias:
            return CheckResult.fail(
                self.id, f"{params.net} has {count} vias > max {params.max_vias}", **details
            )
        if params.min_vias is not None and count < params.min_vias:
            return CheckResult.fail(
                self.id, f"{params.net} has {count} vias < min {params.min_vias}", **details
            )
        return CheckResult.pass_(self.id, f"{params.net} has {count} vias", **details)


@dataclass
class NetLayersParams(CheckParams):
    net: str
    allowed_layers: list[str] | None = None
    """Tracks on any other copper layer fail."""
    required_layers: list[str] | None = None
    """Each of these layers must carry some of the net's track."""

    def validate(self) -> None:
        if not self.net:
            raise ValueError("net is required")
        if not self.allowed_layers and not self.required_layers:
            raise ValueError("Give allowed_layers, required_layers or both")


@register
class NetLayersCheck(Check[NetLayersParams]):
    id = "pcb.net.layers"
    name = "Net Layers"
    description = "Checks which copper layers a net is routed on"
    category = CheckCategory.PCB
    Params: ClassVar[type[NetLayersParams]] = NetLayersParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        params = self.params
        if params.net not in ctx.board.nets:
            return CheckResult.fail(self.id, f"Net '{params.net}' not found on board")
        used = sorted({t.layer for t in ctx.board.tracks_on(params.net)})
        issues = []
        if params.allowed_layers:
            stray = [layer for layer in used if layer not in params.allowed_layers]
            if stray:
                issues.append(f"routed on {', '.join(stray)}")
        if params.required_layers:
            missing = [layer for layer in params.required_layers if layer not in used]
            if missing:
                issues.append(f"nothing routed on {', '.join(missing)}")
        details = {"net": params.net, "layers": used}
        if issues:
            return CheckResult.fail(self.id, f"{params.net}: " + "; ".join(issues), **details)
        return CheckResult.pass_(self.id, f"{params.net} routed on {', '.join(used)}", **details)


@dataclass
class LengthMatchParams(CheckParams):
    max_skew_mm: float | None = None
    """Largest difference between the longest and shortest net."""
    nets: list[str] | None = None
    """The nets to match: a differential pair is two, a bus is several."""
    pattern: str | None = None
    """Or every net whose name matches this wildcard, like ``"/SD_*"``."""
    target_length_mm: float | None = None
    """Every net must also be this long, within ``tolerance_mm``."""
    tolerance_mm: float | None = None
    include_vias: bool | None = None
    """Count via height; None follows the project's setting, as KiCad does."""

    def validate(self) -> None:
        if self.max_skew_mm is None and self.target_length_mm is None:
            raise ValueError("Give max_skew_mm, target_length_mm or both")
        if self.max_skew_mm is not None and self.max_skew_mm < 0:
            raise ValueError("max_skew_mm must be >= 0")
        if self.target_length_mm is not None:
            if self.target_length_mm <= 0:
                raise ValueError("target_length_mm must be > 0")
            if self.tolerance_mm is None or self.tolerance_mm < 0:
                raise ValueError("target_length_mm needs tolerance_mm >= 0")
        elif self.tolerance_mm is not None:
            raise ValueError("tolerance_mm goes with target_length_mm")
        if not self.nets and not self.pattern:
            raise ValueError("Give nets or pattern")
        if self.nets is not None and len(self.nets) < 2 and self.target_length_mm is None:
            raise ValueError("Length matching needs at least two nets")


@register
class LengthMatchCheck(Check[LengthMatchParams]):
    """A group of nets is length matched, and optionally tuned to a target length.

    A differential pair is the two-net case; ``pcb.diff_pair`` also checks its
    gap and coupling. Lengths are counted as KiCad's DRC and length tuning count
    them: tracks, plus via height when the project says so, plus pad-to-die.
    """

    id = "pcb.net.length_match"
    name = "Net Length Match"
    description = "Checks that a group of nets is length matched"
    category = CheckCategory.PCB
    Params: ClassVar[type[LengthMatchParams]] = LengthMatchParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        params = self.params
        board = ctx.board
        if params.nets:
            nets = list(params.nets)
            missing = [n for n in nets if n not in board.nets]
            if missing:
                return CheckResult.fail(self.id, f"Net(s) not on board: {', '.join(missing)}")
        else:
            assert params.pattern is not None
            nets = [n for n in board.nets if fnmatch.fnmatchcase(n, params.pattern)]
            need = 1 if params.target_length_mm is not None else 2
            if len(nets) < need:
                return CheckResult.fail(
                    self.id,
                    f"Pattern {params.pattern!r} matches {len(nets)} net(s); need {need}+",
                )
        lengths = {
            net: round(board.net_length(net, include_vias=params.include_vias), 4) for net in nets
        }
        skew = max(lengths.values()) - min(lengths.values())
        details: dict[str, object] = {
            "lengths_mm": lengths,
            "skew_mm": round(skew, 4),
            "longest": max(lengths, key=lengths.__getitem__),
            "shortest": min(lengths, key=lengths.__getitem__),
            # How far each net falls short of the longest: KiCad's skew figure.
            "skew_to_longest_mm": {
                net: round(length - max(lengths.values()), 4) for net, length in lengths.items()
            },
        }
        issues = []
        if params.max_skew_mm is not None and skew > params.max_skew_mm + 1e-6:
            issues.append(f"skew {skew:.3f}mm > max {params.max_skew_mm}mm")
        if params.target_length_mm is not None:
            tol = params.tolerance_mm or 0.0
            off = {
                net: length
                for net, length in lengths.items()
                if abs(length - params.target_length_mm) > tol + 1e-6
            }
            details["off_target"] = off
            if off:
                issues.append(
                    f"{len(off)} net(s) not {params.target_length_mm}±{tol}mm: "
                    + ", ".join(f"{n} {v:.3f}mm" for n, v in off.items())
                )
        if issues:
            return CheckResult.fail(self.id, "; ".join(issues), **details)
        return CheckResult.pass_(self.id, f"Skew {skew:.3f}mm within limit", **details)
