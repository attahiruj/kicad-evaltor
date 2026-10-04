"""Reading the ``(sheet ...)``, ``(pin ...)`` and ``(hierarchical_label ...)`` nodes.

Everything here is a literal s-expression rather than a file, because these are
the shapes that matter and a test should not have to build a whole project to see
one of them. The one thing that does need a disk is path resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_evaltor.sexpr import parse
from kicad_evaltor.sheets import (
    ROOT_PATH,
    HierarchicalLabel,
    Sheet,
    SheetPin,
    join_sheet_path,
    label_from_node,
    pin_from_node,
    resolve_sheet_path,
    sheet_from_node,
)
from conftest import schematic_file

SHEET_NODE = (
    "(sheet\n"
    "\t(at 50.8 25.4)\n"
    "\t(size 38.1 25.4)\n"
    '\t(uuid "aaaa1111-2222-3333-4444-555555555555")\n'
    '\t(property "Sheetname" "Power"\n'
    "\t\t(at 69.85 27.94 0)\n"
    "\t\t(effects (font (size 1.27 1.27)) (justify bottom))\n"
    "\t)\n"
    '\t(property "Sheetfile" "power.kicad_sch"\n'
    "\t\t(at 69.85 48.26 0)\n"
    "\t\t(hide yes)\n"
    "\t\t(effects (font (size 1.27 1.27)) (justify top))\n"
    "\t)\n"
    '\t(pin "+3.3V" input\n'
    "\t\t(at 58.42 50.8 270)\n"
    '\t\t(uuid "bbbb1111-2222-3333-4444-555555555555")\n'
    "\t\t(effects (font (size 1.27 1.27)) (justify right))\n"
    "\t)\n"
    '\t(pin "GND" passive\n'
    "\t\t(at 74.42 50.8 270)\n"
    '\t\t(uuid "cccc1111-2222-3333-4444-555555555555")\n'
    "\t\t(effects (font (size 1.27 1.27)) (justify right))\n"
    "\t)\n"
    "\t(instances\n"
    '\t\t(project "proj"\n'
    '\t\t\t(path "/bbbb2222"\n'
    '\t\t\t\t(page "4")\n'
    "\t\t\t)\n"
    "\t\t)\n"
    "\t)\n"
    "\t)\n"
)


def read(text: str) -> list:
    node = parse(text.strip())
    assert isinstance(node, list)
    return node


def sheet_node(tmp_path: Path, text: str = SHEET_NODE, **kwargs) -> Sheet:
    return sheet_from_node(
        read(text), base_dir=tmp_path, parent_dir=tmp_path, parent_path=ROOT_PATH, **kwargs
    )


class TestSheetParsing:
    def test_the_two_properties_become_the_name_and_the_file(self, tmp_path):
        sheet = sheet_node(tmp_path)

        assert sheet.name == "Power"
        assert sheet.filename == "power.kicad_sch"
        assert sheet.stem == "power"

    def test_position_and_size_are_read_as_millimetres(self, tmp_path):
        sheet = sheet_node(tmp_path)

        assert (sheet.x, sheet.y) == (50.8, 25.4)
        assert (sheet.width, sheet.height) == (38.1, 25.4)

    def test_the_uuid_is_kept_verbatim(self, tmp_path):
        assert sheet_node(tmp_path).uuid == "aaaa1111-2222-3333-4444-555555555555"

    def test_the_page_number_is_read_off_the_path_not_the_project(self, tmp_path):
        # KiCad nests (page ...) inside (path ...) because one project can
        # instantiate a file more than once and each instance is numbered alone.
        sheet = sheet_node(tmp_path)

        assert sheet.pages == {"proj": "4"}

    def test_a_sheet_with_no_page_number_reports_an_empty_one(self, tmp_path):
        # An instance KiCad never numbered: present, but with nothing to report.
        text = SHEET_NODE.replace('\t\t\t\t(page "4")\n', "")

        assert sheet_node(tmp_path, text).pages == {"proj": ""}

    def test_a_sheet_with_no_instances_at_all_reports_no_pages(self, tmp_path):
        text = SHEET_NODE[: SHEET_NODE.index("\t(instances")] + "\t)\n"

        assert sheet_node(tmp_path, text).pages == {}

    def test_the_instance_path_is_the_parent_path_plus_the_uuid(self, tmp_path):
        assert sheet_node(tmp_path).path == ("/aaaa1111-2222-3333-4444-555555555555")

    def test_the_instance_path_nests_under_a_parent_that_is_not_the_root(self, tmp_path):
        sheet = sheet_from_node(
            read(SHEET_NODE.strip()),
            base_dir=tmp_path,
            parent_dir=tmp_path,
            parent_path="/root-uuid/parent-uuid",
        )

        assert sheet.path == ("/root-uuid/parent-uuid/aaaa1111-2222-3333-4444-555555555555")

    def test_other_properties_are_kept_whole(self, tmp_path):
        text = SHEET_NODE.replace(
            '(property "Sheetfile" "power.kicad_sch"',
            '(property "Sheetfile" "power.kicad_sch"',
        ).replace(
            '(pin "+3.3V" input',
            '(property "Custom" "hello"\n\t\t\t(at 0 0 0)\n\t\t)\n\t\t(pin "+3.3V" input',
        )

        assert sheet_node(tmp_path, text).properties["Custom"] == "hello"


class TestSheetPins:
    def test_each_pin_keeps_its_name_position_and_rotation(self, tmp_path):
        pins = sheet_node(tmp_path).pins

        assert [p.name for p in pins] == ["+3.3V", "GND"]
        assert (pins[0].x, pins[0].y) == (58.42, 50.8)
        assert pins[0].rotation == 270.0

    def test_the_electrical_shape_is_verbatim(self, tmp_path):
        # KiCad spells these input/output/bidirectional/tri_state/passive and this
        # reader has no business normalising them: ERC owns the electrical answer.
        shapes = [p.shape for p in sheet_node(tmp_path).pins]

        assert shapes == ["input", "passive"]

    def test_a_pin_defaults_to_kicads_text_size(self, tmp_path):
        assert sheet_node(tmp_path).pins[0].size == 1.27

    def test_a_pin_can_be_looked_up_by_name(self, tmp_path):
        sheet = sheet_node(tmp_path)

        gnd = sheet.pin("GND")

        assert gnd is not None
        assert gnd.x == 74.42
        assert sheet.pin("nope") is None

    def test_a_pin_read_without_a_size_still_parses(self, tmp_path):
        node = read('(pin "A" output (at 1 2 0))')

        pin = pin_from_node(node)

        assert pin == SheetPin(name="A", shape="output", x=1.0, y=2.0, rotation=0.0, size=1.27)

    def test_a_sheet_with_no_pins_reports_none(self, tmp_path):
        text = SHEET_NODE[: SHEET_NODE.index("\t(pin ")] + "\t)\n"

        assert sheet_node(tmp_path, text).pins == ()


class TestHierarchicalLabels:
    def test_a_label_keeps_its_name_shape_and_position(self):
        label = label_from_node(read('(hierarchical_label "SDA" (shape output) (at 10 20 180))'))

        assert label == HierarchicalLabel(
            name="SDA", shape="output", x=10.0, y=20.0, rotation=180.0
        )

    def test_the_shape_is_verbatim_rather_than_interpreted(self):
        label = label_from_node(read('(hierarchical_label "A" (shape tri_state) (at 0 0 0))'))

        assert label.shape == "tri_state"

    def test_a_label_without_an_angle_is_not_rotated(self):
        label = label_from_node(read('(hierarchical_label "A" (shape input) (at 5 6))'))

        assert (label.x, label.y, label.rotation) == (5.0, 6.0, 0.0)


class TestPathResolution:
    def test_a_relative_link_is_tried_against_the_project_first(self, tmp_path):
        project = tmp_path / "project"
        child = project / "power.kicad_sch"
        child.parent.mkdir(parents=True)
        child.write_text("x")

        resolved, candidates = resolve_sheet_path(
            "power.kicad_sch", base_dir=project, parent_dir=tmp_path / "elsewhere"
        )

        assert resolved == child
        assert candidates[0] == child

    def test_the_referring_sheets_own_directory_is_the_fallback(self, tmp_path):
        # This is the case that makes a file outside the project folder work.
        outside = tmp_path / "shared"
        child = outside / "shared.kicad_sch"
        child.parent.mkdir(parents=True)
        child.write_text("x")

        resolved, candidates = resolve_sheet_path(
            "../shared/shared.kicad_sch", base_dir=tmp_path / "project", parent_dir=outside
        )

        assert resolved == child
        # Both spellings normalise to the same file, so there is one candidate.
        assert candidates == (child,)

    def test_a_link_that_escapes_the_project_folder_still_resolves(self, tmp_path):
        child = tmp_path / "outside.kicad_sch"
        child.write_text("x")

        resolved, _ = resolve_sheet_path(
            "../outside.kicad_sch", base_dir=tmp_path / "project", parent_dir=tmp_path
        )

        assert resolved == child

    def test_a_windows_spelling_resolves_the_same_as_a_posix_one(self, tmp_path):
        # A design authored on Windows writes backslashes. On POSIX a backslash is
        # an ordinary filename character, so this is a real cross-platform trap.
        outside = tmp_path / "shared"
        child = outside / "shared.kicad_sch"
        child.parent.mkdir(parents=True)
        child.write_text("x")

        posix = resolve_sheet_path(
            "../shared/shared.kicad_sch", base_dir=tmp_path, parent_dir=outside
        )
        windows = resolve_sheet_path(
            "..\\shared\\shared.kicad_sch", base_dir=tmp_path, parent_dir=outside
        )

        assert windows == posix

    def test_an_absolute_link_is_never_joined_onto_a_directory(self, tmp_path):
        child = tmp_path / "absolute.kicad_sch"
        child.write_text("x")

        resolved, candidates = resolve_sheet_path(
            str(child), base_dir=tmp_path / "project", parent_dir=tmp_path
        )

        assert resolved == child
        assert candidates == (child,)

    def test_a_windows_drive_letter_counts_as_absolute_on_every_platform(self, tmp_path):
        resolved, candidates = resolve_sheet_path(
            "C:/design/power.kicad_sch", base_dir=tmp_path, parent_dir=tmp_path
        )

        assert resolved is None
        assert len(candidates) == 1

    def test_candidates_are_deduped_when_both_directories_agree(self, tmp_path):
        resolved, candidates = resolve_sheet_path(
            "power.kicad_sch", base_dir=tmp_path, parent_dir=tmp_path
        )

        assert resolved is None
        assert candidates == (tmp_path / "power.kicad_sch",)

    def test_a_link_to_nothing_reports_where_it_looked(self, tmp_path):
        resolved, candidates = resolve_sheet_path(
            "gone.kicad_sch", base_dir=tmp_path, parent_dir=tmp_path
        )

        assert resolved is None
        assert candidates == (tmp_path / "gone.kicad_sch",)

    def test_an_empty_sheetfile_yields_no_candidates_rather_than_raising(self, tmp_path):
        # An unset filename is a finding for sch.sheet.file_missing to report, not
        # something that should abort a run.
        resolved, candidates = resolve_sheet_path("", base_dir=tmp_path, parent_dir=tmp_path)

        assert resolved is None
        assert candidates == ()

    def test_a_sheet_whose_file_exists_resolves_to_it(self, tmp_path):
        child = schematic_file(tmp_path / "power.kicad_sch", uuid="u")

        sheet = sheet_node(tmp_path)

        assert sheet.resolved == child
        assert sheet.candidates == (child,)


class TestJoinSheetPath:
    def test_the_root_plus_a_uuid(self):
        assert join_sheet_path("/", "abc") == "/abc"

    def test_a_nested_uuid_keeps_every_ancestor(self):
        assert join_sheet_path("/a/b", "c") == "/a/b/c"

    def test_a_trailing_slash_does_not_double_up(self):
        assert join_sheet_path("/a/", "b") == "/a/b"

    def test_a_sheet_with_no_uuid_inherits_its_parents_path(self):
        assert join_sheet_path("/a", "") == "/a"

    def test_an_empty_parent_still_yields_the_root(self):
        assert join_sheet_path("", "a") == "/a"
        assert join_sheet_path("", "") == "/"


class TestDegenerateNodes:
    def test_a_sheet_with_no_properties_still_parses(self, tmp_path):
        sheet = sheet_node(tmp_path, '(sheet (at 0 0) (size 1 1) (uuid "u"))')

        assert sheet.name == ""
        assert sheet.filename == ""
        assert sheet.resolved is None

    def test_missing_numbers_default_to_zero_rather_than_raising(self, tmp_path):
        sheet = sheet_node(tmp_path, '(sheet (uuid "u"))')

        assert (sheet.x, sheet.y, sheet.width, sheet.height) == (0.0, 0.0, 0.0, 0.0)

    @pytest.mark.parametrize(
        "atom",
        ["1.27", "-3", "1e2", "0"],
    )
    def test_sizes_are_read_as_floats(self, atom):
        node = read(f'(pin "A" input (at {atom} 0 0))')

        assert pin_from_node(node).x == float(atom)

    def test_a_where_that_is_not_a_number_is_skipped_rather_than_raising(self):
        node = read('(pin "A" input (at left 5 0))')

        pin = pin_from_node(node)

        assert (pin.x, pin.y, pin.rotation) == (0.0, 0.0, 0.0)
