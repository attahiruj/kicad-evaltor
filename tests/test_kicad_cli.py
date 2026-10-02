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
    def test_parse_erc_report_extracts_violations(self):
        payload = json.dumps({"erc_violation": [{"severity": "error"}, {"severity": "warning"}]})
        assert parse_erc_report(payload) == [{"severity": "error"}, {"severity": "warning"}]

    def test_parse_erc_report_reads_the_key_kicad_10_emits(self):
        # KiCad 10 writes "violations"; "erc_violation" was never a real key.
        payload = json.dumps({"violations": [{"severity": "error", "type": "unconnected"}]})
        assert parse_erc_report(payload) == [{"severity": "error", "type": "unconnected"}]

    def test_parse_drc_report_reads_the_key_kicad_10_emits(self):
        payload = json.dumps({"violations": [{"severity": "error", "type": "track_dangling"}]})
        assert parse_drc_report(payload) == [{"severity": "error", "type": "track_dangling"}]

    def test_erc_and_drc_share_the_violations_key(self):
        payload = json.dumps({"violations": [{"severity": "error"}]})
        assert parse_erc_report(payload) == parse_drc_report(payload)

    def test_parse_erc_report_defaults_to_empty(self):
        assert parse_erc_report("{}") == []

    def test_parse_erc_report_rejects_malformed_json(self):
        assert parse_erc_report("not json") == []

    def test_parse_erc_report_rejects_non_list_violations(self):
        assert parse_erc_report(json.dumps({"erc_violation": {"oops": 1}})) == []

    def test_parse_drc_report_extracts_violations(self):
        payload = json.dumps({"drc_violation": [{"severity": "error"}]})
        assert parse_drc_report(payload) == [{"severity": "error"}]

    def test_parse_drc_report_defaults_to_empty(self):
        assert parse_drc_report("{}") == []

    def test_parse_drc_report_rejects_malformed_json(self):
        assert parse_drc_report("") == []
        assert parse_drc_report("<html>error</html>") == []

    def test_parse_drc_report_rejects_non_list_violations(self):
        assert parse_drc_report(json.dumps({"drc_violation": "nope"})) == []


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
