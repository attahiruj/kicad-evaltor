from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import (
    filter_violations_by_severity,
    parse_erc_report,
    report_workspace,
    violation_details,
)


@dataclass
class ERCRunParams(CheckParams):
    severity: str = "error"
    schematic_path: str | None = None
    strict: bool = False
    timeout: float | None = None
    """Seconds kicad-cli may run; None uses the context's ``cli_timeout``."""

    def validate(self) -> None:
        if self.severity not in ("error", "warning", "all"):
            raise ValueError("severity must be 'error', 'warning', or 'all'")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("timeout must be positive")


@register
class ERCRunCheck(Check[ERCRunParams]):
    id = "sch.erc"
    name = "ERC Check"
    description = "Runs ERC via kicad-cli and checks the report"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[ERCRunParams]] = ERCRunParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        sch_path = params.schematic_path or str(ctx.schematic_path)

        with report_workspace("erc") as workdir:
            args = ["sch", "erc", "--format", "json", "-o", str(workdir / "report.json"), sch_path]
            result = ctx.run_kicad_cli(args, timeout=params.timeout)

            if result.timed_out:
                # A timeout says nothing about the design, so it is never a skip
                # that could pass a run unnoticed, nor a violation.
                return CheckResult.error(
                    self.id,
                    f"ERC did not finish: {result.stderr}; raise the check's timeout "
                    "or the context's cli_timeout",
                    timed_out=True,
                )
            if not result.success:
                if params.strict:
                    return CheckResult.error(self.id, f"ERC command failed: {result.stderr}")
                return CheckResult.skip(self.id, f"ERC command failed: {result.stderr}")

            # kicad-cli prints a human summary to stdout and writes the JSON
            # report to the -o path; parsing stdout would always find nothing.
            report = workdir / "report.json"
            if not report.exists():
                return CheckResult.error(self.id, f"ERC report not written to {report}")

            violations = parse_erc_report(report.read_text(encoding="utf-8", errors="replace"))

        filtered = filter_violations_by_severity(violations, params.severity)

        if not filtered:
            return CheckResult.pass_(
                self.id,
                f"ERC passed: no {params.severity} violations found",
                total_violations=len(violations),
                filtered_violations=0,
            )

        details = [violation_details(v) for v in filtered]

        return CheckResult.fail(
            self.id,
            f"ERC found {len(filtered)} {params.severity} violation(s)",
            total_violations=len(violations),
            filtered_violations=len(filtered),
            violations=details,
        )
