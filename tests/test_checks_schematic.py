"""Unit tests for schematic check run() logic, driven by fake kipy objects."""

from pathlib import Path

from conftest import (
    FakeContext,
    FakeNetlistNet,
    FakeNetNode,
    FakeSchematic,
    FakeSymbol,
    cli_result,
    erc_report,
    violation,
)
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.schematic.component_exists import ComponentExistsCheck
from kicad_evaltor.checks.schematic.component_property import ComponentPropertyCheck
from kicad_evaltor.checks.schematic.component_value import ComponentValueCheck
from kicad_evaltor.checks.schematic.components_connected import ComponentsConnectedCheck
from kicad_evaltor.checks.schematic.consistency import ConsistencyCheck
from kicad_evaltor.checks.schematic.erc_check import ERCRunCheck
from kicad_evaltor.checks.schematic.footprint_assigned import FootprintAssignedCheck
from kicad_evaltor.checks.schematic.symbol_in_library import SymbolInLibraryCheck
from kicad_evaltor.core.runner import TestRunner


def ctx_with(symbols=None, nets=None, cli=None) -> FakeContext:
    return FakeContext(
        schematic=FakeSchematic(symbols=symbols or [], nets=nets or []),
        cli_results=cli,
    )


def run(check, ctx):
    return check.run(ctx)


class TestComponentExistsCheck:
    def test_skips_without_schematic(self):
        result = run(ComponentExistsCheck(reference="R1"), FakeContext())
        assert result.status == TestStatus.SKIP
        assert "No schematic" in result.message

    def test_matches_single_reference(self):
        ctx = ctx_with([FakeSymbol("R1"), FakeSymbol("R2")])
        result = run(ComponentExistsCheck(reference="R1"), ctx)

        assert result.is_pass
        assert result.details["references"] == ["R1"]
        assert result.details["count"] == 1

    def test_fails_on_unknown_reference(self):
        result = run(ComponentExistsCheck(reference="R9"), ctx_with([FakeSymbol("R1")]))

        assert result.is_fail
        assert "reference=R9" in result.message

    def test_reference_match_is_exact_not_prefix(self):
        assert run(ComponentExistsCheck(reference="R"), ctx_with([FakeSymbol("R1")])).is_fail

    def test_matches_lib_id(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R"), FakeSymbol("C1", "Device:C")])
        result = run(ComponentExistsCheck(lib_id="Device:C"), ctx)

        assert result.is_pass
        assert result.details["references"] == ["C1"]

    def test_lib_id_pattern_matches_multiple(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R"), FakeSymbol("C1", "Device:C")])
        result = run(ComponentExistsCheck(lib_id_pattern="Device:*"), ctx)

        assert result.is_pass
        assert result.details["count"] == 2

    def test_lib_id_pattern_excludes_non_matching_library(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R")])
        assert run(ComponentExistsCheck(lib_id_pattern="Connector:*"), ctx).is_fail

    def test_value_match_is_case_insensitive(self):
        ctx = ctx_with([FakeSymbol("R1", value="10K")])
        assert run(ComponentExistsCheck(value="10k"), ctx).is_pass

    def test_criteria_are_combined_with_and(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R", "10k")])
        assert run(
            ComponentExistsCheck(reference="R1", lib_id="Device:C"),
            ctx,
        ).is_fail

    def test_criteria_are_combined_with_and_when_all_match(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R", "10k")])
        assert run(
            ComponentExistsCheck(reference="R1", lib_id="Device:R", value="10k"),
            ctx,
        ).is_pass

    def test_fail_lists_every_criterion(self):
        ctx = ctx_with([FakeSymbol("R1", "Device:R")])
        result = run(
            ComponentExistsCheck(reference="R9", lib_id="Device:C", lib_id_pattern="X:*"),
            ctx,
        )
        assert result.details["criteria"] == [
            "reference=R9",
            "lib_id=Device:C",
            "lib_id_pattern=X:*",
        ]

    def test_empty_schematic_fails(self):
        assert run(ComponentExistsCheck(reference="R1"), ctx_with([])).is_fail


class TestComponentValueCheck:
    def test_skips_without_schematic(self):
        assert (
            run(
                ComponentValueCheck(reference="R1", expected="10k"),
                FakeContext(),
            ).status
            == TestStatus.SKIP
        )

    def test_passes_on_case_insensitive_match(self):
        ctx = ctx_with([FakeSymbol("R1", value="10K")])
        result = run(ComponentValueCheck(reference="R1", expected="10k"), ctx)

        assert result.is_pass
        assert result.details["actual"] == "10K"

    def test_case_sensitive_rejects_differing_case(self):
        ctx = ctx_with([FakeSymbol("R1", value="10K")])
        result = run(
            ComponentValueCheck(reference="R1", expected="10k", case_sensitive=True),
            ctx,
        )
        assert result.is_fail
        assert "10K" in result.message

    def test_fails_on_value_mismatch(self):
        ctx = ctx_with([FakeSymbol("R1", value="1k")])
        result = run(ComponentValueCheck(reference="R1", expected="10k"), ctx)

        assert result.is_fail
        assert result.details["actual"] == "1k"
        assert result.details["expected"] == "10k"

    def test_fails_when_component_missing(self):
        result = run(
            ComponentValueCheck(reference="R9", expected="10k"), ctx_with([FakeSymbol("R1")])
        )
        assert result.is_fail
        assert "not found" in result.message
        assert result.details == {"reference": "R9"}

    def test_uses_first_matching_reference(self):
        ctx = ctx_with([FakeSymbol("R1", value="10k"), FakeSymbol("R1", value="1k")])
        result = run(ComponentValueCheck(reference="R1", expected="10k"), ctx)
        assert result.is_pass


class TestComponentsConnectedCheck:
    def test_skips_without_schematic(self):
        check = ComponentsConnectedCheck(reference_a="R1", reference_b="C1")
        assert run(check, FakeContext()).status == TestStatus.SKIP

    def test_passes_when_shared_on_one_net(self):
        ctx = ctx_with(nets=[FakeNetlistNet("NET1", [FakeNetNode("R1"), FakeNetNode("C1")])])
        result = run(ComponentsConnectedCheck(reference_a="R1", reference_b="C1"), ctx)

        assert result.is_pass
        assert result.details["shared_nets"] == ["NET1"]

    def test_fails_when_on_separate_nets(self):
        ctx = ctx_with(
            nets=[
                FakeNetlistNet("NET1", [FakeNetNode("R1")]),
                FakeNetlistNet("NET2", [FakeNetNode("C1")]),
            ]
        )
        result = run(ComponentsConnectedCheck(reference_a="R1", reference_b="C1"), ctx)

        assert result.is_fail
        assert result.details["count"] == 0

    def test_counts_multiple_shared_nets(self):
        ctx = ctx_with(
            nets=[
                FakeNetlistNet("NET1", [FakeNetNode("R1"), FakeNetNode("C1")]),
                FakeNetlistNet("NET2", [FakeNetNode("R1"), FakeNetNode("C1")]),
                FakeNetlistNet("NET3", [FakeNetNode("R1")]),
            ]
        )
        result = run(ComponentsConnectedCheck(reference_a="R1", reference_b="C1"), ctx)
        assert result.details["count"] == 2

    def test_min_connections_below_actual_count_passes(self):
        ctx = ctx_with(
            nets=[
                FakeNetlistNet("NET1", [FakeNetNode("R1"), FakeNetNode("C1")]),
                FakeNetlistNet("NET2", [FakeNetNode("R1"), FakeNetNode("C1")]),
            ]
        )
        check = ComponentsConnectedCheck(reference_a="R1", reference_b="C1", min_connections=2)
        assert run(check, ctx).is_pass

    def test_min_connections_above_actual_count_fails(self):
        ctx = ctx_with(nets=[FakeNetlistNet("NET1", [FakeNetNode("R1"), FakeNetNode("C1")])])
        check = ComponentsConnectedCheck(reference_a="R1", reference_b="C1", min_connections=2)
        result = run(check, ctx)

        assert result.is_fail
        assert "minimum required: 2" in result.message

    def test_fails_when_neither_component_appears(self):
        ctx = ctx_with(nets=[FakeNetlistNet("NET1", [FakeNetNode("U1")])])
        assert run(
            ComponentsConnectedCheck(reference_a="R1", reference_b="C1"),
            ctx,
        ).is_fail


class TestComponentPropertyCheck:
    def test_skips_without_schematic(self):
        check = ComponentPropertyCheck(reference="R1", field="MPN", expected="x")
        assert run(check, FakeContext()).status == TestStatus.SKIP

    def test_matches_user_field(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"MPN": "RC0805"})])
        result = run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="RC0805"),
            ctx,
        )
        assert result.is_pass
        assert result.details["actual"] == "RC0805"

    def test_matches_standard_field(self):
        ctx = ctx_with([FakeSymbol("R1", fields={"Reference": "R1"})])
        assert run(
            ComponentPropertyCheck(reference="R1", field="Reference", expected="R1"),
            ctx,
        ).is_pass

    def test_user_field_overrides_standard_field(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"MPN": "user"}, fields={"MPN": "standard"})])
        result = run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="user"),
            ctx,
        )
        assert result.is_pass

    def test_missing_field_fails(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={})])
        result = run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="x"),
            ctx,
        )
        assert result.is_fail
        assert "has no field" in result.message

    def test_value_mismatch_fails(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"MPN": "RC0805"})])
        result = run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="other"),
            ctx,
        )
        assert result.is_fail
        assert "mismatch" in result.message

    def test_match_is_case_insensitive_by_default(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"MPN": "RC0805"})])
        assert run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="rc0805"),
            ctx,
        ).is_pass

    def test_case_sensitive_rejects_differing_case(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"MPN": "RC0805"})])
        assert run(
            ComponentPropertyCheck(
                reference="R1", field="MPN", expected="rc0805", case_sensitive=True
            ),
            ctx,
        ).is_fail

    def test_non_string_field_value_is_stringified(self):
        ctx = ctx_with([FakeSymbol("R1", user_fields={"Qty": 5})])
        assert run(
            ComponentPropertyCheck(reference="R1", field="Qty", expected="5"),
            ctx,
        ).is_pass

    def test_fails_when_component_missing(self):
        result = run(
            ComponentPropertyCheck(reference="R9", field="MPN", expected="x"),
            ctx_with([FakeSymbol("R1")]),
        )
        assert result.is_fail
        assert "not found" in result.message

    def test_symbol_without_field_attributes_is_tolerated(self):
        from conftest import BareSymbol

        ctx = ctx_with([BareSymbol("R1")])
        result = run(
            ComponentPropertyCheck(reference="R1", field="MPN", expected="x"),
            ctx,
        )
        assert result.is_fail
        assert "has no field" in result.message


class TestFootprintAssignedCheck:
    def test_skips_without_schematic(self):
        assert run(FootprintAssignedCheck(), FakeContext()).status == TestStatus.SKIP

    def test_passes_when_all_have_footprints(self):
        ctx = ctx_with(
            [
                FakeSymbol("R1", footprint="R_0805"),
                FakeSymbol("C1", footprint="C_0603"),
            ]
        )
        result = run(FootprintAssignedCheck(), ctx)

        assert result.is_pass
        assert result.details["checked"] == 2

    def test_fails_listing_missing_footprints(self):
        ctx = ctx_with(
            [
                FakeSymbol("R1", footprint="R_0805"),
                FakeSymbol("C1"),
                FakeSymbol("U1"),
            ]
        )
        result = run(FootprintAssignedCheck(), ctx)

        assert result.is_fail
        assert result.details["missing"] == ["C1", "U1"]

    def test_whitespace_only_footprint_counts_as_missing(self):
        ctx = ctx_with([FakeSymbol("R1", footprint="   ")])
        result = run(FootprintAssignedCheck(), ctx)
        assert result.details["missing"] == ["R1"]

    def test_allow_none_tolerates_missing(self):
        ctx = ctx_with([FakeSymbol("R1"), FakeSymbol("C1")])
        result = run(FootprintAssignedCheck(allow_none=True), ctx)

        assert result.is_pass
        assert "missing" not in result.details

    def test_scoped_to_single_reference(self):
        ctx = ctx_with(
            [
                FakeSymbol("R1", footprint="R_0805"),
                FakeSymbol("C1"),
            ]
        )
        assert run(FootprintAssignedCheck(reference="R1"), ctx).is_pass

    def test_scoped_reference_still_reports_missing(self):
        ctx = ctx_with([FakeSymbol("R1"), FakeSymbol("C1", footprint="C_0603")])
        result = run(FootprintAssignedCheck(reference="R1"), ctx)
        assert result.details["missing"] == ["R1"]

    def test_fails_when_scoped_reference_absent(self):
        result = run(FootprintAssignedCheck(reference="R9"), ctx_with([FakeSymbol("R1")]))
        assert result.is_fail
        assert "not found" in result.message

    def test_empty_schematic_passes(self):
        assert run(FootprintAssignedCheck(), ctx_with([])).is_pass


class TestSymbolInLibraryCheck:
    def test_skips_without_schematic(self):
        check = SymbolInLibraryCheck(lib_id="Device:R")
        assert run(check, FakeContext()).status == TestStatus.SKIP

    def test_passes_when_symbol_listed(self):
        from conftest import cli_json

        ctx = ctx_with([FakeSymbol("R1")], cli=cli_json({"symbols": [{"name": "R"}]}))
        result = run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert result.is_pass
        assert result.details["lib_id"] == "Device:R"

    def test_fails_and_lists_available_symbols(self):
        from conftest import cli_json

        ctx = ctx_with([FakeSymbol("R1")], cli=cli_json({"symbols": [{"name": "C"}]}))
        result = run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert result.is_fail
        assert result.details["available_symbols"] == ["C"]

    def test_cli_failure_yields_error(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=cli_result(returncode=1, stderr="no such command"))
        result = run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert result.status == TestStatus.ERROR
        assert "no such command" in result.message

    def test_malformed_json_yields_error(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=cli_result(stdout="not json"))
        result = run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert result.status == TestStatus.ERROR
        assert "parse" in result.message

    def test_missing_symbols_key_reports_not_found(self):
        from conftest import cli_json

        ctx = ctx_with([FakeSymbol("R1")], cli=cli_json({}))
        result = run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert result.is_fail
        assert result.details["available_symbols"] == []

    def test_queries_with_json_format(self):
        from conftest import cli_json

        ctx = ctx_with([FakeSymbol("R1")], cli=cli_json({"symbols": [{"name": "R"}]}))
        run(SymbolInLibraryCheck(lib_id="Device:R"), ctx)

        assert ctx.cli_calls == [["sym", "list", "--format", "json", "Device:R"]]


class TestERCRunCheck:
    def test_skips_without_schematic(self):
        assert run(ERCRunCheck(), FakeContext()).status == TestStatus.SKIP

    def test_passes_with_no_violations(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=erc_report())
        result = run(ERCRunCheck(), ctx)

        assert result.is_pass
        assert result.details["total_violations"] == 0

    def test_fails_on_error_violations(self):
        ctx = ctx_with(
            [FakeSymbol("R1")],
            cli=erc_report(violation("error", "pin not driven"), violation("warning", "warn")),
        )
        result = run(ERCRunCheck(severity="error"), ctx)

        assert result.is_fail
        assert result.details["total_violations"] == 2
        assert result.details["filtered_violations"] == 1
        assert result.details["violations"][0]["message"] == "pin not driven"

    def test_severity_all_includes_warnings(self):
        ctx = ctx_with(
            [FakeSymbol("R1")],
            cli=erc_report(violation("error", "e"), violation("warning", "w")),
        )
        result = run(ERCRunCheck(severity="all"), ctx)
        assert result.details["filtered_violations"] == 2

    def test_severity_warning_filters_to_warnings(self):
        ctx = ctx_with(
            [FakeSymbol("R1")],
            cli=erc_report(violation("error", "e"), violation("warning", "w")),
        )
        result = run(ERCRunCheck(severity="warning"), ctx)
        assert result.details["filtered_violations"] == 1

    def test_unparseable_output_is_an_error_not_a_silent_pass(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=cli_result(stdout="not json at all"))
        result = run(ERCRunCheck(), ctx)

        # An unreadable report must not read as "clean".
        assert result.status == TestStatus.ERROR

    def test_command_failure_skips_by_default(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=cli_result(returncode=1, stderr="boom"))
        result = run(ERCRunCheck(), ctx)

        assert result.status == TestStatus.SKIP
        assert "boom" in result.message

    def test_command_failure_errors_when_strict(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=cli_result(returncode=1, stderr="boom"))
        result = run(ERCRunCheck(strict=True), ctx)

        assert result.status == TestStatus.ERROR

    def test_invocations_use_schematic_path(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=erc_report())
        run(ERCRunCheck(), ctx)

        args = ctx.cli_calls[0]
        assert args[:4] == ["sch", "erc", "--format", "json"]
        # The report must go to a file; kicad-cli only summarises on stdout.
        assert args[-1] == str(ctx.schematic_path)
        assert Path(args[args.index("-o") + 1]).name == "report.json"

    def test_explicit_schematic_path_overrides_context(self):
        ctx = ctx_with([FakeSymbol("R1")], cli=erc_report())
        run(ERCRunCheck(schematic_path="other.kicad_sch"), ctx)

        assert ctx.cli_calls[0][-1] == "other.kicad_sch"


class TestConsistencyCheck:
    def _ctx(self, sch_refs, pcb_refs):
        from conftest import FakeBoard, FakeFootprint

        return FakeContext(
            schematic=FakeSchematic(symbols=[FakeSymbol(r) for r in sch_refs]),
            board=FakeBoard(footprints=[FakeFootprint(r) for r in pcb_refs]),
        )

    def test_skips_without_schematic(self):
        from conftest import FakeBoard

        ctx = FakeContext(board=FakeBoard())
        assert run(ConsistencyCheck(), ctx).status == TestStatus.SKIP

    def test_skips_without_board(self):
        assert run(ConsistencyCheck(), ctx_with([FakeSymbol("R1")])).status == TestStatus.SKIP

    def test_passes_when_references_match(self):
        result = run(ConsistencyCheck(), self._ctx(["R1", "C1"], ["R1", "C1"]))

        assert result.is_pass
        assert result.details["schematic_count"] == 2
        assert result.details["pcb_count"] == 2

    def test_fails_on_missing_pcb_footprint(self):
        result = run(ConsistencyCheck(), self._ctx(["R1", "C1"], ["R1"]))

        assert result.is_fail
        assert result.details["in_schematic_not_pcb"] == ["C1"]

    def test_fails_on_extra_pcb_footprint(self):
        result = run(ConsistencyCheck(), self._ctx(["R1"], ["R1", "D1"]))

        assert result.is_fail
        assert result.details["in_pcb_not_schematic"] == ["D1"]

    def test_reports_both_directions(self):
        result = run(ConsistencyCheck(), self._ctx(["R1", "C1"], ["R1", "D1"]))
        assert result.details["in_schematic_not_pcb"] == ["C1"]
        assert result.details["in_pcb_not_schematic"] == ["D1"]

    def test_require_both_false_passes_despite_drift(self):
        result = run(ConsistencyCheck(require_both=False), self._ctx(["R1", "C1"], ["R1"]))
        assert result.is_pass

    def test_duplicate_schematic_references_are_deduped(self):
        result = run(ConsistencyCheck(), self._ctx(["R1", "R1"], ["R1"]))
        assert result.details["schematic_count"] == 1


class TestSchematicChecksThroughRunner:
    def test_runner_reports_mixed_outcomes(self):
        from kicad_evaltor.checks.schematic.component_exists import ComponentExistsCheck as Exists

        ctx = ctx_with([FakeSymbol("R1")])
        report = TestRunner(
            [
                Exists(reference="R1"),
                Exists(reference="R9"),
            ]
        ).run(ctx)

        assert report.passed_count == 1
        assert report.failed_count == 1
        assert not report.all_passed

    def test_checks_skipped_cleanly_when_no_schematic(self):
        checks = [
            ComponentExistsCheck(reference="R1"),
            ComponentValueCheck(reference="R1", expected="10k"),
            ComponentsConnectedCheck(reference_a="R1", reference_b="C1"),
            ComponentPropertyCheck(reference="R1", field="MPN", expected="x"),
            FootprintAssignedCheck(),
            SymbolInLibraryCheck(lib_id="Device:R"),
            ERCRunCheck(),
            ConsistencyCheck(),
        ]
        report = TestRunner(checks).run(FakeContext())

        assert report.skipped_count == len(checks)
        assert report.error_count == 0
