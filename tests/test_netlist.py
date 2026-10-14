"""The netlist reader: both export formats, the model, and the real kicad-cli."""

import subprocess
from pathlib import Path

import pytest

from conftest import demo_schematic_path
from kicad_evaltor.netlist import FileNetlist, NetlistParseError, parse_netlist, unescape_net_name
from kicad_evaltor.schematic_file import FileSchematic, NetlistUnavailable

XML = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="BT1">
      <value>4xAA</value>
      <footprint></footprint>
      <description>Battery pack</description>
      <fields>
        <field name="Footprint"/>
        <field name="MPN">BH-4AA</field>
      </fields>
      <libsource lib="Device" part="Battery" description="Battery"/>
      <property name="exclude_from_bom"/>
      <property name="exclude_from_board"/>
      <property name="dnp"/>
      <property name="ki_keywords" value="batt"/>
      <sheetpath names="/power/" tstamps="/abc/"/>
    </comp>
    <comp ref="U1">
      <value>CH340C</value>
      <footprint>Package_SO:SOIC-16</footprint>
      <libsource lib="Interface_USB" part="CH340C"/>
      <sheetpath names="/" tstamps="/"/>
    </comp>
  </components>
  <nets>
    <net code="1" name="+BATT">
      <node ref="BT1" pin="1" pinfunction="+" pintype="passive"/>
      <node ref="U1" pin="16" pinfunction="VCC" pintype="power_in"/>
    </net>
    <net code="2" name="/USB_DP">
      <node ref="U1" pin="5" pinfunction="UD+" pintype="bidirectional"/>
    </net>
    <net code="3" name="">
      <node ref="U1" pin="9"/>
    </net>
  </nets>
</export>
"""

SEXPR = """(export (version "E")
  (components
    (comp (ref "R1") (value "10k") (footprint "R_0603")
      (fields (field (name "Footprint") "R_0603") (field (name "Datasheet")))
      (libsource (lib "Device") (part "R"))
      (property (name "ki_keywords") (value "res"))
      (sheetpath (names "/") (tstamps "/"))))
  (nets
    (net (code "1") (name "/SDA")
      (node (ref "R1") (pin "1") (pintype "passive"))
      (node (ref "U2") (pin "14") (pinfunction "SDA") (pintype "bidirectional")))))
"""


class TestParseXml:
    def test_reads_flags_that_the_sexpr_format_cannot_carry(self):
        netlist = parse_netlist(XML, "kicadxml")
        battery = netlist.components["BT1"]

        assert battery.dnp
        assert battery.exclude_from_bom
        assert battery.exclude_from_board
        assert not netlist.components["U1"].dnp

    def test_reads_component_identity(self):
        comp = parse_netlist(XML, "kicadxml").components["BT1"]

        assert comp.lib_id == "Device:Battery"
        assert comp.value == "4xAA"
        assert comp.description == "Battery pack"
        assert comp.fields["MPN"] == "BH-4AA"
        assert comp.properties["ki_keywords"] == "batt"
        assert comp.sheet_path == "/power/"

    def test_reads_pin_names_and_types(self):
        netlist = parse_netlist(XML, "kicadxml")
        node = netlist.node("U1", "5")

        assert node is not None
        assert node.pinfunction == "UD+"
        assert node.pintype == "bidirectional"
        assert node.net == "/USB_DP"

    def test_skips_nets_without_a_name(self):
        assert [n.name for n in parse_netlist(XML, "kicadxml").nets] == ["+BATT", "/USB_DP"]

    def test_unreadable_xml_raises(self):
        with pytest.raises(NetlistParseError):
            parse_netlist("<export>", "kicadxml")

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="unknown netlist format"):
            parse_netlist(XML, "spice")  # type: ignore[arg-type]


class TestParseSexpr:
    def test_reads_the_same_model(self):
        netlist = parse_netlist(SEXPR, "kicadsexpr")

        assert netlist.components["R1"].lib_id == "Device:R"
        assert netlist.components["R1"].fields == {"Footprint": "R_0603", "Datasheet": ""}
        assert netlist.node("U2", "14").pinfunction == "SDA"
        assert netlist.net_of("R1", "1") == "/SDA"


class TestLookups:
    def test_net_of_and_nodes_on(self):
        netlist = parse_netlist(XML, "kicadxml")

        assert netlist.net_of("BT1", "1") == "+BATT"
        assert netlist.net_of("BT1", "2") is None
        assert [n.ref for n in netlist.nodes_on("+BATT")] == ["BT1", "U1"]
        assert netlist.nodes_on("nope") == []

    def test_pins_of_sorts_numerically(self):
        netlist = parse_netlist(XML, "kicadxml")

        assert [n.pin for n in netlist.pins_of("U1")] == ["5", "16"]

    def test_an_empty_netlist_answers_without_error(self):
        netlist = FileNetlist([])

        assert netlist.net("x") is None
        assert netlist.pins_of("R1") == []
        assert netlist.components == {}


class TestFileSchematicFormats:
    @pytest.fixture
    def exporting(self, monkeypatch):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            fmt = cmd[cmd.index("--format") + 1]
            calls.append(fmt)
            Path(cmd[cmd.index("-o") + 1]).write_text(
                XML if fmt == "kicadxml" else SEXPR, encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_defaults_to_xml(self, exporting):
        schematic = FileSchematic(demo_schematic_path(), cli_path="kicad-cli")

        assert "BT1" in schematic.get_netlist().components
        assert exporting == ["kicadxml"]

    def test_exports_once_per_format(self, exporting):
        schematic = FileSchematic(demo_schematic_path(), cli_path="kicad-cli")
        schematic.get_netlist()
        schematic.get_netlist()
        schematic.get_netlist("kicadsexpr")

        assert exporting == ["kicadxml", "kicadsexpr"]

    def test_rejects_an_unknown_format_before_running_anything(self, exporting):
        schematic = FileSchematic(demo_schematic_path(), cli_path="kicad-cli")

        with pytest.raises(ValueError, match="unknown netlist format"):
            schematic.get_netlist("spice")  # type: ignore[arg-type]
        assert exporting == []

    def test_a_timeout_is_reported_as_unavailable(self, monkeypatch):
        def slow(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", slow)
        schematic = FileSchematic(demo_schematic_path(), cli_path="kicad-cli", cli_timeout=7)

        with pytest.raises(NetlistUnavailable, match="timed out after 7s"):
            schematic.get_netlist()


class TestSymbolFlags:
    def test_reads_placement_flags(self, tmp_path):
        source = demo_schematic_path().read_text(encoding="utf-8")
        start = source.index('(lib_id "Device:R")')
        end = source.index("(dnp no)", start) + len("(dnp no)")
        flagged = (
            source[start:end]
            .replace("(in_bom yes)", "(in_bom no)")
            .replace("(on_board yes)", "(on_board no)")
            .replace("(dnp no)", "(dnp yes)")
        )
        path = tmp_path / "flags.kicad_sch"
        path.write_text(source[:start] + flagged + source[end:], encoding="utf-8")

        symbols = FileSchematic(path).get_symbols()
        flagged_symbol = next(s for s in symbols if s.dnp)

        assert flagged_symbol.lib_id == "Device:R"
        assert not flagged_symbol.in_bom
        assert not flagged_symbol.on_board
        assert sum(s.dnp for s in symbols) == 1
        assert all(s.on_board for s in symbols if s is not flagged_symbol)


@pytest.mark.integration
class TestRealExport:
    def test_xml_keeps_a_part_excluded_from_the_board(self, kicad_cli_required, tmp_path):
        source = demo_schematic_path().read_text(encoding="utf-8")
        start = source.index('(lib_id "Device:R")')
        end = source.index("(dnp no)", start) + len("(dnp no)")
        flagged = source[start:end].replace("(on_board yes)", "(on_board no)")
        path = tmp_path / "offboard.kicad_sch"
        path.write_text(source[:start] + flagged + source[end:], encoding="utf-8")
        offboard = next(s for s in FileSchematic(path).get_symbols() if not s.on_board)

        schematic = FileSchematic(path, cli_path=str(kicad_cli_required))
        xml = schematic.get_netlist()
        sexpr = schematic.get_netlist("kicadsexpr")

        assert xml.components[offboard.reference].exclude_from_board
        assert xml.pins_of(offboard.reference)
        assert offboard.reference not in sexpr.components
        assert {n.name for n in sexpr.nets} == {n.name for n in xml.nets}


class TestNetNameEscapes:
    def test_sexpr_names_match_the_xml_spelling(self):
        text = '(export (nets (net (code "1") (name "Net-(U1-~{RESET}{slash}PC6)") (node (ref "U1") (pin "29")))))'

        assert parse_netlist(text, "kicadsexpr").nets[0].name == "Net-(U1-~{RESET}/PC6)"

    def test_unescape_leaves_plain_and_overbar_names(self):
        assert unescape_net_name("/SDA") == "/SDA"
        assert unescape_net_name("~{CS}") == "~{CS}"
        assert unescape_net_name("A{colon}B{space}C") == "A:B C"


class TestPinNameSuffix:
    def xml(self, *nodes):
        body = "".join(f'<node ref="U1" pin="{pin}" pinfunction="{name}"/>' for pin, name in nodes)
        return f'<export><nets><net code="1" name="N">{body}</net></nets></export>'

    def test_kicad_10_numbers_are_removed(self):
        netlist = parse_netlist(
            self.xml(("1", "VOUT_1"), ("10", "FB_10"), ("2", "Pin_2_2")), "kicadxml"
        )

        assert [n.pinfunction for n in netlist.nets[0].nodes] == ["VOUT", "FB", "Pin_2"]

    def test_names_are_kept_when_not_every_pin_is_suffixed(self):
        netlist = parse_netlist(self.xml(("1", "VCC_1"), ("2", "GND")), "kicadxml")

        assert [n.pinfunction for n in netlist.nets[0].nodes] == ["VCC_1", "GND"]
