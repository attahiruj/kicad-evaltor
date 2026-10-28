from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.pcb._common import DrcRun, _run_drc, board_drc, drc_findings
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.findings import (
    is_owned_by,
    unsuppressed_project,
    validate_severity_overrides,
)
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
    exclude_owned_by: list[str] | None = None
    """Ignore findings whose items all belong to these footprints (a pad too
    close to its own hole). A finding pairing one with the board edge, a track
    or another part still counts, and so does every unconnected item: an
    unrouted link is never a part's own finding, even when KiCad happens to
    report it between two pads of one footprint. See
    :func:`kicad_evaltor.findings.is_owned_by`."""
    reset_severities: bool = False
    """Check a temporary copy of the project whose DRC rule severities are
    KiCad's defaults, to see what the project's ignored rules hide. Each
    finding gains ``hidden_by``/``hidden_kind`` (the rule key or exclusion that
    hid it in the real project, else None) and ``project_severity``."""
    drop_exclusions: bool = False
    """Check a temporary copy of the project without its DRC exclusions,
    tagging findings as ``reset_severities`` does."""
    severity_overrides: dict[str, str] | None = None
    """Rule key to severity (``error``, ``warning``, ``ignore``) set in the
    temporary copy, after ``reset_severities``; e.g. ``{"pth_inside_courtyard":
    "error"}`` for a rule KiCad ignores by default."""

    def validate(self) -> None:
        if self.severity not in ("error", "warning", "all"):
            raise ValueError("severity must be 'error', 'warning', or 'all'")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")
        validate_severity_overrides(self.severity_overrides)

    @property
    def unsuppressed(self) -> bool:
        """Whether the check runs on a copy with silenced findings let through."""
        return self.reset_severities or self.drop_exclusions or bool(self.severity_overrides)


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
            failure = {**run.failure, "timed_out": run.timed_out} if run.failure else {}
            if run.timed_out:
                return CheckResult.error(self.id, run.error, **failure)
            # kicad-cli ran but left no report: that is a broken run, never a skip
            # that could let an unchecked board pass.
            if params.strict or run.ran:
                return CheckResult.error(self.id, run.error, **failure)
            return CheckResult.skip(self.id, run.error, **failure)

        entries = run.entries
        if params.include_types:
            entries = [e for e in entries if e.get("type") in params.include_types]
        if params.exclude_types:
            entries = [e for e in entries if e.get("type") not in params.exclude_types]
        extra: dict[str, Any] = {}
        if params.exclude_owned_by:
            owners = set(params.exclude_owned_by)
            kept = [e for e in entries if not is_owned_by(e, owners)]
            extra["owned_excluded"] = len(entries) - len(kept)
            entries = kept
        filtered = filter_violations_by_severity(entries, params.severity)
        kind = "" if params.severity == "all" else f"{params.severity} "
        if params.unsuppressed:
            extra["hidden_violations"] = sum(1 for e in filtered if e.get("hidden_by"))

        if not filtered:
            return CheckResult.pass_(
                self.id,
                f"DRC passed: no {kind}violations found",
                total_violations=len(run.entries),
                filtered_violations=0,
                **extra,
            )

        return CheckResult.fail(
            self.id,
            f"DRC found {len(filtered)} {kind}violation(s)",
            total_violations=len(run.entries),
            filtered_violations=len(filtered),
            violations=drc_findings(filtered),
            **extra,
        )

    def _report(self, ctx: DesignContext) -> DrcRun:
        params = self.params
        if params.unsuppressed:
            # A copy of the project, so never the context's cached report.
            board = Path(params.board_path or ctx.board_path)
            with unsuppressed_project(
                board,
                "drc",
                reset_severities=params.reset_severities,
                drop_exclusions=params.drop_exclusions,
                severity_overrides=params.severity_overrides,
            ) as (copy, suppressions):
                run = _run_drc(
                    ctx,
                    refill_zones=params.refill_zones,
                    parity=params.schematic_parity and copy.with_suffix(".kicad_sch").is_file(),
                    timeout=params.timeout,
                    board=copy,
                )
            if run.error is None:
                run.entries = suppressions.tag(run.entries)
            return run
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
