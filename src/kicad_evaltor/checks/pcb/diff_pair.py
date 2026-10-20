from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import geometry
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.diff_pairs import DiffPair, find_diff_pairs, measure_pair

_EPSILON = 1e-6


@dataclass
class DiffPairParams(CheckParams):
    pairs: list[list[str]] | None = None
    """Explicit pairs, positive first: ``[["USB_D+", "USB_D-"]]``."""
    pattern: str | None = None
    """Or every pair found by name whose nets match this wildcard (``"*USB*"``).
    With neither, every pair on the board is checked."""
    suffixes: list[list[str]] | None = None
    """Polarity suffixes, positive first, e.g. ``[["_DP", "_DM"], ["_p", "_n"]]``.
    None uses KiCad's own rule: ``+``/``-`` and ``P``/``N``, ignoring trailing
    digits and underscores."""
    max_skew_mm: float | None = None
    """Largest length difference between the two halves."""
    gap_mm: float | None = None
    """Edge-to-edge gap where the pair runs coupled. None takes the net class's
    diff-pair gap (net classes come from pcbnew, so that needs KiCad's Python)."""
    gap_tolerance_mm: float = 0.05
    width_mm: float | None = None
    """Track width on both halves. None takes the net class's diff-pair width."""
    width_tolerance_mm: float = 0.0
    max_uncoupled_mm: float | None = None
    """Total sampled length per half that may run apart from the other half."""
    coupling_distance_mm: float | None = None
    """Edge gap beyond which the halves count as uncoupled. None means twice the
    expected gap, or 0.5 mm when no gap is known."""
    max_via_mismatch: int | None = None
    """Largest difference in via count between the halves."""
    include_vias: bool | None = None
    """Count via height in lengths; None follows the project's setting."""
    check_netclass: bool = True
    """Take gap and width from the net class when not given."""

    def validate(self) -> None:
        if self.pairs is not None:
            for pair in self.pairs:
                if len(pair) != 2 or pair[0] == pair[1]:
                    raise ValueError(f"A pair is two different nets: {pair!r}")
        if self.suffixes is not None:
            for suffix in self.suffixes:
                if len(suffix) != 2 or not suffix[0] or not suffix[1]:
                    raise ValueError(f"A suffix pair is two non-empty suffixes: {suffix!r}")
        for name in ("max_skew_mm", "gap_mm", "width_mm", "max_uncoupled_mm"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be >= 0")


@register
class DiffPairCheck(Check[DiffPairParams]):
    """Differential pairs are routed as pairs: matched, at the right gap and width.

    For each pair: both halves are routed, the skew is within ``max_skew_mm``,
    every track is the pair width, the coupled gap stays within tolerance of the
    pair gap, neither half runs uncoupled for longer than ``max_uncoupled_mm``,
    and the halves use the same number of vias. Lengths are counted as KiCad
    counts them. Gap and width default to the net class's diff-pair settings,
    the values KiCad's router uses.

    KiCad's DRC checks pairs too, but only with custom rules in a ``.kicad_dru``
    file; ``pcb.tuning.rules`` reports those findings.
    """

    id = "pcb.diff_pair"
    name = "Differential Pair"
    description = "Checks differential pairs for skew, gap, width and coupling"
    category = CheckCategory.PCB
    Params: ClassVar[type[DiffPairParams]] = DiffPairParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        p = self.params
        board = ctx.board
        suffixes = [(s[0], s[1]) for s in p.suffixes] if p.suffixes else None
        if p.pairs:
            missing = [n for pair in p.pairs for n in pair if n not in board.nets]
            if missing:
                return CheckResult.fail(self.id, f"Net(s) not on board: {', '.join(missing)}")
            pairs = [DiffPair(pair[0], pair[0], pair[1]) for pair in p.pairs]
        else:
            pairs = find_diff_pairs(board.nets, suffixes=suffixes, pattern=p.pattern)
            if not pairs:
                where = f" matching {p.pattern!r}" if p.pattern else ""
                return CheckResult.fail(self.id, f"No differential pairs found{where}")

        netclass_of: dict[str, str] = {}
        if p.check_netclass and (p.gap_mm is None or p.width_mm is None):
            geo, error = geometry(ctx, self.id)
            if error is not None:
                return error
            assert geo is not None
            netclass_of = geo.netclass_of

        reports = []
        failing = 0
        for pair in pairs:
            report, issues = self._check(ctx, pair, netclass_of)
            reports.append(report)
            failing += bool(issues)
        if failing:
            return CheckResult.fail(
                self.id, f"{failing} of {len(pairs)} differential pair(s) fail", pairs=reports
            )
        return CheckResult.pass_(
            self.id, f"All {len(pairs)} differential pair(s) pass", pairs=reports
        )

    def _check(
        self, ctx: DesignContext, pair: DiffPair, netclass_of: dict[str, str]
    ) -> tuple[dict[str, Any], list[str]]:
        p = self.params
        board = ctx.board
        rule = board.net_classes.effective(netclass_of.get(pair.positive, "Default"))
        gap = (
            p.gap_mm if p.gap_mm is not None else (rule.diff_pair_gap if p.check_netclass else None)
        )
        width = (
            p.width_mm
            if p.width_mm is not None
            else (rule.diff_pair_width if p.check_netclass else None)
        )
        coupling = p.coupling_distance_mm or (2 * gap if gap else 0.5)
        m = measure_pair(board, pair, coupling_distance=coupling, include_vias=p.include_vias)

        issues = []
        unrouted = [n for n in pair.nets if not board.tracks_on(n)]
        if unrouted:
            issues.append(f"{', '.join(unrouted)} not routed")
        if p.max_skew_mm is not None and m.skew > p.max_skew_mm + _EPSILON:
            issues.append(f"skew {m.skew:.3f}mm > {p.max_skew_mm}mm")
        if width is not None:
            wrong = sorted(
                {
                    w
                    for ws in m.widths.values()
                    for w in ws
                    if abs(w - width) > p.width_tolerance_mm + _EPSILON
                }
            )
            if wrong:
                issues.append(
                    f"track width {', '.join(f'{w:g}' for w in wrong)}mm, expected {width:g}mm"
                )
        if gap is not None and m.gap_min is not None and m.gap_max is not None:
            if m.gap_min < gap - p.gap_tolerance_mm - _EPSILON:
                issues.append(f"gap narrows to {m.gap_min:.3f}mm, expected {gap:g}mm")
            if m.gap_max > gap + p.gap_tolerance_mm + _EPSILON:
                issues.append(f"gap widens to {m.gap_max:.3f}mm, expected {gap:g}mm")
        if gap is not None and m.gap_min is None and not unrouted:
            issues.append("the halves never run coupled")
        if p.max_uncoupled_mm is not None:
            loose = {n: v for n, v in m.uncoupled_length.items() if v > p.max_uncoupled_mm}
            for net, value in loose.items():
                issues.append(f"{net} uncoupled for {value:.3f}mm > {p.max_uncoupled_mm}mm")
        if p.max_via_mismatch is not None:
            diff = abs(m.vias[pair.positive] - m.vias[pair.negative])
            if diff > p.max_via_mismatch:
                issues.append(f"via counts differ by {diff}")

        report = {
            "positive": pair.positive,
            "negative": pair.negative,
            "lengths_mm": {k: round(v, 4) for k, v in m.lengths.items()},
            "skew_mm": round(m.skew, 4),
            "widths_mm": m.widths,
            "gap_mm": {"min": m.gap_min, "max": m.gap_max, "expected": gap},
            "coupled_mm": m.coupled_length,
            "uncoupled_mm": m.uncoupled_length,
            "vias": m.vias,
            "issues": issues,
        }
        return report, issues
