"""The board file reader, against the real example board.

Every expectation here was read off the board in KiCad 10 (or from pcbnew), not
made up: 46 footprints, 251 straight tracks, 162 vias, one two-layer GND zone.
"""

import math

import pytest

from kicad_evaltor.board_file import NetClass, NetClasses, arc_length, load_board
from kicad_evaltor.sexpr import parse
from conftest import ESP_BOARD


@pytest.fixture(scope="module")
def board():
    return load_board(ESP_BOARD)


class TestFootprints:
    def test_reads_every_footprint(self, board):
        assert len(board.footprints) == 46
        assert board.version == 20260206

    def test_reads_reference_value_and_library(self, board):
        u2 = board.footprint("U2")

        assert u2.value == "TPS63020"
        assert u2.lib_id == "Package_SON:VSON-14-1EP_3x4.45mm_P0.65mm_EP1.6x4.2mm"
        assert (u2.x, u2.y, u2.rotation, u2.side) == (113.0, 77.05, 0.0, "front")

    def test_pads_carry_net_and_pin_type(self, board):
        pad = board.footprint("U2").pad("1")

        assert (pad.net, pad.pin_function, pad.pin_type) == ("+3.3V", "VOUT_1", "power_out")

    def test_paste_only_pads_are_not_copper(self, board):
        r4 = board.footprint("R4")

        assert [p.number for p in r4.pads if not p.is_copper] == ["", ""]
        assert r4.pad("1").net == "+3.3V"

    def test_a_pad_with_no_net_reads_as_empty(self, board):
        # U2's exposed pad is on no net on this board.
        assert board.footprint("U2").pad("15").net == ""

    def test_reads_attributes(self, board):
        assert board.footprint("R4").smd
        assert board.footprint("H1").exclude_from_bom
        assert not board.footprint("H1").smd

    def test_reads_the_reference_field_style(self, board):
        text = board.footprint("U2").reference_text

        assert (text.layer, text.hidden, text.height, text.thickness) == (
            "F.SilkS",
            True,
            1.0,
            0.15,
        )

    def test_footprint_nets(self, board):
        assert board.footprint("R4").nets == {"+3.3V", "Net-(U2-FB)"}

    def test_drill_is_read_for_holes(self, board):
        assert max(p.drill or 0 for p in board.footprint("H1").pads) == 3.2


class TestCopper:
    def test_reads_tracks_vias_and_zones(self, board):
        assert len(board.tracks) == 251
        assert len(board.vias) == 162
        assert [(z.net, z.layers) for z in board.zones] == [("GND", ("F.Cu", "B.Cu"))]

    def test_kicad_10_names_nets_without_numbers(self, board):
        assert "GND" in board.nets
        assert "/SD_CLK" in board.nets
        assert "" not in board.nets

    def test_routed_length_matches_pcbnew(self, board):
        # pcbnew's PCB_TRACK.GetLength summed per net gives the same figures.
        assert board.routed_length("/SD_CLK") == pytest.approx(17.1402, abs=1e-4)
        assert board.routed_length("GND") == pytest.approx(32.339, abs=1e-3)

    def test_net_length_counts_via_height_as_kicad_does(self, board):
        # Read from KiCad 10's DRC (a length rule reports each net's actual
        # length): every via adds 1.58 mm, F.Cu to B.Cu with both coppers...
        assert board.net_length("/SD_CS") == pytest.approx(30.0499, abs=1e-4)
        assert board.net_length("+3.3V") == pytest.approx(139.3962, abs=1e-4)
        # ...except a via whose tracks are all on one layer: KiCad counts 127
        # of GND's 139 vias, the 12 it skips have tracks on one side only.
        assert board.net_length("GND") == pytest.approx(232.9987, abs=1e-4)
        assert board.net_length("/SD_CS", include_vias=False) == pytest.approx(26.8899, abs=1e-4)

    def test_layer_distance_spans_both_coppers(self, board):
        assert board.layer_distance("F.Cu", "B.Cu") == pytest.approx(0.035 + 1.51 + 0.035)
        assert board.uses_via_height

    def test_escaped_net_names_read_as_kicad_shows_them(self, board):
        # The file stores Net-(U2-PS{slash}SYNC); KiCad shows a slash.
        assert "Net-(U2-PS/SYNC)" in board.nets
        assert board.net_length("Net-(U2-PS/SYNC)") == pytest.approx(1.2953, abs=1e-4)

    def test_via_annular_ring(self, board):
        via = board.vias_on("+3.3V")[0]

        assert (via.size, via.drill, via.annular_ring) == (0.6, 0.3, pytest.approx(0.15))

    def test_stackup(self, board):
        assert board.copper_layers == ["F.Cu", "B.Cu"]
        assert board.thickness == 1.6
        assert board.copper_thickness("F.Cu") == 0.035


class TestNetClasses:
    def test_reads_classes_and_rules_from_the_project(self, board):
        classes = board.net_classes

        assert set(classes.classes) == {"Default", "3.7V", "GND", "SD", "UART", "VCC"}
        assert classes.rules["min_track_width"] == 0.2

    def test_a_composite_class_takes_values_by_priority(self, board):
        # pcbnew names +3.3V's class "VCC,Default": VCC sets the width, Default
        # the clearance VCC leaves unset.
        rule = board.net_classes.effective("VCC,Default")

        assert (rule.track_width, rule.clearance) == (0.3, 0.14)

    def test_lower_priority_number_wins(self):
        classes = NetClasses(
            {
                "Default": NetClass("Default", 1000, track_width=0.2),
                "A": NetClass("A", 5, track_width=0.4),
                "B": NetClass("B", 1, track_width=0.6),
            }
        )

        assert classes.effective("A,B").track_width == 0.6

    def test_missing_project_gives_no_classes(self, tmp_path):
        assert load_board(ESP_BOARD, project_path=tmp_path / "none.kicad_pro").net_classes == (
            NetClasses()
        )


class TestParsing:
    def test_older_numbered_net_form(self):
        from kicad_evaltor.board_file import _net_name

        assert _net_name(parse('(net 3 "GND")')) == "GND"
        assert _net_name(parse("(net 3)")) == ""
        assert _net_name(parse('(net "3")')) == "3"

    def test_rejects_a_schematic(self, tmp_path):
        bad = tmp_path / "x.kicad_pcb"
        bad.write_text("(kicad_sch)", encoding="utf-8")

        with pytest.raises(ValueError):
            load_board(bad)


class TestUnescape:
    def test_known_sequences(self):
        from kicad_evaltor.board_file import unescape

        assert unescape("A{slash}B{space}C{lt}1{gt}") == "A/B C<1>"
        assert unescape("plain") == "plain"
        assert unescape("{unknown}") == "{unknown}"


class TestArcLength:
    def test_quarter_circle(self):
        # Radius 5: start (5,0), mid at 45°, end (0,5).
        mid = (5 * math.cos(math.pi / 4), 5 * math.sin(math.pi / 4))

        assert arc_length((5, 0), mid, (0, 5)) == pytest.approx(math.pi * 5 / 2)

    def test_three_quarter_circle_goes_the_long_way(self):
        mid = (-5 * math.cos(math.pi / 4), -5 * math.sin(math.pi / 4))

        assert arc_length((5, 0), mid, (0, 5)) == pytest.approx(3 * math.pi * 5 / 2)

    def test_collinear_points_are_a_straight_line(self):
        assert arc_length((0, 0), (1, 0), (2, 0)) == pytest.approx(2)
