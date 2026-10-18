from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import DrcRun, _run_drc, board_drc, drc_findings
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import filter_violations_by_severity


@dataclass
class DRCRunParams(CheckParams):
    severity: str = "error"
    board_path: str | None = None
    strict: bool = False
    timeout: float | None = None
    """Seconds kicad-cli may run; None uses the context's ``cli_timeout``."""
    schematic_parity: bool = True
    refill_zones: bool = True
    """Refill zones before checking, as KiCad's DRC dialog does. The file is not
    rewritten."""
    include_types: list[str] | None = None
    """Only count findings of these DRC types (``clearance``, ``unconnected_items``...)."""
    exclude_types: list[str] | None = None
    """Ignore findings of these types, e.g. ``lib_footprint_issues`` for a board
    whose footprint libraries are not installed where it is checked."""

    def validate(self) -> None:
        if self.severity not in ("error", "warning", "all"):
            raise ValueError("severity must be 'error', 'warning', or 'all'")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")


@register
class DRCRunCheck(Check[DRCRunParams]):
    """KiCad's DRC: rule violations, unrouted connections and schematic parity.

    All three of the report's lists count. KiCad 10 keeps unrouted connections
    in ``unconnected_items`` and schematic differences in ``schematic_parity``,
    apart from ``violations``; each finding carries a ``source`` naming its list.
    """

    id = "pcb.drc"
    name = "DRC Check"
    description = "Runs DRC via kicad-cli and checks the report"
    category = CheckCategory.PCB
    Params: ClassVar[type[DRCRunParams]] = DRCRunParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        run = self._report(ctx)
        if run.error is not None:
            if run.timed_out:
                return CheckResult.error(self.id, run.error, timed_out=True)
            # kicad-cli ran but left no report: that is a broken run, never a skip
            # that could let an unchecked board pass.
            if params.strict or run.ran:
                return CheckResult.error(self.id, run.error)
            return CheckResult.skip(self.id, run.error)

        entries = run.entries
        if params.include_types:
            entries = [e for e in entries if e.get("type") in params.include_types]
        if params.exclude_types:
            entries = [e for e in entries if e.get("type") not in params.exclude_types]
        filtered = filter_violations_by_severity(entries, params.severity)
        kind = "" if params.severity == "all" else f"{params.severity} "

        if not filtered:
            return CheckResult.pass_(
                self.id,
                f"DRC passed: no {kind}violations found",
                total_violations=len(run.entries),
                filtered_violations=0,
            )

        return CheckResult.fail(
            self.id,
            f"DRC found {len(filtered)} {kind}violation(s)",
            total_violations=len(run.entries),
            filtered_violations=len(filtered),
            violations=drc_findings(filtered),
        )

    def _report(self, ctx: DesignContext) -> DrcRun:
        params = self.params
        if params.board_path:
            # Another board than the context's: run it on its own, uncached.
            return _run_drc(
                ctx,
                refill_zones=params.refill_zones,
                parity=params.schematic_parity
                and Path(params.board_path).with_suffix(".kicad_sch").is_file(),
                timeout=params.timeout,
                board=params.board_path,
            )
        return board_drc(
            ctx,
            refill_zones=params.refill_zones,
            schematic_parity=params.schematic_parity,
            timeout=params.timeout,
        )
