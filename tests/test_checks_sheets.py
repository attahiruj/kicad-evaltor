"""The four checks on the sheet tree.

Each of these answers something ``kicad-cli sch erc`` does not: whether the files a
design points at are all present, whether a sheet's pins have matching labels,
whether the tree loops, and whether a name or page number is used twice. The tests
build the shapes directly, because the interesting cases are the ones nobody draws
on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_evaltor.checks.base import CheckCategory, TestStatus as Status
from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.core.context import DesignContext
from conftest import (
    FakeContext,
    FakeSchematic,
    hierarchical_label,
    schematic_file,
    sheet_symbol,
)

FILE_MISSING = "sch.sheet.file_missing"
PIN_MISMATCH = "sch.sheet.pin_mismatch"
CYCLE = "sch.sheet.cycle"
COLLISION = "sch.sheet.name_or_page_collision"
ALL = [FILE_MISSING, PIN_MISMATCH, CYCLE, COLLISION]

MATCHED = hierarchical_label("+3.3V") + hierarchical_label("GND")


def run(check_id: str, root: Path, **params):
    return CheckRegistry.create(check_id, **params).run(DesignContext(schematic_path=root))


@pytest.fixture
def sound(hierarchical_design):
    """A root whose two links both resolve and both pair their pins with labels."""

    def build():
        return hierarchical_design({"power": MATCHED, "main": MATCHED})

    return build


class TestRegistration:
    @pytest.mark.parametrize("check_id", ALL)
    def test_check_is_registered(self, check_id):
        assert CheckRegistry.get(check_id).id == check_id

    @pytest.mark.parametrize("check_id", ALL)
    def test_check_is_described(self, check_id):
        check = CheckRegistry.get(check_id)
        assert check.name
        assert check.description

    @pytest.mark.parametrize("check_id", ALL)
    def test_check_is_in_the_schematic_category(self, check_id):
        assert CheckRegistry.get(check_id).category is CheckCategory.SCHEMATIC


class TestSkips:
    @pytest.mark.parametrize("check_id", ALL)
    def test_checks_skip_without_a_schematic(self, check_id):
        ctx = DesignContext(schematic_path="does/not/exist.kicad_sch")

        result = CheckRegistry.create(check_id).run(ctx)

        assert result.status is Status.SKIP

    @pytest.mark.parametrize("check_id", ALL)
    def test_a_schematic_that_is_not_file_backed_is_not_this_checks_business(self, check_id):
        # A live KiCad instance is another source of symbols; a .kicad_sch file is
        # the only one that can say anything about sheet links.
        ctx = FakeContext(schematic=FakeSchematic())

        result = CheckRegistry.create(check_id).run(ctx)

        assert result.status is Status.SKIP
        assert "directly" in result.message


class TestSheetFileMissing:
    def test_a_design_whose_links_all_resolve_passes(self, sound):
        result = run(FILE_MISSING, sound())

        assert result.status is Status.PASS
        assert result.details["checked"] == 2
        assert "2 sheet links resolve" in result.message

    def test_a_single_link_is_counted_in_the_singular(self, hierarchical_design):
        root = hierarchical_design({"power": MATCHED})

        assert "1 sheet link resolves" in run(FILE_MISSING, root).message

    def test_a_link_to_a_file_that_was_never_committed_fails(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(FILE_MISSING, root)

        assert result.status is Status.FAIL
        assert result.details["count"] == 1

    def test_the_finding_names_the_sheet_the_file_and_where_it_looked(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        finding = run(FILE_MISSING, root).details["missing"][0]

        assert finding["sheet"] == "gone"
        assert finding["filename"] == "gone.kicad_sch"
        assert any(c.endswith("gone.kicad_sch") for c in finding["candidates"])

    def test_a_missing_file_a_folder_and_a_junk_file_are_three_different_reasons(self, tmp_path):
        (tmp_path / "folder.kicad_sch").mkdir()
        (tmp_path / "junk.kicad_sch").write_text("this is not a schematic")
        body = (
            sheet_symbol("gone", "gone.kicad_sch", uuid="a")
            + sheet_symbol("folder", "folder.kicad_sch", uuid="b")
            + sheet_symbol("junk", "junk.kicad_sch", uuid="c")
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        findings = run(FILE_MISSING, root).details["missing"]

        assert [f["reason"] for f in findings] == [
            "no candidate path exists",
            "a directory, not a schematic file",
            "file does not parse as a .kicad_sch",
        ]

    def test_an_unset_sheetfile_is_reported_in_its_own_words(self, tmp_path):
        body = sheet_symbol("blank", "", uuid="blank-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(FILE_MISSING, root)

        assert result.status is Status.FAIL
        assert result.details["missing"][0]["reason"] == "Sheetfile is empty"

    def test_the_message_leads_with_the_first_failure(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        assert "gone.kicad_sch" in run(FILE_MISSING, root).message

    def test_one_broken_link_does_not_hide_the_rest_of_the_design(self, tmp_path, sound):
        # The report is about links, and every sheet that did load still counts.
        schematic_file(tmp_path / "ok.kicad_sch", uuid="ok-doc", body=MATCHED)
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="a") + sheet_symbol(
            "ok", "ok.kicad_sch", uuid="b"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(FILE_MISSING, root)

        assert result.status is Status.FAIL
        assert result.details["checked"] == 2

    def test_the_root_file_itself_is_not_a_link(self, sound):
        # The root is where links live, so a check about links says nothing of it.
        assert run(FILE_MISSING, sound()).details["checked"] == 2


class TestSheetPinMismatch:
    def test_matching_pins_and_labels_pass(self, sound):
        result = run(PIN_MISMATCH, sound())

        assert result.status is Status.PASS
        assert result.details["checked"] == 2

    def test_a_sheet_whose_file_was_never_loaded_is_not_counted(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        assert run(PIN_MISMATCH, root).details["checked"] == 0

    def test_a_pin_with_no_label_is_reported(self, hierarchical_design):
        # The child is missing GND's label, so GND simply stops at the pin and
        # KiCad shows no error at all.
        root = hierarchical_design({"power": hierarchical_label("+3.3V")})

        result = run(PIN_MISMATCH, root)

        assert result.status is Status.FAIL
        assert result.details["pins_without_label"] == [{"sheet": "power", "pin": "GND"}]

    def test_a_label_with_no_pin_is_reported(self, hierarchical_design):
        root = hierarchical_design({"power": MATCHED + hierarchical_label("SDA")})

        result = run(PIN_MISMATCH, root)

        assert result.details["labels_without_pin"] == [{"sheet": "power", "label": "SDA"}]

    def test_both_directions_are_reported_together(self, hierarchical_design):
        root = hierarchical_design({"power": hierarchical_label("SDA")})

        result = run(PIN_MISMATCH, root)

        assert result.details["count"] == 3
        assert {f["pin"] for f in result.details["pins_without_label"]} == {"+3.3V", "GND"}
        assert {f["label"] for f in result.details["labels_without_pin"]} == {"SDA"}

    def test_a_sheet_with_no_pins_and_no_labels_is_fine(self, hierarchical_design):
        # Nothing crossing the boundary is a legitimate design, not a mismatch.
        root = hierarchical_design({"blank": {"body": "", "pins": ()}})

        assert run(PIN_MISMATCH, root).status is Status.PASS

    def test_the_finding_names_the_sheet_it_belongs_to(self, hierarchical_design):
        root = hierarchical_design({"power": MATCHED, "main": hierarchical_label("+3.3V")})

        sheets = {f["sheet"] for f in run(PIN_MISMATCH, root).details["pins_without_label"]}

        assert sheets == {"main"}

    def test_pin_shape_is_left_to_erc(self, hierarchical_design):
        # Every builder here writes "input". A mismatch with the label's shape is
        # ERC's business, and duplicating it would only be a worse copy.
        root = hierarchical_design({"power": MATCHED})

        assert run(PIN_MISMATCH, root).status is Status.PASS


class TestSheetCycle:
    def test_a_tree_with_no_loops_passes(self, sound):
        result = run(CYCLE, sound())

        assert result.status is Status.PASS
        assert result.details["checked"] == 3
        assert "No cycles across 3 sheets" in result.message

    def test_a_sheet_linking_back_to_its_ancestor_fails(self, tmp_path):
        schematic_file(tmp_path / "mid.kicad_sch", uuid="mid-doc", body="")
        root = schematic_file(
            tmp_path / "root.kicad_sch",
            uuid="root-doc",
            body=sheet_symbol("mid", "mid.kicad_sch", uuid="mid-uuid"),
        )
        (tmp_path / "mid.kicad_sch").write_text(
            (tmp_path / "mid.kicad_sch")
            .read_text(encoding="utf-8")
            .replace(
                "\t(embedded_fonts",
                sheet_symbol("back", "root.kicad_sch", uuid="back-uuid") + "\t(embedded_fonts",
            ),
            encoding="utf-8",
        )

        result = run(CYCLE, root)

        assert result.status is Status.FAIL
        assert result.details["cycles"] == [{"sheet": "back", "chain": ["root", "mid", "back"]}]

    def test_the_message_reads_the_loop_out(self, tmp_path):
        schematic_file(tmp_path / "mid.kicad_sch", uuid="mid-doc", body="")
        root = schematic_file(
            tmp_path / "root.kicad_sch",
            uuid="root-doc",
            body=sheet_symbol("mid", "mid.kicad_sch", uuid="mid-uuid"),
        )
        (tmp_path / "mid.kicad_sch").write_text(
            (tmp_path / "mid.kicad_sch")
            .read_text(encoding="utf-8")
            .replace(
                "\t(embedded_fonts",
                sheet_symbol("back", "root.kicad_sch", uuid="back-uuid") + "\t(embedded_fonts",
            ),
            encoding="utf-8",
        )

        assert "root -> mid -> back" in run(CYCLE, root).message

    def test_one_file_used_twice_is_not_a_cycle(self, tmp_path):
        # KiCad does this deliberately: the same block under two sheet symbols.
        schematic_file(tmp_path / "shared.kicad_sch", uuid="shared-doc", body=MATCHED)
        body = sheet_symbol("first", "shared.kicad_sch", uuid="first-uuid") + sheet_symbol(
            "second", "shared.kicad_sch", uuid="second-uuid"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(CYCLE, root)

        assert result.status is Status.PASS
        assert result.details["checked"] == 3

    def test_a_missing_link_is_not_a_cycle(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        assert run(CYCLE, root).status is Status.PASS


class TestNameOrPageCollision:
    def test_unique_names_and_pages_pass(self, sound):
        result = run(COLLISION, sound())

        assert result.status is Status.PASS
        assert result.details["checked"] == 3

    def test_two_siblings_with_one_name_fail(self, tmp_path):
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("Power", "a.kicad_sch", uuid="u1") + sheet_symbol(
            "Power", "b.kicad_sch", uuid="u2"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(COLLISION, root)

        assert result.status is Status.FAIL
        assert result.details["duplicate_names"] == [{"sheet": "root", "name": "Power", "count": 2}]

    def test_one_name_in_two_different_subtrees_is_fine(self, tmp_path):
        # Naming the same block twice in two branches is how a designer reuses it,
        # and only siblings are ambiguous: no parent here holds two "Block" sheets.
        for name in ("left", "right", "block"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        for name, page in (("left", "3"), ("right", "5")):
            link = sheet_symbol("Block", "block.kicad_sch", uuid=f"block-{name}", page=page)
            (tmp_path / f"{name}.kicad_sch").write_text(
                (tmp_path / f"{name}.kicad_sch")
                .read_text(encoding="utf-8")
                .replace("\t(embedded_fonts", link + "\t(embedded_fonts"),
                encoding="utf-8",
            )
        body = sheet_symbol("left", "left.kicad_sch", uuid="left-uuid", page="2") + sheet_symbol(
            "right", "right.kicad_sch", uuid="right-uuid", page="4"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(COLLISION, root)

        assert result.details["duplicate_names"] == []
        assert result.details["duplicate_pages"] == []

    def test_two_sheets_claiming_one_page_number_fail(self, tmp_path):
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("A", "a.kicad_sch", uuid="u1", page="2") + sheet_symbol(
            "B", "b.kicad_sch", uuid="u2", page="2"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(COLLISION, root)

        assert result.status is Status.FAIL
        assert result.details["duplicate_pages"] == [{"page": "2", "sheets": ["A", "B"]}]

    def test_distinct_page_numbers_pass(self, tmp_path):
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("A", "a.kicad_sch", uuid="u1", page="2") + sheet_symbol(
            "B", "b.kicad_sch", uuid="u2", page="3"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        assert run(COLLISION, root).status is Status.PASS

    def test_a_sheet_with_no_page_number_is_reported_when_the_root_tracks_pages(self, tmp_path):
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("A", "a.kicad_sch", uuid="u1", page="2") + sheet_symbol(
            "B", "b.kicad_sch", uuid="u2"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(COLLISION, root)

        assert result.status is Status.FAIL
        assert result.details["unnumbered"] == ["B"]

    def test_a_missing_page_number_is_fine_when_the_root_never_tracked_pages(self, tmp_path):
        # No (sheet_instances ...) means the file predates page tracking, and
        # demanding a page number would be inventing a requirement.
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("A", "a.kicad_sch", uuid="u1") + sheet_symbol(
            "B", "b.kicad_sch", uuid="u2"
        )
        root = schematic_file(
            tmp_path / "root.kicad_sch", uuid="root", body=body, sheet_instances=False
        )

        assert run(COLLISION, root).status is Status.PASS

    def test_names_and_pages_are_reported_together(self, tmp_path):
        for name in ("a", "b"):
            schematic_file(tmp_path / f"{name}.kicad_sch", uuid=f"{name}-doc", body="")
        body = sheet_symbol("Same", "a.kicad_sch", uuid="u1", page="2") + sheet_symbol(
            "Same", "b.kicad_sch", uuid="u2", page="2"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        result = run(COLLISION, root)

        assert result.details["count"] == 2
        assert "duplicated sheet name(s)" in result.message
        assert "duplicated page number(s)" in result.message

    def test_the_root_sheets_own_page_is_not_checked_against_the_children(self, tmp_path):
        # KiCad numbers the root 1 and the children from 2, but a design whose
        # root claims 5 must not collide with a child that also claims 5 by accident
        # of the builder -- the root's page comes from a different section.
        schematic_file(tmp_path / "a.kicad_sch", uuid="a-doc", body="")
        body = sheet_symbol("A", "a.kicad_sch", uuid="u1", page="5")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        assert run(COLLISION, root).status is Status.PASS
