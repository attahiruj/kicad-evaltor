"""Tests for the kipy-to-domain adapter models.

These cover the duck-typed shims in Component/Pin/Net/Track, which have to
tolerate kipy objects that differ in which optional attributes they expose.
"""

from types import SimpleNamespace as NS

from kicad_evaltor.models import Component, Net, Pin, Track


class TestComponent:
    def test_optional_collections_default_to_empty_dicts(self):
        component = Component(reference="R1", lib_id="Device:R", value="10k")
        assert component.fields == {}
        assert component.properties == {}

    def test_explicit_collections_are_preserved(self):
        component = Component(
            reference="R1",
            lib_id="Device:R",
            value="10k",
            fields={"MPN": "RC0805"},
            properties={"spice": "R"},
        )
        assert component.fields == {"MPN": "RC0805"}
        assert component.properties == {"spice": "R"}

    def test_collections_are_not_shared_between_instances(self):
        first = Component(reference="R1", lib_id="Device:R", value="10k")
        second = Component(reference="R2", lib_id="Device:R", value="10k")
        first.fields["a"] = "b"
        assert second.fields == {}


class TestComponentFromSchematicSymbol:
    def test_maps_core_attributes(self):
        symbol = NS(reference="R1", lib_id="Device:R", value="10k", footprint=None)
        component = Component.from_schematic_symbol(symbol)
        assert (component.reference, component.lib_id, component.value) == ("R1", "Device:R", "10k")

    def test_extracts_footprint_text(self):
        symbol = NS(
            reference="R1",
            lib_id="Device:R",
            value="10k",
            footprint=NS(text=NS(value="Resistor_SMD:R_0805")),
        )
        assert Component.from_schematic_symbol(symbol).footprint == "Resistor_SMD:R_0805"

    def test_empty_footprint_text_passes_through_as_empty_string(self):
        symbol = NS(
            reference="R1",
            lib_id="Device:R",
            value="10k",
            footprint=NS(text=NS(value="")),
        )
        assert Component.from_schematic_symbol(symbol).footprint == ""

    def test_merges_user_and_standard_fields(self):
        symbol = NS(
            reference="R1",
            lib_id="Device:R",
            value="10k",
            footprint=None,
            user_fields={"MPN": "RC0805", "DNP": "no"},
            fields={"Reference": "R1"},
        )
        assert Component.from_schematic_symbol(symbol).fields == {
            "MPN": "RC0805",
            "DNP": "no",
            "Reference": "R1",
        }

    def test_standard_fields_win_over_user_fields_on_conflict(self):
        symbol = NS(
            reference="R1",
            lib_id="Device:R",
            value="10k",
            footprint=None,
            user_fields={"MPN": "user"},
            fields={"MPN": "standard"},
        )
        assert Component.from_schematic_symbol(symbol).fields["MPN"] == "standard"

    def test_tolerates_symbol_without_field_attributes(self):
        symbol = NS(reference="R1", lib_id="Device:R", value="10k")
        component = Component.from_schematic_symbol(symbol)
        assert component.fields == {}
        assert component.footprint is None

    def test_carries_sheet_when_present(self):
        symbol = NS(reference="R1", lib_id="Device:R", value="10k", sheet="root")
        assert Component.from_schematic_symbol(symbol).sheet == "root"

    def test_sheet_defaults_to_none_when_absent(self):
        symbol = NS(reference="R1", lib_id="Device:R", value="10k")
        assert Component.from_schematic_symbol(symbol).sheet is None


class TestComponentFromBoardFootprint:
    def test_uses_lib_id_as_footprint(self):
        footprint = NS(reference="R1", lib_id="Resistor_SMD:R_0805", value="10k")
        component = Component.from_board_footprint(footprint)
        assert component.footprint == "Resistor_SMD:R_0805"
        assert component.value == "10k"

    def test_missing_value_falls_back_to_empty_string(self):
        footprint = NS(reference="R1", lib_id="Resistor_SMD:R_0805")
        assert Component.from_board_footprint(footprint).value == ""


class TestPin:
    def test_optional_coordinates_default_to_none(self):
        assert Pin(ref="R1", pin="1", net="NET1").x is None
        assert Pin(ref="R1", pin="1", net="NET1").y is None

    def test_from_schematic_node(self):
        node = NS(ref="R1", pin="1", net="NET1", x=10.0, y=20.0)
        assert Pin.from_schematic_node(node) == Pin(ref="R1", pin="1", net="NET1", x=10.0, y=20.0)

    def test_from_schematic_node_without_coordinates(self):
        node = NS(ref="R1", pin="1", net="NET1")
        pin = Pin.from_schematic_node(node)
        assert (pin.x, pin.y) == (None, None)

    def test_from_board_pad(self):
        pad = NS(
            pad_number="2",
            net=NS(name="GND"),
            position=NS(x=1.0, y=2.0),
            parent=NS(reference="R1"),
        )
        assert Pin.from_board_pad(pad) == Pin(ref="R1", pin="2", net="GND", x=1.0, y=2.0)

    def test_from_board_pad_without_parent_reference(self):
        pad = NS(pad_number="1", net=NS(name="GND"), position=NS(x=0.0, y=0.0))
        assert Pin.from_board_pad(pad).ref == ""

    def test_from_unconnected_pad_yields_empty_net(self):
        pad = NS(pad_number="1", net=None, position=NS(x=0.0, y=0.0), parent=NS(reference="R1"))
        assert Pin.from_board_pad(pad).net == ""

    def test_from_board_pad_with_scalar_position(self):
        pad = NS(pad_number="1", net=NS(name="GND"), position=object(), parent=NS(reference="R1"))
        pin = Pin.from_board_pad(pad)
        assert (pin.x, pin.y) == (None, None)


class TestNet:
    def test_nodes_default_to_empty_list(self):
        assert Net(name="GND").nodes == []
        assert Net(name="GND").code is None

    def test_node_lists_are_not_shared(self):
        first, second = Net(name="GND"), Net(name="VCC")
        first.nodes.append(Pin(ref="R1", pin="1", net="GND"))
        assert second.nodes == []

    def test_from_schematic_net(self):
        node = NS(ref="R1", pin="1", net="GND", x=0.0, y=0.0)
        net = Net.from_schematic_net(NS(name="GND", code=0, nodes=[node]))
        assert net.name == "GND"
        assert net.code == 0
        assert net.nodes == [Pin(ref="R1", pin="1", net="GND", x=0.0, y=0.0)]

    def test_from_schematic_net_without_code(self):
        net = Net.from_schematic_net(NS(name="GND", nodes=[]))
        assert net.code is None

    def test_from_schematic_net_without_nodes_attribute(self):
        net = Net.from_schematic_net(NS(name="GND", code=1))
        assert net.nodes == []

    def test_from_board_net(self):
        net = Net.from_board_net(NS(name="GND", net_code=0))
        assert net == Net(name="GND", code=0)

    def test_from_board_net_has_no_nodes(self):
        net = Net.from_board_net(NS(name="GND", net_code=0))
        assert net.nodes == []


class TestTrackFromBoardTrack:
    def test_reads_length_from_get_length(self):
        track = NS(
            net=1,
            layer=NS(name="F.Cu"),
            start=NS(x=0.0, y=0.0),
            end=NS(x=1_000_000.0, y=0.0),
            width=200_000,
            get_length=lambda: 1_000_000,
        )
        parsed = Track.from_board_track(track)
        assert parsed.length == 1_000_000
        assert parsed.layer == "F.Cu"

    def test_falls_back_to_length_attribute(self):
        track = NS(
            net=1,
            layer=NS(name="B.Cu"),
            start=NS(x=0.0, y=0.0),
            end=NS(x=0.0, y=1_000_000.0),
            width=250_000,
            length=1_000_000,
        )
        assert Track.from_board_track(track).length == 1_000_000

    def test_length_stays_none_when_neither_is_exposed(self):
        track = NS(
            net=1,
            layer=NS(name="F.Cu"),
            start=NS(x=0.0, y=0.0),
            end=NS(x=0.0, y=0.0),
            width=200_000,
        )
        assert Track.from_board_track(track).length is None

    def test_scalar_layer_is_stringified(self):
        track = NS(
            net=1,
            layer=74,
            start=NS(x=0.0, y=0.0),
            end=NS(x=0.0, y=0.0),
            width=200_000,
        )
        assert Track.from_board_track(track).layer == "74"

    def test_copies_geometry_and_width(self):
        track = NS(
            net=3,
            layer=NS(name="F.Cu"),
            start=NS(x=1.0, y=2.0),
            end=NS(x=3.0, y=4.0),
            width=200_000,
        )
        parsed = Track.from_board_track(track)
        assert (parsed.net, parsed.start_x, parsed.start_y) == (3, 1.0, 2.0)
        assert (parsed.end_x, parsed.end_y, parsed.width) == (3.0, 4.0, 200_000)


class TestTrackFromBoardVia:
    def test_via_becomes_zero_length_segment(self):
        via = NS(net=5, position=NS(x=1.0, y=2.0), size=NS(width=600_000), height=1_200_000)
        parsed = Track.from_board_via(via)
        assert parsed.layer == "via"
        assert parsed.width == 600_000
        assert parsed.start_x == parsed.end_x == 1.0
        assert parsed.start_y == parsed.end_y == 2.0
        assert parsed.length == 1_200_000

    def test_falls_back_to_drill_for_width(self):
        via = NS(net=5, position=NS(x=0.0, y=0.0), size=object(), drill=300_000)
        parsed = Track.from_board_via(via)
        assert parsed.width == 300_000
        assert parsed.length == 0
