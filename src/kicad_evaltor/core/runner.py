from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from kicad_evaltor.checks.base import Check, CheckCategory, CheckResult, TestStatus
from kicad_evaltor.core.context import DesignContext


@dataclass
class TestReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(r.is_pass for r in self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.PASS)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.FAIL)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.ERROR)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.SKIP)

    def by_category(self) -> dict[CheckCategory, list[CheckResult]]:
        categories: dict[CheckCategory, list[CheckResult]] = {}
        for result in self.results:
            check = next((c for c in self._checks if c.id == result.check_id), None)
            if check:
                cat = check.category
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(result)
        return categories

    def not_passed(self) -> list[CheckResult]:
        """Failures, errors and skips; unlike failures(), includes unchecked work."""
        return [r for r in self.results if not r.is_pass]

    @property
    def evaluated_count(self) -> int:
        return self.passed_count + self.failed_count

    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.is_fail]

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "total": len(self.results),
                "evaluated": self.evaluated_count,
                "passed": self.passed_count,
                "failed": self.failed_count,
                "errors": self.error_count,
                "skipped": self.skipped_count,
                "all_passed": self.all_passed,
            },
            "results": [result.to_dict() for result in self.results],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        import json

        return json.dumps(self.to_dict(), indent=indent)

    def failures_json(self, *, indent: int | None = 2) -> str:
        """JSON for just the failures; a clean run yields ``[]``."""
        import json

        return json.dumps([result.to_dict() for result in self.failures()], indent=indent)

    def summary(self) -> str:
        lines = [
            f"Test Report: {len(self.results)} checks",
            f"  Passed:  {self.passed_count}",
            f"  Failed:  {self.failed_count}",
            f"  Errors:  {self.error_count}",
            f"  Skipped: {self.skipped_count}",
            "",
        ]
        for result in self.results:
            status_icon = {
                TestStatus.PASS: "✓",
                TestStatus.FAIL: "✗",
                TestStatus.ERROR: "✗",
                TestStatus.SKIP: "○",
            }.get(result.status, "?")
            lines.append(
                f"  {status_icon} {result.check_id}: {result.message or result.status.value}"
            )
        return "\n".join(lines)

    _checks: list[Check] = field(default_factory=list, repr=False)


class TestRunner:
    def __init__(self, checks: list[Check] | None = None) -> None:
        self._checks: list[Check] = checks or []

    def add(self, check: Check) -> TestRunner:
        self._checks.append(check)
        return self

    def add_by_id(self, check_id: str, **params: Any) -> TestRunner:
        from kicad_evaltor.checks.registry import CheckRegistry

        check = CheckRegistry.create(check_id, **params)
        self._checks.append(check)
        return self

    @property
    def checks(self) -> list[Check]:
        return self._checks

    def run(self, ctx: DesignContext) -> TestReport:
        results: list[CheckResult] = []
        ids = [check.requirement_id or f"{check.id}[{i}]" for i, check in enumerate(self._checks)]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate requirement_id in suite")
        for check, requirement_id in zip(self._checks, ids):
            start = time.perf_counter()
            parameters = check.params_dict
            try:
                result = check.run(ctx)
                if not isinstance(result, CheckResult):
                    raise TypeError("Check.run must return CheckResult")  # noqa: TRY301
            except Exception as e:
                result = CheckResult.error(check.id, f"Check raised exception: {e}")
            result = _detached(result)
            result.duration = time.perf_counter() - start
            result.requirement_id = requirement_id
            result.parameters = parameters
            results.append(result)

        report = TestReport(results=results)
        report._checks = list(self._checks)
        return report


def _detached(result: CheckResult) -> CheckResult:
    """A copy of the result the check cannot change afterwards.

    A deep copy where possible. Details holding something that cannot be copied
    (a lock, an open file) keep a shallow copy instead: how a check reports its
    evidence must never turn its verdict into an error.
    """
    try:
        return deepcopy(result)
    except Exception:
        return replace(result, details=dict(result.details), parameters=dict(result.parameters))
