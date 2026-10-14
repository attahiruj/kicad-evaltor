"""Reachability across conducting parts, built on real FileNetlist objects."""

from conftest import FakeContext, FakeSchematic, netlist_of
from kicad_evaltor.checks.schematic.components_connected import ComponentsConnectedCheck
from kicad_evaltor.connectivity import (
    Connectivity,
    any_of,
    no_parts,
    pin_pairs,
    power_rails,
    ref_prefix,
    same_pin_name,
    series_parts,
    two_terminal_passives,
    whole_part,
)
from kicad_evaltor.netlist import FileNet, FileNetNode

# J1 D+ -> R1 (22R) -> U1 UD+; J1 D- -> R2 -> U1 UD-.
# R3 pulls D+ up to +3V3, which also feeds U1 and C1.
USB = netlist_of(
    ("/CONN_DP", [("J1", "3", "D+"), ("R1", "1")]),
    ("/CONN_DM", [("J1", "2", "D-"), ("R2", "1")]),
    ("/DP", [("R1", "2"), ("U1", "5", "UD+"), ("R3", "1")]),
    ("/DM", [("R2", "2"), ("U1", "6", "UD-")]),
    ("+3V3", [("R3", "2"), ("U1", "16", "VCC"), ("C1", "1")]),
    ("GND", [("C1", "2"), ("U1", "1", "GND"), ("J1", "5", "GND")]),
)


class TestRefPrefix:
    def test_takes_the_whole_letter_part(self):
        assert ref_prefix("R12") == "R"
        assert ref_prefix("FB3") == "FB"
        assert ref_prefix("#PWR01") == "#PWR"
        assert ref_prefix("12") == ""


class TestReach:
    def test_crosses_a_series_resistor(self):
        reach = Connectivity(USB).reach("/CONN_DP")

        assert reach.touches("U1", "5")
        assert not reach.touches("U1", "6")
        assert reach.crossed == ["R1"]

    def test_stops_at_power_rails(self):
        reach = Connectivity(USB).reach("/CONN_DP")

        # R3 leads to +3V3; entering it would join D+ to everything on the rail.
        assert "+3V3" not in reach.nets
        assert "+3V3" in reach.stopped
        assert not reach.touches("C1")

    def test_with_no_stop_the_rail_is_entered(self):
        reach = Connectivity(USB).reach("/CONN_DP", stop_at=lambda _net: False)

        assert "+3V3" in reach.nets
        assert reach.touches("C1")

    def test_no_parts_stays_on_the_start_net(self):
        reach = Connectivity(USB).reach("/CONN_DP", through=no_parts)

        assert reach.nets == {"/CONN_DP"}
        assert reach.crossed == []

    def test_starting_on_a_rail_returns_that_rail(self):
        assert "+3V3" in Connectivity(USB).reach("+3V3").nets

    def test_unknown_net_reaches_nothing(self):
        reach = Connectivity(USB).reach("nope")

        assert reach.nets == set()
        assert reach.nodes == []

    def test_reach_from_pin_and_connected(self):
        graph = Connectivity(USB)

        assert graph.reach_from_pin("J1", "3").touches("U1", "5")
        assert graph.connected(("J1", "3"), ("U1", "5"))
        assert not graph.connected(("J1", "3"), ("U1", "6"))
        assert not graph.connected(("J1", "99"), ("U1", "5"))

    def test_neighbors(self):
        assert [(n.ref, n.pin) for n in Connectivity(USB).neighbors("R1", "2")] == [
            ("U1", "5"),
            ("R3", "1"),
        ]


class TestBridges:
    def test_series_parts_ignore_a_three_pin_part_with_the_prefix(self):
        netlist = netlist_of(
            ("A", [("RV1", "1"), ("R1", "1")]),
            ("B", [("RV1", "2")]),
            ("C", [("RV1", "3")]),
        )
        assert series_parts("RV")(netlist.components["RV1"], netlist.pins_of("RV1")) == []
        assert Connectivity(netlist).reach("A", through=series_parts("RV")).nets == {"A"}

    def test_default_takes_inductors_and_ferrite_beads(self):
        netlist = netlist_of(
            ("A", [("FB1", "1")]), ("B", [("FB1", "2"), ("L1", "1")]), ("C", [("L1", "2")])
        )

        assert Connectivity(netlist).reach("A", through=two_terminal_passives).nets == {
            "A",
            "B",
            "C",
        }

    def test_pin_pairs_joins_only_the_named_channels(self):
        # A four-channel flow-through ESD array: 1<->10 and 2<->9, ground on 5.
        netlist = netlist_of(
            ("DP_IN", [("J1", "3"), ("U2", "1")]),
            ("DP_OUT", [("U2", "10"), ("U1", "5")]),
            ("DM_IN", [("J1", "2"), ("U2", "2")]),
            ("DM_OUT", [("U2", "9"), ("U1", "6")]),
            ("SHIELD", [("U2", "5")]),
            components={"U2": "Power_Protection:TPD4E05U06DQA"},
        )
        graph = Connectivity(netlist)

        by_ref = graph.reach("DP_IN", through=pin_pairs("U2", ("1", "10"), ("2", "9")))
        by_lib = graph.reach("DP_IN", through=pin_pairs("Power_Protection:TPD4E*", ("1", "10")))

        assert by_ref.nets == {"DP_IN", "DP_OUT"}
        assert by_lib.nets == {"DP_IN", "DP_OUT"}
        # Treating the array as one conductor would short every channel together.
        assert "DM_OUT" in graph.reach("DP_IN", through=whole_part("U2")).nets

    def test_whole_part_joins_every_pin(self):
        netlist = netlist_of(("A", [("JP1", "1")]), ("B", [("JP1", "2")]))

        assert Connectivity(netlist).reach("A", through=whole_part("JP1")).nets == {"A", "B"}

    def test_same_pin_name_pairs_channels_by_name(self):
        netlist = netlist_of(
            ("A_IN", [("U2", "1", "IO1")]),
            ("A_OUT", [("U2", "6", "IO1")]),
            ("B_IN", [("U2", "3", "IO2")]),
        )

        assert Connectivity(netlist).reach("A_IN", through=same_pin_name("U2")).nets == {
            "A_IN",
            "A_OUT",
        }

    def test_any_of_combines(self):
        netlist = netlist_of(
            ("A", [("R1", "1")]), ("B", [("R1", "2"), ("JP1", "1")]), ("C", [("JP1", "2")])
        )
        through = any_of(two_terminal_passives, whole_part("JP1"))

        assert Connectivity(netlist).reach("A", through=through).nets == {"A", "B", "C"}


class TestPowerRails:
    def test_by_pin_type(self):
        assert power_rails(
            FileNet("/LDO_OUT", [FileNetNode("U1", "2", "/LDO_OUT", pintype="power_out")])
        )

    def test_by_name(self):
        for name in ("GND", "AGND", "/sub/GND", "+3V3", "+3.3V", "5V", "VBUS", "VCC_IO", "-12V"):
            assert power_rails(FileNet(name, [])), name

    def test_signals_are_not_rails(self):
        for name in ("/DP", "SDA", "Net-(R1-Pad2)", "/VOUT_SENSE_N", "USB_D+"):
            assert not power_rails(FileNet(name, [])), name


class TestComponentsConnectedThroughPassives:
    def ctx(self):
        return FakeContext(schematic=FakeSchematic(netlist=USB))

    def test_direct_comparison_misses_a_series_resistor(self):
        result = ComponentsConnectedCheck(reference_a="J1", reference_b="U1").run(self.ctx())

        # They share GND only.
        assert result.details["shared_nets"] == ["GND"]

    def test_through_passives_counts_the_data_lines(self):
        check = ComponentsConnectedCheck(
            reference_a="J1", reference_b="U1", min_connections=3, through_passives=True
        )
        result = check.run(self.ctx())

        assert result.is_pass
        assert sorted(result.details["shared_nets"]) == ["/CONN_DM", "/CONN_DP", "GND"]
