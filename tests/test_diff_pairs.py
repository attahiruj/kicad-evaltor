"""Finding differential pairs by name, the way KiCad does, and measuring them."""

import math

import pytest

from conftest import ESP_BOARD
from kicad_evaltor.board_file import BoardTrack, load_board
from kicad_evaltor.diff_pairs import (
    DiffPair,
    complement,
    find_diff_pairs,
    kicad_complement,
    measure_pair,
)
from kicad_evaltor.diff_pairs import _edge_gap


class TestKicadNaming:
    """A port of KiCad's DRC_ENGINE::MatchDpSuffix."""

    @pytest.mark.parametrize(
        ("net", "other", "polarity"),
        [
            ("USB_D+", "USB_D-", 1),
            ("USB_D-", "USB_D+", -1),
            ("TX_P", "TX_N", 1),
            ("TX_N", "TX_P", -1),
            # Trailing digits and underscores are skipped.
            ("LVDS_P0", "LVDS_N0", 1),
            ("DQS_P_1", "DQS_N_1", 1),
            ("/sheet/CLK+", "/sheet/CLK-", 1),
        ],
    )
    def test_pairs(self, net, other, polarity):
        _, complement_net, sign = kicad_complement(net)

        assert (complement_net, sign) == (other, polarity)

    @pytest.mark.parametrize("net", ["GND", "+3.3V", "/SD_CLK", "123", "", "tx_p"])
    def test_not_a_pair_half(self, net):
        assert kicad_complement(net) is None

    def test_a_name_ending_in_n_still_needs_its_partner(self):
        # KiCad reads /EN as the N half of /EP; with no /EP there is no pair.
        assert kicad_complement("/EN")[1] == "/EP"
        assert find_diff_pairs(["/EN", "GND"]) == []


class TestCustomSuffixes:
    def test_usb_dp_dm(self):
        assert complement("USB_DP", [("DP", "DM")]) == ("USB_", "USB_DM", 1)

    def test_longest_suffix_wins(self):
        suffixes = [("P", "N"), ("_DP", "_DM")]

        assert complement("USB_DM", suffixes) == ("USB", "USB_DP", -1)

    def test_lower_case(self):
        pairs = find_diff_pairs(["clk_p", "clk_n", "x"], suffixes=[("_p", "_n")])

        assert pairs == [DiffPair("clk", "clk_p", "clk_n")]


class TestFindPairs:
    def test_finds_each_pair_once_positive_first(self):
        nets = ["USB_D-", "USB_D+", "TX_N", "TX_P", "GND", "LONE+"]

        assert find_diff_pairs(nets) == [
            DiffPair("TX_", "TX_P", "TX_N"),
            DiffPair("USB_D", "USB_D+", "USB_D-"),
        ]

    def test_pattern_filters(self):
        nets = ["USB_D-", "USB_D+", "TX_N", "TX_P"]

        assert [p.base for p in find_diff_pairs(nets, pattern="USB*")] == ["USB_D"]

    def test_example_board_has_none(self):
        assert find_diff_pairs(load_board(ESP_BOARD).nets) == []


class TestMeasure:
    def test_uart_lines_on_the_example_board(self):
        board = load_board(ESP_BOARD)
        m = measure_pair(board, DiffPair("", "/RX0", "/TX0"), coupling_distance=0.5)

        # KiCad's DRC: closest RX0/TX0 track clearance 0.3364 mm; lengths
        # 45.0995 and 39.7294 mm with via height.
        assert m.gap_min == pytest.approx(0.3364, abs=1e-3)
        assert m.lengths["/RX0"] == pytest.approx(45.0995, abs=1e-3)
        assert m.skew == pytest.approx(5.37, abs=1e-3)
        assert m.coupled_length > 0
        assert m.uncoupled_length["/RX0"] > 0

    def test_edge_gap_of_parallel_tracks(self):
        other = BoardTrack(start=(0, 0.45), end=(10, 0.45), width=0.2, layer="F.Cu", net="B")

        assert _edge_gap((5, 0), 0.2, other) == pytest.approx(0.25)

    def test_edge_gap_to_an_arc(self):
        # Quarter arc of radius 1 about the origin; a point at the origin is
        # 1 mm from its centreline.
        mid = (math.cos(math.pi / 4), math.sin(math.pi / 4))
        arc = BoardTrack(start=(1, 0), end=(0, 1), mid=mid, width=0.2, layer="F.Cu", net="B")

        assert _edge_gap((0, 0), 0.2, arc) == pytest.approx(0.8, abs=1e-3)
