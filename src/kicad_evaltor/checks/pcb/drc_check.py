from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import (
    filter_violations_by_severity,
    parse_drc_report,
    report_workspace,
    violation_details,
)


@dataclass
class DRCRunParams(CheckParams):
    severity: str = "error"
    board_path: str | None = None
    strict: bool = False
    schematic_parity: bool = True

    def validate(self) -> None:
        if self.severity not in ("error", "warning", "all"):
            raise ValueError("severity must be 'error', 'warning', or 'all'")


@register
class DRCRunCheck(Check[DRCRunParams]):
    id = "pcb.drc"
    name = "DRC Check"
    description = "Runs DRC via kicad-cli and checks the report"
    category = CheckCategory.PCB
    Params: ClassVar[type[DRCRunParams]] = DRCRunParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")

        params = self.params
        pcb_path = params.board_path or str(ctx.board_path)

        with report_workspace("drc") as workdir:
            args = ["pcb", "drc", "--format", "json"]
            if params.schematic_parity and ctx.has_schematic():
                args.extend(["--schematic-parity", str(ctx.schematic_path)])
            args.extend(["-o", str(workdir / "report.json"), pcb_path])

            result = ctx.run_kicad_cli(args)

            if not result.success:
                if params.strict:
                    return CheckResult.error(self.id, f"DRC command failed: {result.stderr}")
                return CheckResult.skip(self.id, f"DRC command failed: {result.stderr}")

            # kicad-cli prints a human summary to stdout and writes the JSON
            # report to the -o path; parsing stdout would always find nothing.
            report = workdir / "report.json"
            if not report.exists():
                return CheckResult.error(self.id, f"DRC report not written to {report}")

            violations = parse_drc_report(report.read_text(encoding="utf-8", errors="replace"))

        filtered = filter_violations_by_severity(violations, params.severity)

        if not filtered:
            return CheckResult.pass_(
                self.id,
                f"DRC passed: no {params.severity} violations found",
                total_violations=len(violations),
                filtered_violations=0,
            )

        details = [violation_details(v) for v in filtered]

        return CheckResult.fail(
            self.id,
            f"DRC found {len(filtered)} {params.severity} violation(s)",
            total_violations=len(violations),
            filtered_violations=len(filtered),
            violations=details,
        )
