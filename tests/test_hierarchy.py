"""Walking the sheet tree: order, lookup, failures and cycles.

A design is a tree of files, and this is the code that decides what "the whole
design" means: which files are reachable, in what order, and what happens when
one of them cannot be read. The rule that matters throughout is that a broken
link is a finding rather than an exception -- everything else must keep working,
because that is what lets ``sch.sheet.file_missing`` report the problem instead of
the run dying on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_evaltor.hierarchy import (
    CYCLE,
    MISSING,
    UNREADABLE,
    SchematicTree,
    label_prefix,
    select_sheets,
    select_targets,
    tree_for,
)
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.sheets import ROOT_PATH
from conftest import hierarchical_label, schematic_file, sheet_symbol

SYMBOL = """
\t(symbol (lib_id "Device:R") (at 100 100 0)
\t\t(property "Reference" "R1" (at 100 90 0)
\t\t\t(effects (font (size 1.27 1.27))))
\t\t(uuid "22222222-0000-0000-0000-000000000001")
\t\t(instances
\t\t\t(project "proj"
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001"
\t\t\t\t\t(reference "R1") (unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
"""


@pytest.fixture
def tree(hierarchical_design):
    """A root linking to two children, both of which exist and parse."""

    def build():
        root = hierarchical_design(
            {
                "power": SYMBOL + hierarchical_label("+3.3V") + hierarchical_label("GND"),
                "main": SYMBOL + hierarchical_label("+3.3V") + hierarchical_label("GND"),
            }
        )
        return SchematicTree.load(root)

    return build


class TestTreeShape:
    def test_the_root_file_is_itself_a_sheet(self, tree):
        root_sheet = tree().root_sheet

        assert root_sheet.path == ROOT_PATH
        assert root_sheet.name == "root"

    def test_sheets_come_back_root_first_then_depth_first(self, tree):
        names = [sheet.name for sheet in tree().sheets()]

        assert names == ["root", "power", "main"]

    def test_children_are_the_sheets_drawn_on_one_sheet(self, tree):
        walked = tree()

        assert [c.name for c in walked.children(walked.root_sheet)] == ["power", "main"]
        assert walked.children(walked.find("power")) == []

    def test_the_root_file_itself_is_reachable_as_a_schematic(self, tree):
        assert tree().schematic_for(tree().root_sheet) is not None

    def test_a_child_that_parsed_is_reachable_as_a_schematic(self, tree):
        walked = tree()

        assert walked.schematic_for(walked.find("main")) is not None

    def test_a_grandchild_is_walked_too(self, tmp_path):
        # root -> mid -> leaf. A sheet file that carries both symbols and a
        # (sheet ...) block is the normal case, not a special one.
        schematic_file(tmp_path / "leaf.kicad_sch", uuid="leaf-doc", body=SYMBOL)
        schematic_file(
            tmp_path / "mid.kicad_sch",
            uuid="mid-doc",
            body=SYMBOL + sheet_symbol("leaf", "leaf.kicad_sch", uuid="leaf-uuid"),
        )
        root = schematic_file(
            tmp_path / "root.kicad_sch",
            uuid="root-doc",
            body=sheet_symbol("mid", "mid.kicad_sch", uuid="mid-uuid"),
        )

        walked = SchematicTree.load(root)

        assert [s.name for s in walked.sheets()] == ["root", "mid", "leaf"]

    def test_the_grandchild_belongs_to_mid_not_to_the_root(self, tmp_path):
        schematic_file(tmp_path / "leaf.kicad_sch", uuid="leaf-doc", body=SYMBOL)
        schematic_file(
            tmp_path / "mid.kicad_sch",
            uuid="mid-doc",
            body=SYMBOL + sheet_symbol("leaf", "leaf.kicad_sch", uuid="leaf-uuid"),
        )
        root = schematic_file(
            tmp_path / "root.kicad_sch",
            uuid="root-doc",
            body=sheet_symbol("mid", "mid.kicad_sch", uuid="mid-uuid"),
        )

        walked = SchematicTree.load(root)

        assert [(s.name, sym.sheet) for s, sym in walked.iter_symbols()] == [
            ("mid", "mid"),
            ("leaf", "leaf"),
        ]


class TestFind:
    def test_a_sheet_is_found_by_its_readable_name(self, tree):
        assert tree().find("main").name == "main"

    def test_a_sheet_is_found_by_its_instance_path(self, tree):
        walked = tree()
        found = walked.find("power")

        assert walked.find(found.path) is found

    def test_a_sheet_is_found_by_its_linked_file_stem(self, tree):
        assert tree().find("main").stem == "main"
        assert tree().find("power").stem == "power"

    def test_an_unknown_name_finds_nothing(self, tree):
        assert tree().find("nonesuch") is None


class TestSymbols:
    def test_every_symbol_in_the_tree_is_yielded_with_its_sheet(self, tree):
        pairs = [(s.name, sym.reference) for s, sym in tree().iter_symbols()]

        assert pairs == [("power", "R1"), ("main", "R1")]

    def test_a_stamped_symbol_carries_its_sheets_readable_name(self, tree):
        stamped = {s.name: sym.sheet for s, sym in tree().iter_symbols()}

        assert stamped == {"power": "power", "main": "main"}

    def test_a_symbol_knows_the_uuid_path_of_its_instance(self, tree):
        walked = tree()
        power = walked.find("power")

        _, symbol = next(iter(walked.iter_symbols()))

        assert symbol.sheet_path == power.path

    def test_the_root_sheets_symbols_report_no_sheet_name(self, hierarchical_design):
        root = hierarchical_design({}, root_body=SYMBOL)

        walked = SchematicTree.load(root)
        _, symbol = next(iter(walked.iter_symbols()))

        assert symbol.sheet is None
        assert symbol.sheet_path == ROOT_PATH


class TestSceneMemoisation:
    def test_the_same_sheet_returns_the_same_scene_object(self, tree):
        walked = tree()
        main = walked.find("main")

        assert walked.scene_for(main) is walked.scene_for(main)

    def test_different_sheets_get_different_scenes(self, tree):
        walked = tree()

        assert walked.scene_for(walked.find("power")) is not walked.scene_for(walked.find("main"))

    def test_a_sheet_whose_file_could_not_be_read_has_no_scene(self, hierarchical_design):
        root = hierarchical_design({"gone": None})
        walked = SchematicTree.load(root)
        missing = walked.children(walked.root_sheet)[0]

        assert walked.schematic_for(missing) is None
        assert walked.scene_for(missing) is None


class TestFailuresAreFindings:
    def test_a_link_to_nothing_is_reported_rather_than_raised(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert [s.name for s in walked.sheets()] == ["root"]
        assert [p.kind for p in walked.problems()] == [MISSING]

    def test_a_missing_link_names_the_sheet_and_where_it_looked(self, tmp_path):
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        problem = SchematicTree.load(root).problems()[0]

        assert problem.sheet.name == "gone"
        assert "gone.kicad_sch" in problem.detail

    def test_an_empty_sheetfile_is_reported_like_any_other_unresolved_link(self, tmp_path):
        body = sheet_symbol("blank", "", uuid="blank-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert [p.kind for p in walked.problems()] == [MISSING]
        assert "no Sheetfile" in walked.problems()[0].detail

    def test_a_link_to_a_directory_is_reported_as_unreadable(self, tmp_path):
        # The path resolves -- it exists -- but it is not a schematic.
        (tmp_path / "weird.kicad_sch").mkdir(parents=True)
        body = sheet_symbol("weird", "weird.kicad_sch", uuid="weird-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert [p.kind for p in walked.problems()] == [UNREADABLE]

    def test_a_file_that_is_not_a_schematic_is_reported_as_unreadable(self, tmp_path):
        (tmp_path / "junk.kicad_sch").write_text("this is not a schematic")
        body = sheet_symbol("junk", "junk.kicad_sch", uuid="junk-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert [p.kind for p in walked.problems()] == [UNREADABLE]

    def test_a_broken_link_does_not_stop_the_rest_of_the_tree(self, tmp_path):
        schematic_file(tmp_path / "ok.kicad_sch", uuid="ok-doc", body=SYMBOL)
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid") + sheet_symbol(
            "ok", "ok.kicad_sch", uuid="ok-uuid"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert [s.name for s in walked.sheets()] == ["root", "ok"]
        assert [p.kind for p in walked.problems()] == [MISSING]

    def test_a_root_that_is_not_a_schematic_still_raises(self, tmp_path):
        path = tmp_path / "nope.kicad_sch"
        path.write_text("this is not a schematic")

        with pytest.raises(ValueError, match="Not a KiCad schematic"):
            SchematicTree.load(path)


class TestCycles:
    def _looping_tree(self, tmp_path):
        """``root -> mid -> root``: the mid file links back to the root."""
        (tmp_path / "root.kicad_sch").write_text("", encoding="utf-8")
        root = schematic_file(
            tmp_path / "root.kicad_sch",
            uuid="root-doc",
            body=sheet_symbol("mid", "mid.kicad_sch", uuid="mid-uuid"),
        )
        schematic_file(
            tmp_path / "mid.kicad_sch",
            uuid="mid-doc",
            body=sheet_symbol("back", "root.kicad_sch", uuid="back-uuid"),
        )
        return SchematicTree.load(root)

    def test_a_sheet_that_links_to_an_ancestor_is_a_cycle(self, tmp_path):
        problems = self._looping_tree(tmp_path).problems()

        assert [p.kind for p in problems] == [CYCLE]

    def test_a_cycle_is_not_descended_into(self, tmp_path):
        walked = self._looping_tree(tmp_path)

        assert [s.name for s in walked.sheets()] == ["root", "mid"]

    def test_a_cycle_names_the_chain_from_the_repeated_sheet_back_to_itself(self, tmp_path):
        # The chain starts where the cycle is re-entered, so it reads as the loop
        # it is: root -> mid -> back, where `back` points at root again.
        problem = self._looping_tree(tmp_path).problems()[0]

        assert problem.chain == ("root", "mid", "back")

    def test_a_problem_reports_the_sheet_it_is_attached_to_by_name(self, tmp_path):
        assert self._looping_tree(tmp_path).problems()[0].name == "back"

    def test_one_file_instantiated_twice_is_not_a_cycle(self, tmp_path):
        # KiCad does this legitimately: the same file reached through two different
        # sheet symbols. Only a path back to an ancestor is a cycle.
        shared = schematic_file(tmp_path / "shared.kicad_sch", uuid="shared-doc", body=SYMBOL)
        body = sheet_symbol("first", "shared.kicad_sch", uuid="first-uuid") + sheet_symbol(
            "second", "shared.kicad_sch", uuid="second-uuid"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert walked.problems() == []
        assert [s.name for s in walked.sheets()] == ["root", "first", "second"]
        assert shared.exists()

    def test_both_instances_get_their_own_schematic_and_its_symbols(self, tmp_path):
        schematic_file(tmp_path / "shared.kicad_sch", uuid="shared-doc", body=SYMBOL)
        body = sheet_symbol("first", "shared.kicad_sch", uuid="first-uuid") + sheet_symbol(
            "second", "shared.kicad_sch", uuid="second-uuid"
        )
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        walked = SchematicTree.load(root)

        assert len(list(walked.iter_symbols())) == 2


class TestTreeMemoisation:
    def test_the_same_schematic_gives_back_the_same_tree(self, tree):
        walked = tree()
        schematic = FileSchematic(Path(walked.root.path))

        assert tree_for(schematic) is tree_for(schematic)

    def test_a_tree_built_from_a_schematic_reuses_it_rather_than_reparsing(self, tree):
        walked = tree()
        schematic = FileSchematic(Path(walked.root.path))

        assert tree_for(schematic).root is schematic


class TestSelection:
    def test_no_selector_means_the_whole_tree(self, tree):
        # The optional parameter has to stay optional and still general: a flat
        # design is a one-sheet tree, and a hierarchical one would otherwise be
        # checked only on its empty root.
        walked = tree()

        assert select_sheets(walked, None) == walked.sheets()

    def test_all_means_every_sheet_in_walk_order(self, tree):
        selected = select_sheets(tree(), "all")

        assert selected is not None
        assert len(selected) == 3

    def test_the_all_selector_is_case_insensitive(self, tree):
        assert select_sheets(tree(), "ALL") == select_sheets(tree(), "all")

    def test_a_name_selects_that_one_sheet(self, tree):
        selected = select_sheets(tree(), "main")

        assert selected is not None
        assert [s.name for s in selected] == ["main"]

    def test_an_unknown_name_selects_nothing(self, tree):
        assert select_sheets(tree(), "nonesuch") is None

    def test_a_target_is_the_sheets_file_and_its_geometry(self, tree):
        walked = tree()
        targets, found = select_targets(walked, "main")

        assert found
        assert targets is not None
        assert [t.sheet.name for t in targets] == ["main"]
        assert targets[0].scene is walked.scene_for(walked.find("main"))

    def test_a_sheet_whose_file_could_not_be_read_is_left_out_silently(self, tmp_path):
        # sch.sheet.file_missing owns that finding; a layout check has nothing
        # useful to say about a file it cannot parse.
        body = sheet_symbol("gone", "gone.kicad_sch", uuid="gone-uuid")
        root = schematic_file(tmp_path / "root.kicad_sch", uuid="root", body=body)

        targets, found = select_targets(SchematicTree.load(root), "all")

        assert found
        assert targets is not None
        assert [t.sheet.name for t in targets] == ["root"]


class TestLabelPrefix:
    def test_a_root_finding_keeps_its_label_exactly_as_it_always_was(self, tree):
        assert label_prefix(tree().root_sheet) == ""

    def test_a_finding_from_another_sheet_is_qualified_by_its_name(self, tree):
        walked = tree()

        assert label_prefix(walked.find("main")) == "main."
