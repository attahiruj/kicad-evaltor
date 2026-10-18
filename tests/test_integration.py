"""Integration tests that drive the real kicad-cli binary.

Split into two tiers:

* ``TestKicadCliReports`` runs real sch erc invocations against the demo
  circuit. These only need the kicad-cli executable.
* ``TestERCAndDRCChecksAgainstRealKicadCli`` runs the checks themselves.

Board checks are exercised against a real board in ``test_pcb_integration.py``.
"""

import json
import subprocess
from pathlib import Path

import pytest

from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.schematic.erc_check import ERCRunCheck
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.core.runner import TestRunner
from kicad_evaltor.utils.kicad_cli import parse_erc_report
from conftest import demo_schematic_path

DEMO_SCHEMATIC = demo_schematic_path()


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Keep kicad-cli report files out of the repository.

    `sch erc` and `pcb drc` write their JSON report into the current working
    directory unless given -o, so every check invocation in this module runs
    from a temp directory.
    """
    monkeypatch.chdir(tmp_path)


def run_cli(cli: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(cli), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=cwd,
    )


class TestKicadCliReports:
    """Real invocations, asserting on the reports kicad-cli actually emits."""

    def test_erc_report_is_written_to_a_file_not_stdout(self, kicad_cli_required, tmp_path):
        result = run_cli(
            kicad_cli_required, ["sch", "erc", "--format", "json", str(DEMO_SCHEMATIC)], tmp_path
        )

        assert result.returncode == 0
        # stdout is a human-readable summary, not the JSON payload.
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
        assert "Saved ERC Report" in result.stdout

    def test_erc_report_nests_violations_under_sheets(self, kicad_cli_required, tmp_path):
        # Read the shape off a real report rather than trusting the docs: KiCad 10
        # writes metadata at the top level and the violations per sheet, with no
        # top-level "violations" key at all.
        report = tmp_path / "erc.json"
        result = run_cli(
            kicad_cli_required,
            ["sch", "erc", "--format", "json", "-o", str(report), str(DEMO_SCHEMATIC)],
            tmp_path,
        )

        assert result.returncode == 0
        data = json.loads(report.read_text(encoding="utf-8"))

        assert "violations" not in data
        assert isinstance(data["sheets"], list)
        assert data["sheets"], "every sheet is reported, even a clean one"

    def test_parse_erc_report_agrees_with_the_raw_report(self, kicad_cli_required, tmp_path):
        # The invariant that matters: whatever the report holds, the parser
        # returns exactly that many violations, counted across every sheet. A
        # parser that only looks at the top level returns 0 here and passes a
        # schematic with errors on it.
        report = tmp_path / "erc.json"
        run_cli(
            kicad_cli_required,
            ["sch", "erc", "--format", "json", "-o", str(report), str(DEMO_SCHEMATIC)],
            tmp_path,
        )

        data = json.loads(report.read_text(encoding="utf-8"))
        expected = sum(len(sheet["violations"]) for sheet in data["sheets"])

        assert len(parse_erc_report(report.read_text(encoding="utf-8"))) == expected

    def test_parse_erc_report_finds_violations_in_a_real_report(self, kicad_cli_required, tmp_path):
        # The demo design is clean, so it cannot show the parser finding
        # anything. This sheet has one resistor with both pins floating, which
        # KiCad reports as errors, and it embeds its own library symbol so it
        # needs no project around it.
        dirty = Path(__file__).resolve().parent / "fixtures" / "erc_violations.kicad_sch"
        report = tmp_path / "dirty.json"
        result = run_cli(
            kicad_cli_required,
            ["sch", "erc", "--format", "json", "-o", str(report), str(dirty)],
            tmp_path,
        )

        assert result.returncode == 0
        violations = parse_erc_report(report.read_text(encoding="utf-8"))

        assert violations, "a real report with errors must not parse as clean"
        assert any(v["severity"] == "error" for v in violations)
        # Positions live in items[*].pos; nothing is named "message" or "at".
        assert violations[0]["items"]
        assert "message" not in violations[0]

    def test_erc_check_fails_on_a_sheet_that_really_has_errors(self, kicad_cli_required, tmp_path):
        dirty = Path(__file__).resolve().parent / "fixtures" / "erc_violations.kicad_sch"
        ctx = DesignContext(
            schematic_path=dirty,
            kicad_cli_path=str(kicad_cli_required),
        )
        try:
            result = ERCRunCheck(severity="error").run(ctx)
        finally:
            ctx.close()

        assert result.is_fail
        assert result.details["total_violations"] > 0
        assert result.details["violations"][0]["description"]
        assert result.details["violations"][0]["sheet"] == "/"


class TestERCAndDRCChecksAgainstRealKicadCli:
    def test_erc_check_does_not_error(self, kicad_cli_required, tmp_path):
        ctx = DesignContext(
            schematic_path=DEMO_SCHEMATIC,
            kicad_cli_path=str(kicad_cli_required),
        )
        try:
            report = TestRunner([ERCRunCheck()]).run(ctx)
        finally:
            ctx.close()

        assert report.error_count == 0

    def test_erc_check_completes_against_the_real_binary(self, kicad_cli_required):
        # The demo schematic is clean, so this cannot prove detection either way;
        # it only shows the check completes without erroring on a real binary.
        ctx = DesignContext(
            schematic_path=DEMO_SCHEMATIC,
            kicad_cli_path=str(kicad_cli_required),
        )
        try:
            result = ERCRunCheck().run(ctx)
        finally:
            ctx.close()

        assert result.status == TestStatus.PASS


class TestSymbolInLibraryCheckAgainstRealKicadCli:
    def test_sym_list_subcommand_does_not_exist(self, kicad_cli_required):
        result = subprocess.run(
            [str(kicad_cli_required), "sym", "list", "--format", "json", "Device:R"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode != 0
        # kicad-cli reports the unknown subcommand on one stream or the other.
        assert "list" in (result.stdout + result.stderr)

    def test_check_reports_error_against_real_cli(self, kicad_cli_required, tmp_path):
        from kicad_evaltor.checks.schematic.symbol_in_library import SymbolInLibraryCheck

        ctx = DesignContext(
            schematic_path=DEMO_SCHEMATIC,
            kicad_cli_path=str(kicad_cli_required),
        )
        try:
            result = SymbolInLibraryCheck(lib_id="Device:R").run(ctx)
        finally:
            ctx.close()

        # The check shells out to a subcommand KiCad 10 removed, so it can only
        # ever error out. This is the current, broken behaviour.
        assert result.status == TestStatus.ERROR
