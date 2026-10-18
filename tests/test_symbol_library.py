"""Symbol libraries: discovery, extends, signatures, the writer, and editing."""

import subprocess
from pathlib import Path

import pytest

from conftest import FakeContext, demo_schematic_path, hierarchy_schematic_path
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.schematic.symbol_matches_library import SymbolMatchesLibraryCheck
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.edit import (
    EditError,
    embed_symbol,
    load_document,
    placed_symbols,
    save_document,
    set_symbol_property,
    swap_symbol,
)
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.sexpr import Quoted, child, dumps, parse, value_of
from kicad_evaltor.utils.kicad_cli import parse_erc_report
from kicad_evaltor.symbol_library import (
    SymbolLibraries,
    SymbolNotFound,
    find_symbol_dirs,
    flatten_symbol,
    read_lib_table,
    split_lib_id,
    symbol_diff,
    symbol_signature,
)

LIBRARY = """(kicad_symbol_lib (version 20241209) (generator "test")
  (symbol "Q_BASE"
    (pin_names (offset 0) (hide yes))
    (in_bom yes) (on_board yes)
    (property "Reference" "Q" (at 0 0 0))
    (property "Value" "Q_BASE" (at 0 0 0))
    (property "Description" "base" (at 0 0 0))
    (symbol "Q_BASE_0_1" (polyline (pts (xy 0 0) (xy 1 1)) (stroke (width 0.254))))
    (symbol "Q_BASE_1_1"
      (pin input line (at -5.08 0 0) (length 2.54) (name "G") (number "1"))
      (pin passive line (at 2.54 5.08 270) (length 2.54) (name "D") (number "3")))
    (embedded_fonts no))
  (symbol "Q_PART"
    (extends "Q_BASE")
    (property "Reference" "Q" (at 0 0 0))
    (property "Value" "Q_PART" (at 1 1 0))
    (property "ki_fp_filters" "SOT?23*" (at 0 0 0))
    (embedded_fonts no))
  (symbol "Q_GRANDCHILD" (extends "Q_PART") (property "Value" "Q_GRANDCHILD" (at 0 0 0)))
  (symbol "LOOP_A" (extends "LOOP_B"))
  (symbol "LOOP_B" (extends "LOOP_A"))
  (symbol "ORPHAN" (extends "MISSING")))
"""


@pytest.fixture
def lib_dir(tmp_path):
    folder = tmp_path / "symbols"
    folder.mkdir()
    (folder / "Test.kicad_sym").write_text(LIBRARY, encoding="utf-8")
    return folder


@pytest.fixture
def libs(lib_dir):
    return SymbolLibraries([lib_dir])


def stock_libraries() -> SymbolLibraries:
    libs = SymbolLibraries()
    if not libs.dirs:
        pytest.skip("KiCad's stock symbol libraries are not installed")
    return libs


class TestWriter:
    def test_quoted_atoms_survive_a_round_trip(self):
        tree = parse('(a "yes" yes "" "x y" "q\\"uote" (b 1.0))')
        out = dumps(tree)

        assert '"yes" yes "" "x y" "q\\"uote"' in out
        assert parse(out) == tree
        assert isinstance(parse(out)[1], Quoted)
        assert not isinstance(parse(out)[2], Quoted)

    def test_atom_lists_stay_on_one_line(self):
        assert dumps(["at", "1", "2", "0"]) == "(at 1 2 0)\n"
        assert dumps(["a", ["b", "1"], "c"]) == "(a\n\t(b 1)\n\tc\n)\n"

    @pytest.mark.parametrize(
        "path", [demo_schematic_path(), hierarchy_schematic_path()], ids=lambda p: p.name
    )
    def test_a_real_schematic_round_trips(self, path):
        tree = parse(path.read_text(encoding="utf-8"))
        back = parse(dumps(tree))

        assert back == tree
        assert _quoting(back) == _quoting(tree)


def _quoting(node):
    if isinstance(node, list):
        return [_quoting(n) for n in node]
    return isinstance(node, Quoted)


class TestLibraryLookup:
    def test_split_lib_id(self):
        assert split_lib_id("Device:R") == ("Device", "R")
        for bad in ("R", ":R", "Device:"):
            with pytest.raises(ValueError):
                split_lib_id(bad)

    def test_get_and_contains(self, libs):
        assert value_of(libs.get("Test:Q_PART")) == "Q_PART"
        assert "Test:Q_PART" in libs
        assert "Test:NOPE" not in libs
        assert "Nope:R" not in libs
        assert "garbage" not in libs

    def test_missing_library_names_where_it_looked(self, libs, lib_dir):
        with pytest.raises(SymbolNotFound, match="Nope"):
            libs.get("Nope:R")

    def test_symdir_libraries(self, tmp_path):
        folder = tmp_path / "Split.kicad_symdir"
        folder.mkdir()
        (folder / "Q_BASE.kicad_sym").write_text(LIBRARY, encoding="utf-8")

        assert "Split:Q_BASE" in SymbolLibraries([tmp_path])

    def test_project_table_remaps_a_nickname(self, tmp_path, lib_dir):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "local.kicad_sym").write_text(LIBRARY, encoding="utf-8")
        (project / "sym-lib-table").write_text(
            '(sym_lib_table (version 7) (lib (name "Device") (type "KiCad") '
            '(uri "${KIPRJMOD}/local.kicad_sym") (options "") (descr "")))',
            encoding="utf-8",
        )

        libs = SymbolLibraries([lib_dir], project_dir=project)

        assert libs.library_path("Device") == project / "local.kicad_sym"
        assert "Device:Q_BASE" in libs

    def test_lib_table_skips_unknown_variables(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
        table = tmp_path / "sym-lib-table"
        table.write_text(
            '(sym_lib_table (lib (name "A") (uri "${NOT_SET_ANYWHERE}/a.kicad_sym")) '
            '(lib (name "B") (uri "/libs/b.kicad_sym")))',
            encoding="utf-8",
        )

        assert read_lib_table(table) == {"B": Path("/libs/b.kicad_sym")}

    def test_env_var_comes_first(self, monkeypatch, lib_dir):
        monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(lib_dir))

        assert find_symbol_dirs()[0] == lib_dir


class TestFlatten:
    def test_takes_the_parents_units_renamed(self, libs):
        flat = libs.resolve("Test:Q_PART")

        assert value_of(flat) == "Q_PART"
        assert child(flat, "extends") is None
        assert [value_of(u) for u in flat if isinstance(u, list) and u[0] == "symbol"] == [
            "Q_PART_0_1",
            "Q_PART_1_1",
        ]

    def test_child_properties_replace_in_place_and_extras_follow(self, libs):
        flat = libs.resolve("Test:Q_PART")
        props = [
            (value_of(p), value_of(p, 2))
            for p in flat
            if isinstance(p, list) and p[0] == "property"
        ]

        assert props == [
            ("Reference", "Q"),
            ("Value", "Q_PART"),
            ("Description", "base"),
            ("ki_fp_filters", "SOT?23*"),
        ]

    def test_parent_settings_are_kept(self, libs):
        flat = libs.resolve("Test:Q_PART")

        assert child(flat, "pin_names") is not None
        assert value_of(child(flat, "in_bom")) == "yes"

    def test_resolves_more_than_one_level(self, libs):
        flat = libs.resolve("Test:Q_GRANDCHILD")

        assert symbol_signature(flat) == symbol_signature(libs.resolve("Test:Q_BASE"))
        assert value_of(child(flat, "symbol")) == "Q_GRANDCHILD_0_1"

    def test_a_cycle_raises(self, libs):
        with pytest.raises(ValueError, match="cycle"):
            libs.resolve("Test:LOOP_A")

    def test_a_missing_parent_raises(self, libs):
        with pytest.raises(SymbolNotFound, match="MISSING"):
            libs.resolve("Test:ORPHAN")

    def test_resolve_returns_a_fresh_tree(self, libs):
        libs.resolve("Test:Q_BASE")[1] = "changed"

        assert value_of(libs.resolve("Test:Q_BASE")) == "Q_BASE"

    def test_embeddable_is_named_by_lib_id(self, libs):
        assert value_of(libs.embeddable("Test:Q_PART")) == "Test:Q_PART"

    def test_flatten_symbol_with_any_lookup(self):
        lookup = {
            "A": ["symbol", "A", ["extends", "B"]],
            "B": ["symbol", "B", ["symbol", "B_1_1"]],
        }.get

        assert flatten_symbol("A", lookup) == ["symbol", "A", ["symbol", "A_1_1"]]


class TestSignature:
    def test_ignores_names_fields_and_formatting(self, libs):
        base = libs.resolve("Test:Q_BASE")
        part = libs.resolve("Test:Q_PART")

        assert symbol_signature(base) == symbol_signature(part)

    def test_numbers_compare_by_value(self):
        a = parse(
            '(symbol "A" (symbol "A_1_1" (pin input line (at 1.0 0 0) (length 2.54) (name "X") (number "1"))))'
        )
        b = parse(
            '(symbol "B" (symbol "B_1_1" (pin input line (at 1 0 360) (length 2.540) (name "X") (number "1"))))'
        )

        assert symbol_signature(a) == symbol_signature(b)

    def test_diff_names_each_change(self, libs):
        base = libs.resolve("Test:Q_BASE")
        changed = libs.resolve("Test:Q_BASE")
        unit = [u for u in changed if isinstance(u, list) and u[0] == "symbol"][1]
        gate = next(p for p in unit if isinstance(p, list) and p[0] == "pin")
        gate[1] = "output"
        child(gate, "name")[1] = "GATE"
        unit.append(parse('(pin passive line (at 0 0 0) (length 2.54) (name "S") (number "2"))'))

        problems = symbol_diff(symbol_signature(base), symbol_signature(changed))

        assert "extra pin 2 S (unit 1)" in problems
        assert any("electrical_type: expected 'input', found 'output'" in p for p in problems)
        assert any("name: expected 'G', found 'GATE'" in p for p in problems)

    def test_graphics_can_be_left_out(self, libs):
        base = libs.resolve("Test:Q_BASE")
        moved = libs.resolve("Test:Q_BASE")
        child(child(child(moved, "symbol"), "polyline"), "pts")[1][1] = "9"

        assert symbol_signature(base) != symbol_signature(moved)
        assert symbol_diff(symbol_signature(base), symbol_signature(moved)) == [
            "body drawing differs: 1 shape(s) missing, 1 extra"
        ]
        assert symbol_signature(base, include_graphics=False) == symbol_signature(
            moved, include_graphics=False
        )

    def test_an_unflattened_symbol_is_refused(self, libs):
        with pytest.raises(ValueError, match="flatten"):
            symbol_signature(libs.get("Test:Q_PART"))


class TestStockLibraries:
    """Against the installed KiCad libraries, where the real format lives."""

    @pytest.mark.parametrize(
        "path",
        [
            demo_schematic_path(),
            hierarchy_schematic_path(),
            hierarchy_schematic_path().with_name("circuit.kicad_sch"),
        ],
        ids=lambda p: p.name,
    )
    def test_flattening_matches_what_kicad_embeds(self, path):
        libs = stock_libraries()
        embedded = FileSchematic(path).lib_symbols()

        for lib_id, node in embedded.items():
            # Byte for byte, derived symbols (2N7002, ATmega328P-M) included.
            assert dumps(libs.embeddable(lib_id)) == dumps(node), lib_id


class TestEditing:
    def demo_doc(self):
        return load_document(hierarchy_schematic_path())

    def test_placed_symbols_finds_by_reference(self):
        doc = self.demo_doc()
        assert len(placed_symbols(doc, "Q1")) == 1
        assert placed_symbols(doc, "NOPE") == []

    def test_load_rejects_other_files(self, tmp_path):
        bad = tmp_path / "x.kicad_sch"
        bad.write_text("(kicad_wks)", encoding="utf-8")
        with pytest.raises(EditError):
            load_document(bad)

    def test_swap_a_derived_symbol(self, tmp_path):
        libs = stock_libraries()
        doc = self.demo_doc()
        swap_symbol(doc, "Q1", "Transistor_FET:BSS138", libs)
        path = save_document(doc, tmp_path / "hierarchy.kicad_sch")

        schematic = FileSchematic(path)
        q1 = next(s for s in schematic.get_symbols() if s.reference == "Q1")
        assert (q1.lib_id, q1.value) == ("Transistor_FET:BSS138", "BSS138")
        assert "Transistor_FET:2N7002" not in schematic.lib_symbols()
        assert dumps(schematic.lib_symbols()["Transistor_FET:BSS138"]) == dumps(
            libs.embeddable("Transistor_FET:BSS138")
        )

    def test_swap_keeps_a_real_value_and_a_still_used_copy(self, tmp_path):
        libs = stock_libraries()
        doc = self.demo_doc()
        swap_symbol(doc, "R12", "Device:R_US", libs)
        schematic = FileSchematic(save_document(doc, tmp_path / "h.kicad_sch"))

        r12 = next(s for s in schematic.get_symbols() if s.reference == "R12")
        assert r12.lib_id == "Device:R_US"
        assert r12.value not in ("R", "R_US")
        assert {"Device:R", "Device:R_US"} <= set(schematic.lib_symbols())

    def test_swap_rebuilds_pin_uuids(self):
        libs = stock_libraries()
        doc = self.demo_doc()
        before = {
            value_of(p): p
            for p in placed_symbols(doc, "Q1")[0]
            if isinstance(p, list) and p[0] == "pin"
        }
        swap_symbol(doc, "Q1", "Device:R", libs)
        after = {
            value_of(p): p
            for p in placed_symbols(doc, "Q1")[0]
            if isinstance(p, list) and p[0] == "pin"
        }

        assert sorted(after) == ["1", "2"]
        assert after["1"] is before["1"]
        assert "3" not in after

    def test_set_symbol_property_and_embed(self, libs):
        doc = self.demo_doc()
        set_symbol_property(doc, "Q1", "Value", "custom")
        embed_symbol(doc, "Test:Q_PART", libs)

        assert (
            value_of(
                next(
                    p
                    for p in placed_symbols(doc, "Q1")[0]
                    if isinstance(p, list) and value_of(p) == "Value"
                ),
                2,
            )
            == "custom"
        )
        assert "Test:Q_PART" in [value_of(n) for n in child(doc, "lib_symbols")[1:]]
        with pytest.raises(EditError):
            set_symbol_property(doc, "Q1", "NoSuchField", "x")
        with pytest.raises(EditError):
            swap_symbol(doc, "NOPE", "Test:Q_PART", libs)

    @pytest.mark.integration
    def test_swapped_fixture_passes_erc(self, kicad_cli_required, tmp_path):
        libs = stock_libraries()
        source = hierarchy_schematic_path().parent
        for file in source.iterdir():
            if file.suffix in (".kicad_sch", ".kicad_pro"):
                (tmp_path / file.name).write_bytes(file.read_bytes())
        root = tmp_path / "hierarchy.kicad_sch"
        doc = load_document(root)
        swap_symbol(doc, "Q1", "Transistor_FET:BSS138", libs)
        save_document(doc, root)

        report = tmp_path / "erc.json"
        subprocess.run(
            [
                str(kicad_cli_required),
                "sch",
                "erc",
                "--severity-all",
                "--format",
                "json",
                "-o",
                str(report),
                str(root),
            ],
            capture_output=True,
            timeout=300,
            check=False,
        )
        # The demo is clean, so any violation at all was caused by the swap.
        assert parse_erc_report(report.read_text(encoding="utf-8")) == []


class TestSymbolMatchesLibraryCheck:
    def ctx(self, path):
        return DesignContext(schematic_path=path)

    def test_stock_design_matches(self):
        stock_libraries()
        result = SymbolMatchesLibraryCheck().run(self.ctx(hierarchy_schematic_path()))

        assert result.is_pass, result.details
        assert "Q1" in result.details["compared"]
        assert not any(ref.startswith("#") for ref in result.details["compared"])

    def test_a_changed_copy_fails_with_the_difference(self, tmp_path):
        stock_libraries()
        doc = load_document(demo_schematic_path())
        table = child(doc, "lib_symbols")
        resistor = next(n for n in table[1:] if value_of(n) == "Device:R")
        pin_unit = [u for u in resistor if isinstance(u, list) and u[0] == "symbol"][-1]
        child(child(pin_unit, "pin"), "name")[1] = "X"
        path = save_document(doc, tmp_path / "changed.kicad_sch")

        result = SymbolMatchesLibraryCheck(lib_id="Device:R").run(self.ctx(path))

        assert result.is_fail
        mismatches = result.details["mismatches"]
        assert mismatches
        for problems in mismatches.values():
            assert any("name: expected '', found 'X'" in p for p in problems)

    def test_expected_lib_id_compares_against_one_symbol(self):
        stock_libraries()
        q1 = SymbolMatchesLibraryCheck(references=["Q1"], expected_lib_id="Transistor_FET:BSS138")
        r = SymbolMatchesLibraryCheck(references=["Q1"], expected_lib_id="Device:R")

        # BSS138 and 2N7002 share their parent, so their pins and body agree.
        assert q1.run(self.ctx(hierarchy_schematic_path())).is_pass
        assert r.run(self.ctx(hierarchy_schematic_path())).is_fail

    def test_missing_reference_fails(self):
        stock_libraries()
        result = SymbolMatchesLibraryCheck(references=["Q99"]).run(
            self.ctx(hierarchy_schematic_path())
        )

        assert result.is_fail
        assert result.details["missing"] == ["Q99"]

    def test_library_dirs_param(self, lib_dir):
        result = SymbolMatchesLibraryCheck(references=["Q1"], library_dirs=[str(lib_dir)]).run(
            self.ctx(hierarchy_schematic_path())
        )

        assert result.is_fail
        assert "Transistor_FET" in result.details["mismatches"]["Q1"][0]

    def test_skips_without_schematic(self):
        assert SymbolMatchesLibraryCheck().run(FakeContext()).status == TestStatus.SKIP

    def test_rejects_a_bad_expected_lib_id(self):
        with pytest.raises(ValueError):
            SymbolMatchesLibraryCheck(expected_lib_id="R")
