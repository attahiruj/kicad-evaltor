"""Opt-in composition of explicitly supplied checks and measurements."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult, TestStatus


class MeasurementCheck(Check[CheckParams]):
    """Evaluate exactly the measurement and acceptance predicate the user supplies."""

    Params = CheckParams
    category = CheckCategory.PROJECT
    name = "Caller measurement"
    description = "A caller-authored assertion"

    def __init__(
        self,
        check_id: str,
        measure: Callable,
        accept: Callable[[Any], bool],
        *,
        requirement_id: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(requirement_id=requirement_id)
        self.id = check_id
        self.measure = measure
        self.accept = accept
        self._recorded_parameters = dict(parameters or {})

    @property
    def params_dict(self) -> dict[str, Any]:
        from copy import deepcopy

        return deepcopy(self._recorded_parameters)

    def run(self, ctx) -> CheckResult:
        measured = self.measure(ctx)
        accepted = self.accept(measured)
        if not isinstance(accepted, bool):
            raise TypeError("Acceptance predicate must return bool")
        result = CheckResult.pass_ if accepted else CheckResult.fail
        return result(self.id, measured=measured)


class CompositeCheck(Check[CheckParams]):
    Params = CheckParams
    category = CheckCategory.PROJECT
    name = "Caller composition"
    description = "All or any of explicitly selected checks"

    def __init__(
        self,
        check_id: str,
        checks: Sequence[Check],
        *,
        mode: str,
        requirement_id: str | None = None,
    ) -> None:
        super().__init__(requirement_id=requirement_id)
        if mode not in ("all", "any") or not checks:
            raise ValueError("A composition needs nonempty checks and mode all or any")
        self.id, self.children, self.mode = check_id, tuple(checks), mode

    def run(self, ctx) -> CheckResult:
        from kicad_evaltor.core.runner import TestRunner

        report = TestRunner(list(self.children)).run(ctx)
        statuses = [r.status for r in report.results]
        details: dict[str, Any] = {
            "mode": self.mode,
            "children": [r.to_dict() for r in report.results],
        }
        if (self.mode == "all" and all(s is TestStatus.PASS for s in statuses)) or (
            self.mode == "any" and TestStatus.PASS in statuses
        ):
            return CheckResult.pass_(self.id, **details)
        if TestStatus.ERROR in statuses:
            return CheckResult.error(self.id, "A selected branch could not be evaluated", **details)
        if self.mode == "all" and TestStatus.FAIL in statuses:
            return CheckResult.fail(self.id, **details)
        if TestStatus.SKIP in statuses:
            return CheckResult.skip(self.id, "A selected branch was not evaluated", **details)
        return CheckResult.fail(self.id, **details)
