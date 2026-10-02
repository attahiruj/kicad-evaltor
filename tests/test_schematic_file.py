"""Tests for the dependency-free s-expression reader and the file-backed schematic."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_evaltor.schematic_file import (
    FileSchematic,
    NetlistUnavailable,
    load_schematic,
    to_dict,
)
from kicad_evaltor.sexpr import child, children, head, parse, value_of
from conftest import demo_schematic_path

DEMO_SCHEMATIC = demo_schematic_path()

MINIMAL = """(kicad_sch (version 20250114) (generator "evaltor")
  (lib_symbols
    (symbol "Device:R" (property "Reference" "R?"))
  )
  (symbol (lib_id "Device:R") (at 10 20)
    (property "Reference" "R1" (at 0 0))
    (property "Value" "10k" (at 0 0))
    (property "Footprint" "Resistor_SMD:R_0603" (at 0 0))
    (property "MPN" "RC0603FR-0710KL" (at 0 0))
  )
  (symbol (lib_id "power:GND") (at 30 40)
    (property "Reference" "#PWR01" (at 0 0))
    (property "Value" "GND" (at 0 0))
    (property "Footprint" "" (at 0 0))
  )
)
"""


@pytest.fixture
def minimal_sch(tmp_path):
    path = tmp_path / "minimal.kicad_sch"
    path.write_text(MINIMAL, encoding="utf-8")
    return path


class TestParse:
    def test_parses_nested_lists(self):
        assert parse("(a (b 1) (c 2))") == ["a", ["b", "1"], ["c", "2"]]

    def test_quoted_string_keeps_spaces(self):
        assert parse('(t "hello world")') == ["t", "hello world"]

    def test_ignores_whitespace_and_newlines(self):
        assert parse("(a\r\n  (b 1)\n)") == ["a", ["b", "1"]]

    def test_escapes_are_honoured(self):
        assert parse(r'(t "a\"b\\c")') == ["t", 'a"b\\c']

    def test_unknown_escape_keeps_the_literal_character(self):
        assert parse(r'(t "a\qb")') == ["t", "aqb"]

    def test_returns_none_when_unbalanced(self):
        assert parse("(a (b 1)") is None

    def test_returns_none_for_empty_input(self):
        assert parse("") is None

    def test_stops_at_end_of_first_top_level_expression(self):
        assert parse("(a) (b)") == ["a"]

    def test_tolerates_garbage_before_the_expression(self):
        assert parse("garbage (a 1)") == ["a", "1"]


class TestAccessors:
    def test_head_of_list(self):
        assert head(["symbol", "x"]) == "symbol"

    def test_head_of_atom_is_empty(self):
        assert head("symbol") == ""

    def test_head_of_empty_list_is_empty(self):
        assert head([]) == ""

    def test_children_matches_only_direct_children(self):
        node = parse("(a (b 1) (b 2) (c (b 3)))")
        assert children(node, "b") == [["b", "1"], ["b", "2"]]

    def test_children_of_atom_is_empty(self):
        assert children("a", "b") == []

    def test_child_returns_first_match(self):
        assert child(parse("(a (b 1) (b 2))"), "b") == ["b", "1"]

    def test_child_returns_none_when_absent(self):
        assert child(parse("(a 1)"), "b") is None

    def test_value_of_reads_indexed_atom(self):
        assert value_of(["lib_id", "Device:R"]) == "Device:R"

    def test_value_of_out_of_range_returns_default(self):
        assert value_of(["lib_id"], 1, "fallback") == "fallback"

    def test_value_of_non_string_returns_default(self):
        assert value_of(["lib_id", ["nested"]], 1) == ""


class TestFileSchematicSymbols:
    def test_gets_placed_symbols_only(self, minimal_sch):
        refs = [s.reference for s in FileSchematic(minimal_sch).get_symbols()]

        # The lib_symbols definition for Device:R must not become a placed part.
        assert refs == ["R1", "#PWR01"]

    def test_reads_lib_id_value_and_footprint(self, minimal_sch):
        r1 = FileSchematic(minimal_sch).get_symbols()[0]

        assert r1.lib_id == "Device:R"
        assert r1.value == "10k"
        assert r1.footprint.text.value == "Resistor_SMD:R_0603"

    def test_standard_properties_land_in_fields(self, minimal_sch):
        r1 = FileSchematic(minimal_sch).get_symbols()[0]

        assert r1.fields["Reference"] == "R1"
        assert r1.fields["Value"] == "10k"

    def test_custom_properties_land_in_user_fields(self, minimal_sch):
        r1 = FileSchematic(minimal_sch).get_symbols()[0]

        assert r1.user_fields == {"MPN": "RC0603FR-0710KL"}
        assert "MPN" not in r1.fields

    def test_blank_footprint_is_empty_not_missing(self, minimal_sch):
        gnd = FileSchematic(minimal_sch).get_symbols()[1]

        assert gnd.footprint.text.value == ""

    def test_rejects_a_non_schematic_file(self, tmp_path):
        path = tmp_path / "nope.kicad_sch"
        path.write_text("this is not a schematic")

        with pytest.raises(ValueError, match="Not a KiCad schematic"):
            FileSchematic(path)

    def test_accepts_the_real_demo_schematic(self):
        schematic = FileSchematic(DEMO_SCHEMATIC)
        symbols = schematic.get_symbols()

        assert len(symbols) == 34
        by_ref = {s.reference: s for s in symbols}
        assert by_ref["U1"].lib_id == "MCU_Microchip_ATmega:ATmega328P-M"
        assert by_ref["R1"].value == "10k"
        assert by_ref["R2"].value == "4k7"

    def test_demo_power_symbols_use_the_power_prefix(self):
        symbols = FileSchematic(DEMO_SCHEMATIC).get_symbols()
        power = [s for s in symbols if s.lib_id.startswith("power:")]

        assert len(power) == 19


class TestFileSchematicNetlist:
    def test_reports_unavailable_without_a_cli(self, minimal_sch):
        schematic = FileSchematic(minimal_sch, cli_path=None)
        schematic._resolve_cli = lambda: None

        with pytest.raises(NetlistUnavailable, match="needs kicad-cli"):
            schematic.get_netlist()

    def test_reports_unavailable_when_the_cli_fails(self, minimal_sch, monkeypatch):
        class FakeCompleted:
            returncode = 3
            stdout = ""
            stderr = "Failed to load schematic"

        monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: FakeCompleted())
        schematic = FileSchematic(minimal_sch, cli_path="kicad-cli")

        with pytest.raises(NetlistUnavailable, match="Failed to load schematic"):
            schematic.get_netlist()

    def test_reports_unavailable_when_output_is_unreadable(self, minimal_sch, monkeypatch):
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("not a netlist")
            return real_run([sys.executable, "-c", "pass"], *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)
        schematic = FileSchematic(minimal_sch, cli_path="kicad-cli")

        with pytest.raises(NetlistUnavailable, match="unreadable"):
            schematic.get_netlist()

    def test_reads_nets_from_an_exported_netlist(self, minimal_sch, monkeypatch, tmp_path):
        def fake_run(cmd, *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text(
                '(kicad_netlist (nets (net (name "/SDA") '
                '(node (ref "U1") (pin "15")) (node (ref "U2") (pin "14")))))',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        netlist = FileSchematic(minimal_sch, cli_path="kicad-cli").get_netlist()

        assert [n.name for n in netlist.nets] == ["/SDA"]
        assert [(n.ref, n.pin) for n in netlist.nets[0].nodes] == [("U1", "15"), ("U2", "14")]

    def test_skips_nets_without_a_name(self, minimal_sch, monkeypatch):
        def fake_run(cmd, *args, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text(
                '(kicad_netlist (nets (net (code "1") (node (ref "U1") (pin "1")))))',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert FileSchematic(minimal_sch, cli_path="kicad-cli").get_netlist().nets == []


class TestHelpers:
    def test_load_schematic_returns_a_file_schematic(self, minimal_sch):
        assert isinstance(load_schematic(minimal_sch), FileSchematic)

    def test_to_dict_summarises_the_schematic(self, minimal_sch):
        summary = to_dict(load_schematic(minimal_sch))

        assert summary["symbol_count"] == 2
        assert summary["symbols"][0] == {
            "reference": "#PWR01",
            "lib_id": "power:GND",
            "value": "GND",
        }
        assert summary["path"] == str(minimal_sch)

    def test_to_dict_json_serialises(self, minimal_sch):
        assert json.loads(json.dumps(to_dict(load_schematic(minimal_sch))))["symbol_count"] == 2
