"""Tests for TestRunner, TestReport and the Check base contract."""

from dataclasses import dataclass

import pytest

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult, TestStatus
from kicad_evaltor.checks.registry import CheckRegistry, register
from kicad_evaltor.core.runner import TestReport, TestRunner


@dataclass
class FixedParams(CheckParams):
    pass


def make_check(check_id: str, category: CheckCategory, result=None, raises=None) -> Check:
    class _Check(Check):
        Params = FixedParams

        def run(self, ctx):
            if raises is not None:
                raise raises
            return result

    # Assigned after class creation: a class body cannot close over an
    # enclosing local that shares the attribute name.
    _Check.id = check_id
    _Check.name = check_id
    _Check.description = check_id
    _Check.category = category
    return _Check()


def passing(check_id="a.b", category=CheckCategory.SCHEMATIC) -> Check:
    return make_check(check_id, category, CheckResult.pass_(check_id, "ok"))


def failing(check_id="a.b", category=CheckCategory.SCHEMATIC) -> Check:
    return make_check(check_id, category, CheckResult.fail(check_id, "nope"))


class TestCheckParamsLifecycle:
    def test_base_params_skip_validation_on_construction(self):
        # CheckParams is declared with init=False, so __post_init__ never runs
        # for the base class itself; subclasses generate their own __init__.
        assert CheckParams() is not None

    def test_subclass_validates_on_construction(self):
        @dataclass
        class Strict(CheckParams):
            value: int = 0

            def validate(self):
                if self.value < 0:
                    raise ValueError("value must be >= 0")

        with pytest.raises(ValueError, match="value must be >= 0"):
            Strict(value=-1)

    def test_check_construction_validates_params(self):
        from kicad_evaltor.checks.schematic.component_exists import ComponentExistsCheck

        with pytest.raises(ValueError, match="At least one of"):
            ComponentExistsCheck()


class TestCheckAccessors:
    def test_params_property(self):
        check = make_check("x.y", CheckCategory.PCB)
        assert isinstance(check.params, FixedParams)

    def test_params_dict_exposes_fields(self):
        check = make_check("x.y", CheckCategory.PCB)
        assert check.params_dict == {}

    def test_params_dict_with_values(self):
        from kicad_evaltor.checks.pcb.trace_width import TraceWidthCheck

        check = TraceWidthCheck(min_width_mm=0.25, net_name="VCC")
        assert check.params_dict["min_width_mm"] == 0.25
        assert check.params_dict["net_name"] == "VCC"


class TestCheckResultProperties:
    def test_error_counts_as_failure(self):
        assert CheckResult.error("a", "boom").is_fail

    def test_skip_is_neither_pass_nor_fail(self):
        result = CheckResult.skip("a", "why")
        assert not result.is_pass
        assert not result.is_fail
        assert result.status == TestStatus.SKIP

    def test_details_are_captured_from_kwargs(self):
        result = CheckResult.pass_("a", "ok", count=3, refs=["R1"])
        assert result.details == {"count": 3, "refs": ["R1"]}

    def test_error_requires_message(self):
        with pytest.raises(TypeError):
            CheckResult.error("a")


class TestTestReportCounts:
    def test_empty_report_is_all_passed(self):
        report = TestReport()
        assert report.all_passed
        assert report.passed_count == 0
        assert report.failed_count == 0
        assert report.error_count == 0
        assert report.skipped_count == 0

    def test_empty_report_has_no_failures(self):
        assert TestReport().failures() == []

    def test_all_passed_false_when_a_check_fails(self):
        report = TestReport([CheckResult.pass_("a"), CheckResult.fail("b")])
        assert not report.all_passed

    def test_all_passed_false_when_a_check_skips(self):
        report = TestReport([CheckResult.pass_("a"), CheckResult.skip("b", "no sch")])
        assert not report.all_passed

    def test_counts_are_mutually_exclusive(self):
        report = TestReport(
            [
                CheckResult.pass_("p"),
                CheckResult.fail("f"),
                CheckResult.error("e", "boom"),
                CheckResult.skip("s", "nope"),
            ]
        )
        assert report.passed_count == 1
        assert report.failed_count == 1
        assert report.error_count == 1
        assert report.skipped_count == 1

    def test_failures_include_fail_and_error_only(self):
        failures = TestReport(
            [
                CheckResult.pass_("p"),
                CheckResult.fail("f"),
                CheckResult.error("e", "boom"),
                CheckResult.skip("s", "nope"),
            ]
        ).failures()
        assert {r.check_id for r in failures} == {"f", "e"}


class TestTestReportByCategory:
    def test_groups_results_by_check_category(self):
        sch = passing("sch.a", CheckCategory.SCHEMATIC)
        pcb = passing("pcb.b", CheckCategory.PCB)
        report = TestReport([CheckResult.pass_("sch.a"), CheckResult.pass_("pcb.b")])
        report._checks = [sch, pcb]

        grouped = report.by_category()
        assert set(grouped) == {CheckCategory.SCHEMATIC, CheckCategory.PCB}
        assert len(grouped[CheckCategory.SCHEMATIC]) == 1

    def test_unknown_check_ids_are_omitted(self):
        report = TestReport([CheckResult.pass_("orphan")])
        report._checks = []
        assert report.by_category() == {}

    def test_categories_preserve_multiple_results(self):
        checks = [
            passing("sch.a", CheckCategory.SCHEMATIC),
            failing("sch.b", CheckCategory.SCHEMATIC),
        ]
        report = TestReport([CheckResult.pass_("sch.a"), CheckResult.fail("sch.b")])
        report._checks = checks
        assert len(report.by_category()[CheckCategory.SCHEMATIC]) == 2


class TestTestReportSerialization:
    def test_to_dict_summary(self):
        report = TestReport([CheckResult.pass_("a"), CheckResult.error("b", "boom")])
        data = report.to_dict()
        assert data["summary"] == {
            "total": 2,
            "passed": 1,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "all_passed": False,
        }

    def test_to_dict_results_shape(self):
        report = TestReport([CheckResult.pass_("a", "ok", count=1)])
        entry = report.to_dict()["results"][0]
        assert entry == {
            "check_id": "a",
            "status": "pass",
            "message": "ok",
            "details": {"count": 1},
            "duration": 0.0,
        }

    def test_to_json_round_trips(self):
        import json

        report = TestReport([CheckResult.pass_("a", "ok")])
        assert json.loads(report.to_json()) == report.to_dict()

    def test_to_json_indent_is_configurable(self):
        import json

        report = TestReport([CheckResult.pass_("a", "ok")])
        assert json.loads(report.to_json(indent=None)) == report.to_dict()
        assert "\n" not in report.to_json(indent=None)

    def test_result_serialises_on_its_own(self):
        result = CheckResult.fail("b", "broke", count=2)
        assert result.to_dict() == {
            "check_id": "b",
            "status": "fail",
            "message": "broke",
            "details": {"count": 2},
            "duration": 0.0,
        }

    def test_failures_json_carries_only_the_failures(self):
        import json

        report = TestReport(
            [
                CheckResult.pass_("a", "ok"),
                CheckResult.fail("b", "broke", count=2),
                CheckResult.error("c", "crashed"),
                CheckResult.skip("d", "nope"),
            ]
        )
        payload = json.loads(report.failures_json())
        assert [entry["check_id"] for entry in payload] == ["b", "c"]
        assert payload[0]["details"] == {"count": 2}

    def test_failures_json_of_a_clean_run_is_an_empty_list(self):
        import json

        report = TestReport([CheckResult.pass_("a", "ok")])
        assert json.loads(report.failures_json()) == []

    def test_summary_uses_status_icon_per_status(self):
        report = TestReport(
            [
                CheckResult.pass_("a", "all good"),
                CheckResult.fail("b", "broke"),
                CheckResult.error("c", "crashed"),
                CheckResult.skip("d", "skipped"),
            ]
        )
        text = report.summary()
        assert "✓ a: all good" in text
        assert "✗ b: broke" in text
        assert "✗ c: crashed" in text
        assert "○ d: skipped" in text

    def test_summary_falls_back_to_status_value_when_message_empty(self):
        assert "✓ a: pass" in TestReport([CheckResult.pass_("a")]).summary()

    def test_summary_reports_header_counts(self):
        text = TestReport([CheckResult.pass_("a")]).summary()
        assert "Test Report: 1 checks" in text
        assert "Passed:  1" in text


class TestTestRunner:
    def test_starts_with_no_checks(self):
        assert TestRunner().checks == []

    def test_accepts_checks_in_constructor(self):
        check = passing()
        assert TestRunner([check]).checks == [check]

    def test_add_returns_self_for_chaining(self):
        runner = TestRunner()
        assert runner.add(passing()) is runner
        assert len(runner.checks) == 1

    def test_add_by_id_resolves_through_registry(self):
        @register(id="custom.runner.check")
        class _Check(Check):
            id = "custom.runner.check"
            name = "Custom"
            description = "Custom"
            category = CheckCategory.PROJECT
            Params = FixedParams

            def run(self, ctx):
                return CheckResult.pass_(self.id)

        runner = TestRunner().add_by_id("custom.runner.check")
        assert runner.checks[0].id == "custom.runner.check"

    def test_add_by_id_unknown_check_raises(self):
        with pytest.raises(KeyError):
            TestRunner().add_by_id("does.not.exist")

    def test_run_collects_results_in_order(self):
        report = TestRunner(
            [
                make_check("first", CheckCategory.SCHEMATIC, CheckResult.pass_("first")),
                make_check("second", CheckCategory.SCHEMATIC, CheckResult.fail("second")),
            ]
        ).run(None)
        assert [r.check_id for r in report.results] == ["first", "second"]

    def test_run_converts_raised_exception_into_error_result(self):
        check = make_check("boom", CheckCategory.SCHEMATIC, raises=RuntimeError("kaboom"))
        report = TestRunner([check]).run(None)

        assert report.error_count == 1
        assert "kaboom" in report.results[0].message
        assert report.results[0].status == TestStatus.ERROR

    def test_run_keeps_checking_after_a_check_raises(self):
        report = TestRunner(
            [
                make_check("boom", CheckCategory.SCHEMATIC, raises=ValueError("bad")),
                make_check("after", CheckCategory.SCHEMATIC, CheckResult.pass_("after")),
            ]
        ).run(None)
        assert len(report.results) == 2
        assert report.results[1].is_pass

    def test_run_records_nonzero_duration(self):
        report = TestRunner([passing()]).run(None)
        assert report.results[0].duration > 0

    def test_run_attaches_checks_for_category_grouping(self):
        report = TestRunner([passing("sch.a", CheckCategory.SCHEMATIC)]).run(None)
        assert report.by_category() == {CheckCategory.SCHEMATIC: report.results}

    def test_run_on_empty_runner_produces_empty_report(self):
        report = TestRunner().run(None)
        assert report.results == []
        assert report.all_passed


class TestCheckRegistryIntegration:
    def test_create_builds_a_runnable_check(self):
        check = CheckRegistry.create("sch.component.value", reference="R1", expected="10k")
        assert check.id == "sch.component.value"
        assert check.params.expected == "10k"

    def test_all_shipped_checks_are_registered(self):
        expected = {
            "sch.component.exists",
            "sch.component.value",
            "sch.components.connected",
            "sch.component.property",
            "sch.footprint.assigned",
            "sch.erc",
            "sch.symbol.in_library",
            "pcb.footprint.exists",
            "pcb.footprint.overlap",
            "pcb.trace.width",
            "pcb.trace.length",
            "pcb.drc",
            "pcb.power.pour",
            "project.schematic_pcb_consistency",
        }
        assert expected <= {check_id for check_id, _ in CheckRegistry.list()}

    def test_registered_ids_match_class_ids(self):
        for check_id, check_class in CheckRegistry.list():
            assert check_id == check_class.id
