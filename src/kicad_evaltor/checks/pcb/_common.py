"""What the board checks share: one DRC run, the geometry, and net selection.

DRC is run once per context with ``--severity-all``, and every DRC-backed check
filters that one report, so a suite of ten board checks costs one kicad-cli run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_evaltor.checks.base import CheckResult
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pcbnew_bridge import BoardGeometry, GeometryUnavailable
from kicad_evaltor.utils.kicad_cli import (
    InvalidReport,
    parse_drc_report,
    report_workspace,
    violation_details,
)


@dataclass
class DrcRun:
    """One DRC report, or why there is none."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    ran: bool = False
    """kicad-cli ran and exited cleanly; an error after that (no report) means
    something is wrong with the run, not that the tool is missing."""

    def of_types(self, *types: str, source: str | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.entries
            if (not types or e.get("type") in types)
            and (source is None or e.get("source") == source)
        ]


def board_drc(
    ctx: DesignContext,
    *,
    refill_zones: bool = False,
    schematic_parity: bool = True,
    timeout: float | None = None,
) -> DrcRun:
    """The board's DRC report, computed once per context and settings.

    Schematic parity needs no path: kicad-cli finds the schematic beside the
    board by project name, and ``--schematic-parity`` is a flag that takes no
    value. So parity is only asked for when that schematic exists.

    Only a report is kept. A failed run is not, so a later check, perhaps with a
    longer timeout, runs DRC again rather than inheriting the failure.
    """
    parity = schematic_parity and parity_schematic(ctx) is not None
    key = ("drc", refill_zones, parity)
    run = ctx.memo(
        key, lambda: _run_drc(ctx, refill_zones=refill_zones, parity=parity, timeout=timeout)
    )
    if run.error is not None:
        ctx.forget(key)
    return run


def parity_schematic(ctx: DesignContext) -> Path | None:
    """The schematic kicad-cli compares the board with, or None if there is none.

    kicad-cli looks beside the board for a schematic of the same name, whatever
    schematic the context was given.
    """
    if not ctx.has_board():
        return None
    candidate = ctx.board_path.with_suffix(".kicad_sch")
    return candidate if candidate.is_file() else None


def _run_drc(
    ctx: DesignContext,
    *,
    refill_zones: bool,
    parity: bool,
    timeout: float | None,
    board: str | Path | None = None,
) -> DrcRun:
    # The subprocess runs in a scratch directory; input paths must be absolute.
    board_path = str(Path(board or ctx.board_path).resolve())
    with report_workspace("drc") as workdir:
        report = workdir / "report.json"
        args = ["pcb", "drc", "--format", "json", "--severity-all"]
        if parity:
            args.append("--schematic-parity")
        if refill_zones:
            args.append("--refill-zones")
        args.extend(["-o", str(report), board_path])
        result = ctx.run_kicad_cli(args, timeout=timeout)
        if result.timed_out:
            return DrcRun(
                error=f"DRC did not finish: {result.stderr}; raise the check's timeout "
                "or the context's cli_timeout",
                timed_out=True,
            )
        if not result.success:
            return DrcRun(error=f"DRC command failed: {result.stderr.strip() or result.stdout}")
        if not report.exists():
            return DrcRun(error=f"DRC report not written to {report}", ran=True)
        try:
            entries = parse_drc_report(report.read_text(encoding="utf-8", errors="replace"))
        except InvalidReport as exc:
            return DrcRun(error=str(exc), ran=True)
        return DrcRun(entries=entries, ran=True)


def drc_error(check_id: str, run: DrcRun) -> CheckResult:
    return CheckResult.error(check_id, run.error or "DRC failed", timed_out=run.timed_out)


def items_of(entry: dict[str, Any]) -> list[str]:
    """The descriptions of the items a DRC entry names, e.g. ``"Footprint U1"``."""
    return [str(i.get("description", "")) for i in entry.get("items", []) if isinstance(i, dict)]


def footprints_named(entry: dict[str, Any]) -> list[str]:
    """References of the footprints a DRC entry names."""
    refs = []
    for description in items_of(entry):
        if description.startswith("Footprint "):
            refs.append(description.split()[1])
        elif " of " in description:
            refs.append(description.rsplit(" of ", 1)[1].split()[0])
    return refs


def drc_findings(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [violation_details(e) for e in entries]


def geometry(ctx: DesignContext, check_id: str) -> tuple[BoardGeometry | None, CheckResult | None]:
    """The board's pcbnew geometry, or the error result to return instead."""
    try:
        return ctx.board_geometry(), None
    except GeometryUnavailable as exc:
        return None, CheckResult.error(check_id, str(exc), timed_out=exc.timed_out)
