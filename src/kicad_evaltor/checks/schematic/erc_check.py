from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.findings import (
    ProjectSuppressions,
    is_owned_by,
    unsuppressed_project,
    validate_severity_overrides,
)
from kicad_evaltor.utils.kicad_cli import (
    InvalidReport,
    describe_failure,
    failure_details,
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
    exclude_owned_by: list[str] | None = None
    """Ignore findings whose items all belong to these symbols. A finding
    pairing one with another symbol (a ``pin_to_pin`` against a PWR_FLAG) still
    counts. See :func:`kicad_evaltor.findings.is_owned_by`."""
    reset_severities: bool = False
    """Check a temporary copy of the project whose ERC rule severities are
    KiCad's defaults, to see what the project's ignored rules hide. Each
    finding gains ``hidden_by``/``hidden_kind`` (the rule key or exclusion that
    hid it in the real project, else None) and ``project_severity``."""
    drop_exclusions: bool = False
    """Check a temporary copy of the project without its ERC exclusions,
    tagging findings as ``reset_severities`` does."""
    severity_overrides: dict[str, str] | None = None
    """Rule key to severity (``error``, ``warning``, ``ignore``) set in the
    temporary copy, after ``reset_severities``."""

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
        sch_path = Path(params.schematic_path or ctx.schematic_path).resolve()
        suppressions: ProjectSuppressions | None = None

        with ExitStack() as stack:
            if params.unsuppressed:
                sch_path, suppressions = stack.enter_context(
                    unsuppressed_project(
                        sch_path,
                        "erc",
                        reset_severities=params.reset_severities,
                        drop_exclusions=params.drop_exclusions,
                        severity_overrides=params.severity_overrides,
                    )
                )
            workdir = stack.enter_context(report_workspace("erc"))
            args = [
                "sch",
                "erc",
                "--format",
                "json",
                "-o",
                str(workdir / "report.json"),
                str(sch_path),
            ]
            result = ctx.run_kicad_cli(args, timeout=params.timeout)

            if result.timed_out:
                # A timeout says nothing about the design, so it is never a skip
                # that could pass a run unnoticed, nor a violation.
                return CheckResult.error(
                    self.id,
                    f"ERC did not finish: {result.stderr}; raise the check's timeout "
                    "or the context's cli_timeout",
                    **failure_details(result),
                )
            if not result.success:
                # A killed run prints nothing; name the exit status instead.
                message = f"ERC command failed: {describe_failure(result)}"
                if params.strict:
                    return CheckResult.error(self.id, message, **failure_details(result))
                return CheckResult.skip(self.id, message, **failure_details(result))

            # kicad-cli prints a human summary to stdout and writes the JSON
            # report to the -o path; parsing stdout would always find nothing.
            report = workdir / "report.json"
            if not report.exists():
                return CheckResult.error(self.id, f"ERC report not written to {report}")

            try:
                violations = parse_erc_report(report.read_text(encoding="utf-8", errors="replace"))
            except InvalidReport as exc:
                return CheckResult.error(self.id, str(exc))

        if suppressions is not None:
            violations = suppressions.tag(violations)
        entries = violations
        extra: dict[str, Any] = {}
        if params.exclude_owned_by:
            owners = set(params.exclude_owned_by)
            kept = [v for v in entries if not is_owned_by(v, owners)]
            extra["owned_excluded"] = len(entries) - len(kept)
            entries = kept
        filtered = filter_violations_by_severity(entries, params.severity)
        if suppressions is not None:
            extra["hidden_violations"] = sum(1 for v in filtered if v.get("hidden_by"))

        if not filtered:
            return CheckResult.pass_(
                self.id,
                f"ERC passed: no {params.severity} violations found",
                total_violations=len(violations),
                filtered_violations=0,
                **extra,
            )

        lib_ids = symbol_lib_ids(ctx)
        details = [violation_details(v, lib_ids) for v in filtered]

        return CheckResult.fail(
            self.id,
            f"ERC found {len(filtered)} {params.severity} violation(s)",
            total_violations=len(violations),
            filtered_violations=len(filtered),
            violations=details,
            **extra,
        )


def symbol_lib_ids(ctx: DesignContext) -> dict[str, str] | None:
    """Reference to library id for every symbol of the design, power symbols included.

    None when the schematic cannot be read; items then carry no ``lib_id``.
    """
    try:
        symbols = ctx.sheet_symbols()
    except Exception:
        return None
    found: dict[str, str] = {}
    for symbol in symbols:
        reference = getattr(symbol, "reference", None)
        lib_id = getattr(symbol, "lib_id", None)
        if reference and lib_id:
            found.setdefault(str(reference), str(lib_id))
    return found
