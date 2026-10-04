"""Tests for the kicad-cli subprocess wrapper and report parsers.

subprocess.run is monkeypatched throughout so no KiCad install is required.
"""

import json
import subprocess

import pytest

from kicad_evaltor.utils.kicad_cli import (
    SubprocessResult,
    filter_violations_by_severity,
    parse_drc_report,
    parse_erc_report,
    report_workspace,
    run_kicad_cli,
    violation_details,
)


class TestReportWorkspace:
    def test_creates_an_existing_directory(self):
        with report_workspace("erc") as workdir:
            assert workdir.is_dir()

    def test_directory_is_removed_on_exit(self):
        with report_workspace("drc") as workdir:
            created = workdir
        assert not created.exists()

    def test_directory_is_removed_even_when_the_body_raises(self):
        created = []

        def boom():
            with report_workspace("erc") as workdir:
                created.append(workdir)
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            boom()

        assert created
        assert not created[0].exists()

    def test_each_call_gets_a_distinct_directory(self):
        with report_workspace("erc") as first, report_workspace("erc") as second:
            assert first != second


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestSubprocessResult:
    def test_success_only_on_zero_exit(self):
        assert SubprocessResult(0, "", "").success
        assert not SubprocessResult(1, "", "").success
        assert not SubprocessResult(-1, "", "").success


class TestRunKiCadCli:
    def test_returns_completed_process_output(self, monkeypatch):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return FakeCompleted(0, "out", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = run_kicad_cli(["kicad-cli", "sch", "erc"])

        assert result == SubprocessResult(0, "out", "")
        assert seen["args"] == ["kicad-cli", "sch", "erc"]

    def test_passes_through_subprocess_options(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            subprocess, "run", lambda _args, **kw: seen.update(kw) or FakeCompleted(0, "", "")
        )
        run_kicad_cli(["kicad-cli", "version"], cwd="/tmp", timeout=5)

        assert seen["capture_output"] is True
        assert seen["text"] is True
        assert seen["cwd"] == "/tmp"
        assert seen["timeout"] == 5

    def test_nonzero_exit_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda _args, **_kw: FakeCompleted(3, "", "bad"))
        result = run_kicad_cli(["kicad-cli", "bogus"])
        assert result.returncode == 3
        assert result.stderr == "bad"

    def test_timeout_returns_minus_one(self, monkeypatch):
        def raise_timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=60)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        result = run_kicad_cli(["kicad-cli", "sch", "erc"])

        assert result.returncode == -1
        assert "timed out after 60s" in result.stderr

    def test_timeout_keeps_partial_stdout(self, monkeypatch):
        def raise_timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=60, output=b"partial")

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        assert run_kicad_cli(["kicad-cli"]).stdout == "partial"

    def test_timeout_with_no_output_yields_empty_stdout(self, monkeypatch):
        def raise_timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=60)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        assert run_kicad_cli(["kicad-cli"]).stdout == ""

    def test_missing_binary_yields_actionable_message(self, monkeypatch):
        def raise_missing(args, **kwargs):
            raise FileNotFoundError("kicad-cli")

        monkeypatch.setattr(subprocess, "run", raise_missing)
        result = run_kicad_cli(["kicad-cli"])

        assert result.returncode == -1
        assert "in PATH" in result.stderr

    def test_unexpected_error_is_captured_as_stderr(self, monkeypatch):
        def raise_os(args, **kwargs):
            raise OSError("access denied")

        monkeypatch.setattr(subprocess, "run", raise_os)
        result = run_kicad_cli(["kicad-cli"])

        assert result.returncode == -1
        assert result.stderr == "access denied"


class TestReportParsers:
    def test_parse_erc_report_reads_the_per_sheet_shape_kicad_10_writes(self):
        # KiCad 10 puts every violation in sheets[*].violations and writes no
        # top-level "violations" key at all. Reading only the top level finds
        # nothing and reports a schematic with errors on it as clean.
        payload = json.dumps(
            {
                "$schema": "https://schemas.kicad.org/erc.v1.json",
                "kicad_version": "10.0.1",
                "sheets": [
                    {
                        "path": "/",
                        "uuid_path": "/root-uuid",
                        "violations": [{"severity": "error", "type": "pin_not_connected"}],
                    }
                ],
            }
        )

        found = parse_erc_report(payload)

        assert [v["type"] for v in found] == ["pin_not_connected"]
        assert found[0]["sheet"] == "/"

    def test_parse_erc_report_collects_every_sheet(self):
        # A hierarchical design reports per sheet, and a violation on a subsheet
        # must not be dropped just because the root sheet's list came first.
        payload = json.dumps(
            {
                "sheets": [
                    {"path": "/", "violations": [{"severity": "error", "type": "root_only"}]},
                    {"path": "/sub-uuid", "violations": [{"severity": "error", "type": "on_sub"}]},
                ]
            }
        )

        found = parse_erc_report(payload)

        assert [(v["type"], v["sheet"]) for v in found] == [
            ("root_only", "/"),
            ("on_sub", "/sub-uuid"),
        ]

    def test_parse_erc_report_reports_a_clean_sheet_as_no_violations(self):
        payload = json.dumps(
            {"sheets": [{"path": "/", "uuid_path": "/root", "violations": []}]},
        )

        assert parse_erc_report(payload) == []

    def test_parse_erc_report_still_reads_the_flat_shape_of_kicad_8_and_9(self):
        payload = json.dumps({"violations": [{"severity": "error", "type": "unconnected"}]})

        assert parse_erc_report(payload) == [{"severity": "error", "type": "unconnected"}]

    def test_parse_drc_report_reads_the_flat_list_kicad_10_writes(self):
        # DRC did not move per sheet, so it keeps one top-level list.
        payload = json.dumps({"violations": [{"severity": "error", "type": "track_dangling"}]})

        assert parse_drc_report(payload) == [{"severity": "error", "type": "track_dangling"}]

    def test_drc_violations_carry_no_sheet(self):
        payload = json.dumps({"violations": [{"severity": "error", "type": "clearance"}]})

        assert "sheet" not in parse_drc_report(payload)[0]

    def test_parse_erc_report_defaults_to_empty(self):
        assert parse_erc_report("{}") == []

    def test_parse_erc_report_rejects_malformed_json(self):
        assert parse_erc_report("not json") == []

    def test_parse_erc_report_rejects_a_non_list_sheets_key(self):
        assert parse_erc_report(json.dumps({"sheets": {"oops": 1}})) == []

    def test_parse_erc_report_rejects_non_list_violations(self):
        payload = json.dumps({"sheets": [{"path": "/", "violations": {"oops": 1}}]})

        assert parse_erc_report(payload) == []

    def test_parse_drc_report_rejects_non_list_violations(self):
        assert parse_drc_report(json.dumps({"violations": "nope"})) == []

    def test_parse_drc_report_defaults_to_empty(self):
        assert parse_drc_report("{}") == []

    def test_parse_drc_report_rejects_malformed_json(self):
        assert parse_drc_report("") == []
        assert parse_drc_report("<html>error</html>") == []


class TestViolationDetails:
    def test_it_names_the_fields_kicad_actually_writes(self):
        # KiCad spells the human text "description" and puts positions in
        # items[*].pos. Reading "message" or "at" yields None for every entry.
        details = violation_details(
            {
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "items": [{"description": "R1 Pin 1", "pos": {"x": 1.0, "y": 2.0}}],
            }
        )

        assert details["description"] == "Pin not connected"
        assert details["items"][0]["pos"] == {"x": 1.0, "y": 2.0}

    def test_a_violation_with_no_items_reports_an_empty_list(self):
        assert violation_details({"type": "x", "severity": "error"})["items"] == []

    def test_the_sheet_is_carried_through_when_the_report_has_one(self):
        assert violation_details({"severity": "error", "sheet": "/sub"})["sheet"] == "/sub"

    def test_the_sheet_is_absent_otherwise(self):
        assert "sheet" not in violation_details({"severity": "error"})


class TestFilterViolationsBySeverity:
    @pytest.fixture
    def violations(self):
        return [
            {"severity": "error", "message": "a"},
            {"severity": "warning", "message": "b"},
            {"severity": "warning", "message": "c"},
        ]

    def test_all_returns_everything_unchanged(self, violations):
        assert filter_violations_by_severity(violations, "all") == violations

    def test_filters_errors(self, violations):
        assert filter_violations_by_severity(violations, "error") == [violations[0]]

    def test_filters_warnings(self, violations):
        assert [v["message"] for v in filter_violations_by_severity(violations, "warning")] == [
            "b",
            "c",
        ]

    def test_matching_is_case_insensitive(self, violations):
        assert filter_violations_by_severity(violations, "ERROR") == [violations[0]]

    def test_violations_without_severity_are_dropped(self):
        assert filter_violations_by_severity([{"message": "x"}], "error") == []

    def test_unknown_severity_yields_empty(self, violations):
        assert filter_violations_by_severity(violations, "fatal") == []
