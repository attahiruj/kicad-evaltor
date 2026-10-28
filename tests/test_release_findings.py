"""Structured ERC/DRC findings, their filters, and the kicad-cli fixes of 0.6.1.

Everything here runs on the real imu project under ``tests/fixtures/boards/imu``,
on copies of it changed one way by ``tools/record_pcb_fixtures.py``
(``IMU_VARIANTS``), or on reports kicad-cli 10.0.7 wrote for them
(``--severity-all``, DRC with ``--schematic-parity --refill-zones``):

- ``report_erc.json`` and ``report_drc.json``: the project as it is (ERC: two
  LSM6DSL pins against a PWR_FLAG; DRC: clean).
- ``report_j2_edge_drc.json``: J2 pushed 0.8 mm onto the board edge, so its
  silkscreen crosses it (the field report's USB connector did the same).
- ``report_relinked_*.json``: a copy edited the way the field reports
  describe: the LED symbols and footprints moved to a ``Logger`` library the
  configuration lacks, the resistor footprints saved without their
  ``Resistor_SMD:`` nickname, the H1 mounting hole's symbol deleted, and C1's
  ``LCSC`` field changed on the schematic.

Tests that need KiCad (marked ``integration``) build the same copies in
``tmp_path`` and run kicad-cli on them.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import PCB_RECORDINGS, FakeContext, cli_result
from kicad_evaltor.board_file import load_board
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.pcb.drc_check import DRCRunCheck
from kicad_evaltor.checks.pcb.drc_rules import SchematicParityCheck, _mechanical
from kicad_evaltor.checks.schematic.erc_check import ERCRunCheck
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.findings import (
    DESIGN,
    ENVIRONMENT,
    ProjectSuppressions,
    finding_origin,
    finding_references,
    findings_owned_by,
    is_owned_by,
    parse_item,
    unsuppressed_project,
)
from kicad_evaltor.schematic_file import load_schematic
from kicad_evaltor.utils import kicad_cli, kicad_python
from kicad_evaltor.utils.kicad_cli import (
    CHILD_ENV_DROPPED,
    describe_failure,
    kicad_child_env,
    parse_drc_report,
    parse_erc_report,
    violation_details,
)

IMU = Path(__file__).resolve().parent / "fixtures" / "boards" / "imu"
TOOLS = Path(__file__).resolve().parent.parent / "tools"
GLX = "__GLX_VENDOR_LIBRARY_NAME"


def design(suffix: str) -> Path:
    return IMU / f"imu{suffix}"


def report(name: str) -> str:
    return (IMU / f"report_{name}.json").read_text(encoding="utf-8")


def erc(name: str = "erc") -> list[dict[str, Any]]:
    return parse_erc_report(report(name))


def drc(name: str = "j2_edge_drc") -> list[dict[str, Any]]:
    """By default the DRC of the copy with J2's silkscreen over the edge."""
    return parse_drc_report(report(name))


def recorder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "record_pcb_fixtures", TOOLS / "record_pcb_fixtures.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def flag_on_bidirectional(finding: dict[str, Any]) -> bool:
    """The README's allowance: a bidirectional pin opposed only by a PWR_FLAG."""
    items = finding["items"]
    flags = [i for i in items if (i.get("lib_id") or "").endswith(":PWR_FLAG")]
    return (
        finding["type"] == "pin_to_pin"
        and sorted(i["pin_type"] for i in items) == ["Bidirectional", "Power output"]
        and len(flags) == 1
        and flags[0]["pin_type"] == "Power output"
    )


def copy_case(tmp_path: Path, variant: str = "base") -> Path:
    """A folder ``imu`` holding the imu project, changed as ``variant`` says."""
    board: Path = recorder().make_imu_variant(variant, tmp_path / "imu")
    return board.parent


def edit_project(folder: Path, edit: Any) -> None:
    project = folder / f"{folder.name}.kicad_pro"
    data = json.loads(project.read_text(encoding="utf-8"))
    edit(data)
    project.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ------------------------------------------------------------- 1. child env


class TestChildEnvironment:
    def test_drops_the_gl_vendor_and_keeps_the_rest(self):
        env = kicad_child_env({GLX: "nvidia", "PATH": "/usr/bin", "HOME": "/h"})
        assert env == {"PATH": "/usr/bin", "HOME": "/h"}
        assert GLX in CHILD_ENV_DROPPED

    def test_defaults_to_this_process_environment(self, monkeypatch):
        monkeypatch.setenv(GLX, "nvidia")
        monkeypatch.setenv("KICAD_EVALTOR_PROBE", "kept")
        env = kicad_child_env()
        assert GLX not in env
        assert env["KICAD_EVALTOR_PROBE"] == "kept"

    @staticmethod
    def _capture(monkeypatch, module) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []

        def fake_run(args, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(module.subprocess, "run", fake_run)
        return calls

    def test_kicad_cli_starts_without_it(self, monkeypatch):
        monkeypatch.setenv(GLX, "nvidia")
        calls = self._capture(monkeypatch, kicad_cli)
        kicad_cli.run_kicad_cli(["kicad-cli", "version"])
        assert GLX not in calls[0]["env"]
        assert "PATH" in calls[0]["env"]

    def test_kicad_python_starts_without_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv(GLX, "nvidia")
        calls = self._capture(monkeypatch, kicad_python)
        kicad_python.run_kicad_python(Path("python3"), tmp_path / "script.py", [])
        assert GLX not in calls[0]["env"]


# --------------------------------------------------------- 2. failure reason


class TestFailureReason:
    def test_a_killed_run_names_its_signal(self):
        result = cli_result(returncode=-9)
        assert describe_failure(result) == "killed by signal 9 (SIGKILL)"

    def test_last_lines_of_stderr_and_stdout_follow(self):
        result = cli_result(
            returncode=1, stdout="Loading board\n", stderr="x\nFailed to load board\n"
        )
        assert describe_failure(result) == "exit code 1; Failed to load board; Loading board"

    def test_drc_says_why_it_failed(self):
        # Field report #6: 'DRC command failed: ' with empty details.
        ctx = FakeContext(board=object(), cli_results=cli_result(returncode=-11))
        result = DRCRunCheck(strict=True).run(ctx)
        assert result.status == TestStatus.ERROR
        assert result.message == "DRC command failed: killed by signal 11 (SIGSEGV)"
        assert result.details["returncode"] == -11
        assert result.details["signal"] == "SIGSEGV"
        assert result.details["timed_out"] is False

    def test_drc_rule_checks_carry_the_reason_too(self):
        ctx = FakeContext(
            board=object(),
            cli_results=cli_result(returncode=-9),
            board_path=str(design(".kicad_pcb")),
        )
        result = SchematicParityCheck().run(ctx)
        assert result.status == TestStatus.ERROR
        assert "SIGKILL" in result.message
        assert result.details["returncode"] == -9

    def test_a_drc_timeout_keeps_its_reason(self):
        timed_out = kicad_cli.SubprocessResult(-1, "", "Command timed out after 180s", True)
        result = DRCRunCheck().run(FakeContext(board=object(), cli_results=timed_out))
        assert result.status == TestStatus.ERROR
        assert "Command timed out after 180s" in result.message
        assert result.details["timed_out"] is True

    def test_erc_says_why_it_failed(self):
        ctx = FakeContext(schematic=object(), cli_results=cli_result(returncode=-6))
        result = ERCRunCheck(strict=True).run(ctx)
        assert result.status == TestStatus.ERROR
        assert result.message == "ERC command failed: killed by signal 6 (SIGABRT)"
        assert result.details["returncode"] == -6


# ------------------------------------------------- 3. item descriptions (P, H)

# Each description below is copied from a kicad-cli 10.0.7 report on a real
# project: the fixtures, the edited copies described above, or KiCad's demos.
FORMATS = [
    (
        "Symbol U5 Pin 1 [SDO/SA0, Bidirectional, Line]",
        dict(kind="pin", reference="U5", pin="1", pin_name="SDO/SA0", pin_type="Bidirectional"),
    ),
    (
        "Symbol #FLG06 Pin 1 [Power output, Line]",
        dict(kind="pin", reference="#FLG06", pin="1", pin_name=None, pin_type="Power output"),
    ),
    (
        "Symbol #PWR02 Hidden pin 1 [+12V, Power input, Line]",
        dict(
            kind="pin",
            reference="#PWR02",
            pin="1",
            pin_name="+12V",
            pin_type="Power input",
            hidden=True,
        ),
    ),
    (
        "Symbol J6 Pin R [Passive, Line]",
        dict(kind="pin", reference="J6", pin="R", pin_type="Passive"),
    ),
    ("Symbol D3 [LED]", dict(kind="symbol", reference="D3", value="LED", pin=None)),
    ("Footprint J1", dict(kind="footprint", reference="J1")),
    (
        "Pad 2 [/SD_CLK] of J3 on F.Cu",
        dict(kind="pad", reference="J3", pin="2", net="/SD_CLK", layer="F.Cu"),
    ),
    (
        "Pad 1 [Net-(U2-PS/SYNC)] of C5 on F.Cu",
        dict(kind="pad", reference="C5", pin="1", net="Net-(U2-PS/SYNC)"),
    ),
    (
        "PTH pad 1 [+BATT] of J2",
        dict(kind="pad", reference="J2", pin="1", plating="PTH", net="+BATT"),
    ),
    ("NPTH pad of H4", dict(kind="pad", reference="H4", pin=None, plating="NPTH")),
    ("Reference field of J9", dict(kind="field", reference="J9", field="Reference")),
    ("Value field of POLY (VAL**)", dict(kind="field", reference="POLY", value="VAL**")),
    (
        "Rectangle of J2 on F.Silkscreen",
        dict(kind="graphic", reference="J2", shape="Rectangle", layer="F.Silkscreen"),
    ),
    (
        "Segment of J1 on F.Silkscreen",
        dict(kind="graphic", reference="J1", shape="Segment", layer="F.Silkscreen"),
    ),
    ("Polygon [<no net>] of POLY on F.Cu", dict(kind="graphic", reference="POLY", net="<no net>")),
    ("Segment on Edge.Cuts", dict(kind="graphic", reference=None, layer="Edge.Cuts")),
    (
        "Track [Net-(U2-PS/SYNC)] on F.Cu, length 0.9700 mm",
        dict(kind="track", reference=None, net="Net-(U2-PS/SYNC)", layer="F.Cu"),
    ),
    ("Via [/SD_CS] on F.Cu - B.Cu", dict(kind="via", reference=None, net="/SD_CS")),
    (
        "Zone 'GND' [GND] on F.Cu and B.Cu, priority 0",
        dict(kind="zone", reference=None, zone="GND", net="GND", layer="F.Cu and B.Cu"),
    ),
    (
        "Zone [GND] on bottom_copper, priority 0",
        dict(kind="zone", net="GND", layer="bottom_copper"),
    ),
]


class TestParseItem:
    @pytest.mark.parametrize(("description", "expected"), FORMATS, ids=[f[0] for f in FORMATS])
    def test_known_formats(self, description, expected):
        parsed = parse_item(description)
        assert {k: parsed.get(k) for k in expected} == expected
        assert {"kind", "reference", "pin", "pin_name", "pin_type"} <= parsed.keys()

    def test_every_recorded_item_has_a_known_shape(self):
        known = {"pin", "symbol", "pad", "footprint", "field", "graphic", "track", "via", "zone"}
        reports = [
            *(parse_erc_report(report(n)) for n in ("erc", "relinked_erc")),
            *(parse_drc_report(report(n)) for n in ("drc", "j2_edge_drc", "relinked_drc")),
            *(parse_drc_report(p.read_text("utf-8")) for p in PCB_RECORDINGS.glob("*.drc.json")),
        ]
        descriptions = {
            str(item["description"])
            for found in reports
            for v in found
            for item in v.get("items", [])
        }
        assert len(descriptions) > 40
        unknown = sorted(d for d in descriptions if parse_item(d)["kind"] not in known)
        assert unknown == []

    def test_an_unknown_shape_keeps_its_owner(self):
        parsed = parse_item("Text box of U7 on F.Fab")
        assert parsed["reference"] == "U7"

    def test_violation_details_adds_items_and_origin(self):
        finding = violation_details(
            erc()[0], {"U5": "Sensor_Motion:LSM6DSL", "#FLG06": "power:PWR_FLAG"}
        )
        assert finding["origin"] == DESIGN
        assert [i["lib_id"] for i in finding["items"]] == [
            "Sensor_Motion:LSM6DSL",
            "power:PWR_FLAG",
        ]
        # The report's own keys stay.
        assert {"description", "pos", "uuid"} <= finding["items"][0].keys()


class TestOrigin:
    def test_library_configuration_findings_are_environment(self):
        by_type = {v["type"]: finding_origin(v) for v in erc("relinked_erc")}
        assert by_type["lib_symbol_issues"] == ENVIRONMENT
        assert by_type["footprint_link_issues"] == ENVIRONMENT
        assert by_type["pin_to_pin"] == DESIGN

    def test_a_part_missing_from_a_configured_library_is_design(self):
        finding = {
            "type": "lib_footprint_issues",
            "description": "Footprint 'C_0402_NOPE' not found in library 'Capacitor_SMD'",
        }
        assert finding_origin(finding) == DESIGN

    def test_erc_check_reports_origin(self):
        ctx = FakeContext(
            schematic=load_schematic(design(".kicad_sch")),
            cli_results=cli_result(stdout=report("relinked_erc")),
        )
        result = ERCRunCheck(severity="all").run(ctx)
        origins = {v["type"]: v["origin"] for v in result.details["violations"]}
        assert origins["lib_symbol_issues"] == ENVIRONMENT
        assert origins["pin_to_pin"] == DESIGN


class TestErcAllowance:
    def test_flag_against_bidirectional_is_a_filter(self):
        # Confirms the fixtures' ERC baseline: both findings are an LSM6DSL
        # bidirectional pin against a PWR_FLAG power output.
        ctx = FakeContext(
            schematic=load_schematic(design(".kicad_sch")),
            cli_results=cli_result(stdout=report("erc")),
        )
        result = ERCRunCheck(severity="all").run(ctx)
        found = result.details["violations"]
        assert len(found) == 2
        assert all(flag_on_bidirectional(f) for f in found)
        assert {i["lib_id"] for f in found for i in f["items"]} == {
            "Sensor_Motion:LSM6DSL",
            "power:PWR_FLAG",
        }

    def test_the_filter_rejects_other_pin_pairs(self):
        finding = violation_details(erc()[0], {"U5": "power:PWR_FLAG", "#FLG06": "power:PWR_FLAG"})
        finding["items"][0]["pin_type"] = "Output"
        assert not flag_on_bidirectional(finding)


# ----------------------------------------------------------- 4. ownership (B)


class TestOwnership:
    def test_a_pairing_with_the_board_edge_is_not_owned(self):
        silk = drc()[0]
        assert silk["type"] == "silk_edge_clearance"
        assert finding_references(silk) == [None, "J2"]
        assert not is_owned_by(silk, {"J2"})

    def test_a_pairing_with_another_part_needs_both(self):
        overlap = parse_drc_report((PCB_RECORDINGS / "overlap.drc.json").read_text("utf-8"))
        courtyards = next(v for v in overlap if v["type"] == "courtyards_overlap")
        refs = set(finding_references(courtyards))
        assert len(refs) == 2
        assert not is_owned_by(courtyards, refs - {sorted(refs)[0]})
        assert is_owned_by(courtyards, refs)

    def test_an_itemless_finding_is_owned_by_the_part_it_names(self):
        parity = parse_drc_report((PCB_RECORDINGS / "parity.drc.json").read_text("utf-8"))
        missing = next(v for v in parity if v["type"] == "missing_footprint")
        assert missing["items"] == []
        assert finding_references(missing) == ["R13"]
        assert findings_owned_by(parity, {"R13"}) == [missing]

    def test_an_unconnected_link_is_never_owned(self):
        unrouted = parse_drc_report((PCB_RECORDINGS / "unrouted.drc.json").read_text("utf-8"))
        link = next(v for v in unrouted if v["type"] == "unconnected_items")
        assert finding_references(link) == ["U1", "J3"]
        assert not is_owned_by(link, {"U1", "J3"})
        # KiCad may report the same missing link between two pads of one part
        # ("J1 pad A4 to J1 pad A9"); the recorded end on J3 is moved onto U1.
        same_part = {
            **link,
            "items": [
                link["items"][0],
                {**link["items"][1], "description": "Pad 16 [/SD_CLK] of U1 on F.Cu"},
            ],
        }
        assert finding_references(same_part) == ["U1", "U1"]
        assert not is_owned_by(same_part, {"U1"})
        assert findings_owned_by([same_part], {"U1"}) == []

    def test_drc_check_keeps_unconnected_links_of_excluded_parts(self):
        unrouted = (PCB_RECORDINGS / "unrouted.drc.json").read_text("utf-8")
        ctx = FakeContext(board=object(), cli_results=cli_result(stdout=unrouted))
        full = DRCRunCheck(severity="all").run(ctx)
        ctx = FakeContext(board=object(), cli_results=cli_result(stdout=unrouted))
        trimmed = DRCRunCheck(severity="all", exclude_owned_by=["U1", "J3"]).run(ctx)
        links = [v for v in parse_drc_report(unrouted) if v["type"] == "unconnected_items"]
        assert links
        assert trimmed.status == full.status
        kept = trimmed.details["filtered_violations"]
        assert kept >= len(links)

    def test_erc_ownership_counts_the_flag(self):
        pin = erc()[0]
        assert not is_owned_by(pin, {"U5"})
        assert is_owned_by(pin, {"U5", "#FLG06"})

    def test_works_on_projected_findings(self):
        projected = [violation_details(v) for v in drc()]
        assert findings_owned_by(projected, {"J2"}) == []

    def test_drc_check_excludes_owned_findings(self):
        overlap = (PCB_RECORDINGS / "overlap.drc.json").read_text("utf-8")
        entries = parse_drc_report(overlap)
        # The C5/D1 courtyard overlap, and D1's silkscreen over C5's pad.
        pair = [v for v in entries if set(finding_references(v)) == {"C5", "D1"}]
        ctx = FakeContext(board=object(), cli_results=cli_result(stdout=overlap))
        full = DRCRunCheck(severity="all").run(ctx)
        ctx = FakeContext(board=object(), cli_results=cli_result(stdout=overlap))
        trimmed = DRCRunCheck(severity="all", exclude_owned_by=["C5", "D1"]).run(ctx)
        assert len(pair) == 2
        assert trimmed.details["owned_excluded"] == len(pair)
        assert trimmed.details["filtered_violations"] == full.details["filtered_violations"] - len(
            pair
        )


# ------------------------------------------------------- 5. parity tags (#3)


@pytest.fixture(scope="module")
def findings() -> dict[str, dict[str, Any]]:
    """The parity findings of the relinked copy, by reference."""
    board = design(".kicad_pcb")
    ctx = FakeContext(
        board=load_board(board),
        cli_results=cli_result(stdout=report("relinked_drc")),
        board_path=str(board),
    )
    result = SchematicParityCheck().run(ctx)
    assert result.status == TestStatus.FAIL
    return {f["reference"]: f for f in result.details["findings"]}


class TestParityTags:
    def test_a_mounting_hole_without_a_symbol_is_board_only_mechanical(self, findings):
        hole = findings["H1"]
        assert hole["type"] == "extra_footprint"
        assert hole["tags"] == ["board_only_mechanical"]
        assert hole["footprint_attributes"]["exclude_from_bom"] is True
        assert hole["pad_numbers"] == []

    def test_a_missing_nickname_is_nickname_only(self, findings):
        r1 = findings["R1"]
        assert r1["tags"] == ["nickname_only"]
        assert r1["board_footprint"] == "R_0402_1005Metric"
        assert r1["symbol_footprint"] == "Resistor_SMD:R_0402_1005Metric"

    def test_a_different_nickname_is_nickname_only(self, findings):
        assert findings["D1"]["tags"] == ["nickname_only"]

    def test_a_field_difference_is_field_only(self, findings):
        c1 = findings["C1"]
        assert c1["type"] == "footprint_symbol_field_mismatch"
        assert c1["tags"] == ["field_only"]
        assert c1["field"] == "LCSC"

    def test_findings_keep_their_drc_fields(self, findings):
        assert findings["H1"]["source"] == "schematic_parity"
        assert findings["H1"]["items"][0]["kind"] == "footprint"

    def test_an_electrical_part_is_not_mechanical(self):
        board = load_board(design(".kicad_pcb"))
        by_ref = {fp.reference: fp for fp in board.get_footprints()}
        assert _mechanical(by_ref["H1"])
        assert not _mechanical(by_ref["R1"])
        assert not _mechanical(by_ref["J1"])


# ----------------------------------------------- 6. project suppressions (A)


class TestProjectSuppressions:
    def test_reads_the_rules_a_real_project_ignores(self):
        found = ProjectSuppressions.read(design(".kicad_pro"), "drc")
        assert found.severities["pth_inside_courtyard"] == "ignore"
        erc_found = ProjectSuppressions.read(design(".kicad_pro"), "erc")
        assert erc_found.severities["single_global_label"] == "ignore"

    def test_a_missing_project_silences_nothing(self, tmp_path):
        assert ProjectSuppressions.read(None, "drc") == ProjectSuppressions()
        assert ProjectSuppressions.read(tmp_path / "none.kicad_pro", "erc") == ProjectSuppressions()

    def test_a_rule_set_to_ignore_hid_its_findings(self):
        silk = drc()[0]
        rules = ProjectSuppressions(severities={"silk_edge_clearance": "ignore"})
        assert rules.hidden_by(silk) == ("silk_edge_clearance", "rule")
        assert ProjectSuppressions().hidden_by(silk) == (None, None)

    def test_an_exclusion_naming_the_items_hid_the_finding(self):
        silk = drc()[0]
        uuids = [i["uuid"] for i in silk["items"]]
        # KiCad's PCB_MARKER serialisation: type|x_nm|y_nm|main uuid|aux uuid.
        key = f"silk_edge_clearance|145000000|150000000|{uuids[0]}|{uuids[1]}"
        other = f"silk_edge_clearance|0|0|{uuids[0]}|00000000-0000-0000-0000-000000000001"
        found = ProjectSuppressions(exclusions=(other, key))
        assert found.hidden_by(silk) == (key, "exclusion")
        tagged = found.tag([silk])[0]
        assert tagged["hidden_kind"] == "exclusion"
        assert tagged["project_severity"] is None

    def test_exclusions_with_comments_are_read(self, tmp_path):
        folder = copy_case(tmp_path)
        uuids = [i["uuid"] for i in drc()[0]["items"]]
        key = f"silk_edge_clearance|0|0|{uuids[0]}|{uuids[1]}"

        def add(data):
            data["board"]["design_settings"]["drc_exclusions"] = [[key, "reviewed"]]

        edit_project(folder, add)
        found = ProjectSuppressions.read(folder / "imu.kicad_pro", "drc")
        assert found.exclusions == (key,)

    def test_the_copy_lets_everything_through_and_the_original_stays(self, tmp_path):
        folder = copy_case(tmp_path)
        (folder / "local.pretty").mkdir()
        (folder / "fp-lib-table").write_text(
            '(fp_lib_table (lib (name "Local")(type "KiCad")(uri "${KIPRJMOD}/local.pretty")))\n',
            encoding="utf-8",
        )

        def exclude(data):
            data["board"]["design_settings"]["drc_exclusions"] = ["silk_edge_clearance|0|0"]

        edit_project(folder, exclude)
        project = folder / "imu.kicad_pro"
        before = project.read_bytes()
        with unsuppressed_project(
            folder / "imu.kicad_pcb",
            "drc",
            reset_severities=True,
            drop_exclusions=True,
            severity_overrides={"pth_inside_courtyard": "error"},
        ) as (board, suppressions):
            copied = json.loads(board.with_suffix(".kicad_pro").read_text("utf-8"))
            settings = copied["board"]["design_settings"]
            assert settings["rule_severities"] == {"pth_inside_courtyard": "error"}
            assert "drc_exclusions" not in settings
            # ERC settings are left alone on a DRC copy.
            assert copied["erc"]["rule_severities"]["single_global_label"] == "ignore"
            assert board.is_file()
            assert board.with_suffix(".kicad_sch").is_file()
            assert (board.parent / "local.pretty").is_dir()
            table = (board.parent / "fp-lib-table").read_text("utf-8")
            assert "${KIPRJMOD}" not in table
            assert folder.as_posix() in table
            assert suppressions.severities["pth_inside_courtyard"] == "ignore"
            assert suppressions.exclusions == ("silk_edge_clearance|0|0",)
            root = board.parent
        assert not root.exists()
        assert project.read_bytes() == before

    def test_overrides_are_validated(self):
        with pytest.raises(ValueError, match="severity_overrides"):
            DRCRunCheck(severity_overrides={"clearance": "loud"})
        with pytest.raises(ValueError, match="severity_overrides"):
            ERCRunCheck(severity_overrides={"pin_to_pin": "off"})


# ---------------------------------------------------- integration: real KiCad


def silence(folder: Path, tool: str, rule: str) -> None:
    def edit(data):
        # Some projects have no "erc" section until a rule is changed.
        section = data.setdefault("erc", {}) if tool == "erc" else data["board"]["design_settings"]
        section.setdefault("rule_severities", {})[rule] = "ignore"

    edit_project(folder, edit)


@pytest.mark.integration
class TestWithKicad:
    @pytest.mark.parametrize("variant", ["base", "stubs"])
    def test_erc_runs_with_the_nvidia_gl_vendor_set(
        self, variant, monkeypatch, tmp_path, kicad_cli_required
    ):
        # kicad-cli 10.0.7 segfaults at start-up when this is set.
        folder = copy_case(tmp_path, variant)
        monkeypatch.setenv(GLX, "nvidia")
        ctx = DesignContext(schematic_path=folder / "imu.kicad_sch")
        result = ERCRunCheck(severity="all").run(ctx)
        assert result.status == TestStatus.FAIL, result.message
        found = result.details["violations"]
        assert [f["type"] for f in found] == ["pin_to_pin", "pin_to_pin"]
        assert all(flag_on_bidirectional(f) for f in found)

    def test_drc_runs_with_the_nvidia_gl_vendor_set(
        self, monkeypatch, tmp_path, kicad_cli_required
    ):
        folder = copy_case(tmp_path, "j2_edge")
        monkeypatch.setenv(GLX, "nvidia")
        ctx = DesignContext(board_path=folder / "imu.kicad_pcb")
        result = DRCRunCheck(severity="all").run(ctx)
        assert result.status == TestStatus.FAIL, result.message
        assert [f["type"] for f in result.details["violations"]] == ["silk_edge_clearance"]

    def test_drc_reset_shows_what_an_ignored_rule_hid(self, tmp_path, kicad_cli_required):
        folder = copy_case(tmp_path, "j2_edge")
        silence(folder, "drc", "silk_edge_clearance")
        project = (folder / "imu.kicad_pro").read_bytes()
        ctx = DesignContext(board_path=folder / "imu.kicad_pcb")

        assert DRCRunCheck(severity="all").run(ctx).status == TestStatus.PASS
        result = DRCRunCheck(severity="all", reset_severities=True).run(ctx)

        assert result.status == TestStatus.FAIL, result.message
        found = result.details["violations"]
        assert [(f["type"], f["hidden_by"], f["hidden_kind"]) for f in found] == [
            ("silk_edge_clearance", "silk_edge_clearance", "rule")
        ]
        assert all(f["project_severity"] == "ignore" for f in found)
        assert result.details["hidden_violations"] == 1
        assert (folder / "imu.kicad_pro").read_bytes() == project

    def test_erc_reset_shows_what_an_ignored_rule_hid(self, tmp_path, kicad_cli_required):
        folder = copy_case(tmp_path)
        silence(folder, "erc", "pin_to_pin")
        ctx = DesignContext(schematic_path=folder / "imu.kicad_sch")

        assert ERCRunCheck(severity="all").run(ctx).status == TestStatus.PASS
        result = ERCRunCheck(severity="all", reset_severities=True).run(ctx)

        assert result.status == TestStatus.FAIL, result.message
        found = result.details["violations"]
        assert [(f["hidden_by"], f["hidden_kind"]) for f in found] == [("pin_to_pin", "rule")] * 2
        assert all(flag_on_bidirectional(f) for f in found)

    def test_turning_on_courtyard_rules_a_project_ignores(self, kicad_cli_required):
        # The project ignores pth/npth_inside_courtyard, as KiCad's stock
        # templates do; turning them on finds nothing on this board.
        ctx = DesignContext(board_path=design(".kicad_pcb"))
        result = DRCRunCheck(
            severity="all",
            reset_severities=True,
            severity_overrides={"pth_inside_courtyard": "error", "npth_inside_courtyard": "error"},
        ).run(ctx)
        assert result.status == TestStatus.PASS, result.message
        assert result.details["hidden_violations"] == 0

    def test_a_pad_against_its_own_hole_is_owned(self, tmp_path, kicad_cli_required):
        folder = copy_case(tmp_path)

        def tighten(data):
            data["board"]["design_settings"]["rules"]["min_hole_clearance"] = 0.6

        edit_project(folder, tighten)
        ctx = DesignContext(board_path=folder / "imu.kicad_pcb")
        check = DRCRunCheck(severity="all", include_types=["hole_clearance"])
        found = check.run(ctx).details["violations"]
        own = [f for f in found if is_owned_by(f, {"J1"})]
        # The USB-C receptacle's pads against its own unplated pegs.
        assert own
        assert all(
            [i["kind"] for i in f["items"]] == ["pad", "pad"]
            and any(i.get("plating") == "NPTH" for i in f["items"])
            for f in own
        )
        # Its pegs against a zone are not the receptacle's alone.
        assert any("J1" in finding_references(f) and not is_owned_by(f, {"J1"}) for f in found)

        trimmed = DRCRunCheck(
            severity="all", include_types=["hole_clearance"], exclude_owned_by=["J1"]
        ).run(ctx)
        assert trimmed.details["owned_excluded"] == len(own)
        assert trimmed.details["filtered_violations"] == len(found) - len(own)

    def test_parity_tags_on_an_edited_copy(self, tmp_path, kicad_cli_required):
        folder = copy_case(tmp_path)
        sch = folder / "imu.kicad_sch"
        pcb = folder / "imu.kicad_pcb"
        text = sch.read_text("utf-8")
        start = text.rindex("\n\t(symbol\n", 0, text.index('(property "Reference" "H1"'))
        end = text.index("\n\t(symbol\n", start + 1)
        sch.write_text(text[:start] + text[end:], encoding="utf-8")  # H1 loses its symbol
        board = pcb.read_text("utf-8")
        board = board.replace(
            '(footprint "Resistor_SMD:R_0402_1005Metric"', '(footprint "R_0402_1005Metric"'
        )
        pcb.write_text(board, encoding="utf-8")

        result = SchematicParityCheck().run(DesignContext(board_path=pcb))
        findings = {f["reference"]: f for f in result.details["findings"]}
        assert findings["H1"]["tags"] == ["board_only_mechanical"]
        resistors = [f for ref, f in findings.items() if re.fullmatch(r"R\d+", ref)]
        assert resistors
        assert all(f["tags"] == ["nickname_only"] for f in resistors)
        assert set(findings) == {"H1", *(f["reference"] for f in resistors)}
