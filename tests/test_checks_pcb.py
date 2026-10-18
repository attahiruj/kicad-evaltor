"""Board checks against the real example board, with no KiCad installed.

The board file is read for real. What KiCad itself would say (DRC reports and
pcbnew geometry) is replayed from recordings of that board and of copies with
one thing broken; see ``tools/record_pcb_fixtures.py``. The expected numbers
are the board's own: they were read off it in KiCad 10, not invented.
"""

import pytest

from conftest import (
    ESP_BOARD,
    FakeContext,
    FakeSchematic,
    cli_result,
    drc_report,
    recorded_drc,
    recorded_geometry,
    violation,
)
from kicad_evaltor.board_file import load_board
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.pcb import (
    BoardOutlineCheck,
    CurrentCapacityCheck,
    DRCRunCheck,
    DiffPairCheck,
    FootprintExistsCheck,
    FootprintMatchesLibraryCheck,
    FootprintMatchesSchematicCheck,
    FootprintNearCheck,
    FootprintOnBoardCheck,
    FootprintOverlapCheck,
    FootprintPlacementCheck,
    LengthMatchCheck,
    MountingHolesCheck,
    NetLayersCheck,
    NetViasCheck,
    PlaneCoverageCheck,
    PowerPourCheck,
    ReferenceVisibleCheck,
    RoutingCompleteCheck,
    SchematicParityCheck,
    SilkOverCopperCheck,
    SilkOverlapCheck,
    TextOffBoardCheck,
    ThermalViasCheck,
    TraceLengthCheck,
    TraceWidthCheck,
    TuningRulesCheck,
    ViaRulesCheck,
)
from kicad_evaltor.checks.pcb.copper import ipc2221_width
from kicad_evaltor.core.runner import TestRunner
from kicad_evaltor.schematic_file import load_schematic

BOARD = load_board(ESP_BOARD)


def ctx(drc: str | None = "base", geometry: str | None = "base", schematic=None) -> FakeContext:
    return FakeContext(
        board=BOARD,
        schematic=schematic,
        geometry=recorded_geometry(geometry) if geometry else None,
        cli_results=recorded_drc(drc) if drc else None,
        # The real path, so kicad-cli's schematic (beside the board) exists.
        board_path=str(ESP_BOARD),
    )


def no_board() -> FakeContext:
    return FakeContext()


class TestDRC:
    def test_clean_board_passes(self):
        assert DRCRunCheck(severity="all").run(ctx()).status == TestStatus.PASS

    def test_counts_unrouted_connections(self):
        # The report's violations list is empty; the finding is only in
        # unconnected_items, which a violations-only parser never sees.
        result = DRCRunCheck().run(ctx("unrouted"))

        assert result.status == TestStatus.FAIL
        assert result.details["violations"][0]["source"] == "unconnected_items"

    def test_counts_schematic_parity_findings(self):
        result = DRCRunCheck(severity="all").run(ctx("parity"))

        assert {v["type"] for v in result.details["violations"]} == {
            "missing_footprint",
            "track_dangling",
        }

    def test_include_and_exclude_types(self):
        only = DRCRunCheck(severity="all", include_types=["courtyards_overlap"]).run(ctx("overlap"))
        without = DRCRunCheck(severity="all", exclude_types=["unconnected_items"]).run(
            ctx("unrouted")
        )

        assert {v["type"] for v in only.details["violations"]} == {"courtyards_overlap"}
        assert without.status == TestStatus.PASS

    def test_parity_flag_takes_no_path(self):
        context = ctx(schematic=FakeSchematic())
        DRCRunCheck().run(context)
        args = context.cli_calls[0]

        assert "--schematic-parity" in args
        assert args[args.index("--schematic-parity") + 1].startswith("-")

    def test_parity_omitted_without_a_schematic_beside_the_board(self):
        # kicad-cli finds the schematic by the board's name, so a schematic given
        # to the context somewhere else does not count.
        context = FakeContext(
            board=BOARD,
            schematic=FakeSchematic(),
            cli_results=recorded_drc(),
            board_path="elsewhere/design.kicad_pcb",
        )
        DRCRunCheck().run(context)

        assert "--schematic-parity" not in context.cli_calls[0]

    def test_board_path_is_absolute(self):
        # kicad-cli runs from a scratch folder, so a relative path would miss.
        context = ctx()
        DRCRunCheck().run(context)

        assert context.cli_calls[0][-1] == str(context.board_path.resolve())

    def test_one_drc_run_serves_every_drc_backed_check(self):
        context = ctx()
        TestRunner(
            [DRCRunCheck(), RoutingCompleteCheck(), SilkOverlapCheck(), FootprintOverlapCheck()]
        ).run(context)

        assert len(context.cli_calls) == 1

    def test_cli_failure_is_a_skip_unless_strict(self):
        failing = FakeContext(board=BOARD, cli_results=cli_result(returncode=1, stderr="boom"))

        assert DRCRunCheck().run(failing).status == TestStatus.SKIP
        failing = FakeContext(board=BOARD, cli_results=cli_result(returncode=1, stderr="boom"))
        assert DRCRunCheck(strict=True).run(failing).status == TestStatus.ERROR

    def test_severity_filter(self):
        report = drc_report(violation("warning", "w"), violation("error", "e"))
        context = FakeContext(board=BOARD, cli_results=report)

        result = DRCRunCheck(severity="warning").run(context)

        assert [v["severity"] for v in result.details["violations"]] == ["warning"]

    def test_skips_without_board(self):
        assert DRCRunCheck().run(no_board()).status == TestStatus.SKIP


class TestDrcRules:
    def test_routing_complete_on_the_clean_board(self):
        assert RoutingCompleteCheck().run(ctx()).status == TestStatus.PASS

    def test_routing_reports_the_open_net(self):
        result = RoutingCompleteCheck().run(ctx("unrouted"))

        assert result.status == TestStatus.FAIL
        assert [f["net"] for f in result.details["findings"]] == ["/SD_CLK"]

    def test_parity_reports_the_missing_footprint(self):
        result = SchematicParityCheck().run(ctx("parity", schematic=FakeSchematic()))

        assert result.status == TestStatus.FAIL
        assert [f["type"] for f in result.details["findings"]] == ["missing_footprint"]

    def test_parity_skips_without_a_schematic_beside_the_board(self):
        context = FakeContext(
            board=BOARD,
            schematic=FakeSchematic(),
            cli_results=recorded_drc("parity"),
            board_path="elsewhere/design.kicad_pcb",
        )

        result = SchematicParityCheck().run(context)

        assert result.status == TestStatus.SKIP
        assert "design.kicad_sch" in result.message

    def test_library_silk_checks_pass_on_the_clean_board(self):
        for check in (FootprintMatchesLibraryCheck(), SilkOverCopperCheck(), SilkOverlapCheck()):
            assert check.run(ctx()).status == TestStatus.PASS, check.id

    def test_silk_over_copper_found_after_moving_a_part(self):
        assert SilkOverCopperCheck().run(ctx("overlap")).status == TestStatus.FAIL

    def test_references_filter_findings(self):
        assert SilkOverCopperCheck(references=["U1"]).run(ctx("overlap")).status == TestStatus.PASS

    def test_drc_failure_is_an_error(self):
        failing = FakeContext(board=BOARD, cli_results=cli_result(returncode=1, stderr="boom"))

        assert RoutingCompleteCheck().run(failing).status == TestStatus.ERROR


class TestFootprintExists:
    def test_by_reference_and_lib_id(self):
        assert FootprintExistsCheck(reference="U1").run(ctx()).status == TestStatus.PASS
        assert (
            FootprintExistsCheck(lib_id="RF_Module:ESP32-C6-MINI-1").run(ctx()).status
            == TestStatus.PASS
        )

    def test_unknown_reference_fails(self):
        assert FootprintExistsCheck(reference="U9").run(ctx()).status == TestStatus.FAIL

    def test_criteria_combine(self):
        check = FootprintExistsCheck(reference="U1", lib_id="Resistor_SMD:R_0201_0603Metric")

        assert check.run(ctx()).status == TestStatus.FAIL

    def test_needs_a_criterion(self):
        with pytest.raises(ValueError):
            FootprintExistsCheck()


class TestTraceWidth:
    def test_net_width_within_limits(self):
        result = TraceWidthCheck(net_name="/SD_CLK", min_width_mm=0.25).run(ctx())

        assert result.status == TestStatus.PASS

    def test_reports_narrow_tracks_with_position(self):
        result = TraceWidthCheck(net_name="+3.7V", min_width_mm=0.5).run(ctx())

        assert result.status == TestStatus.FAIL
        finding = result.details["violations"][0]
        assert set(finding) == {"net", "layer", "start", "end", "width_mm", "issue"}

    def test_net_class_selection(self):
        assert TraceWidthCheck(netclass="SD", min_width_mm=0.25).run(ctx()).status == (
            TestStatus.PASS
        )

    def test_match_netclass_finds_the_board_s_thin_3v7_tracks(self):
        # Class 3.7V asks for 0.5 mm; 17 tracks on +3.7V are 0.3 mm. KiCad's DRC
        # does not enforce a class width as a minimum, so only this check sees it.
        result = TraceWidthCheck(match_netclass=True).run(ctx())

        assert result.status == TestStatus.FAIL
        assert {v["net"] for v in result.details["violations"]} == {"+3.7V"}
        assert len(result.details["violations"]) == 17

    def test_by_reference(self):
        result = TraceWidthCheck(reference="R4", min_width_mm=0.2).run(ctx())

        assert result.status == TestStatus.PASS

    def test_net_class_without_kicad_is_an_error(self):
        result = TraceWidthCheck(netclass="SD", min_width_mm=0.2).run(ctx(geometry=None))

        assert result.status == TestStatus.ERROR

    def test_unknown_net_fails(self):
        assert TraceWidthCheck(net_name="NOPE", min_width_mm=0.1).run(ctx()).status == (
            TestStatus.FAIL
        )

    def test_validation(self):
        with pytest.raises(ValueError):
            TraceWidthCheck(net_name="GND")
        with pytest.raises(ValueError):
            TraceWidthCheck(min_width_mm=0.2)


class TestTraceLength:
    def test_net_length(self):
        result = TraceLengthCheck(net="/SD_CLK", max_length_mm=20).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["length_mm"] == pytest.approx(17.1402, abs=1e-4)

    def test_net_too_long(self):
        assert TraceLengthCheck(net="/SD_CLK", max_length_mm=10).run(ctx()).status == (
            TestStatus.FAIL
        )

    def test_pad_to_pad_route(self):
        result = TraceLengthCheck(from_pad="U2.1", to_pad="C9.1", max_length_mm=5).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["length_mm"] == pytest.approx(2.5953, abs=1e-3)

    def test_pads_on_different_nets_fail(self):
        result = TraceLengthCheck(from_pad="U2.1", to_pad="U2.3", max_length_mm=5).run(ctx())

        assert result.status == TestStatus.FAIL

    def test_reference_is_rejected_with_a_pointer(self):
        with pytest.raises(ValueError, match="from_pad"):
            TraceLengthCheck(reference="U1", max_length_mm=10)

    def test_via_height_follows_the_project(self):
        # The project counts via height, as KiCad does: /SD_CS's two vias add
        # 1.58 mm each to 26.89 mm of track. KiCad's DRC reports 30.0499 mm.
        counted = TraceLengthCheck(net="/SD_CS", max_length_mm=100).run(ctx())
        tracks_only = TraceLengthCheck(net="/SD_CS", max_length_mm=100, include_vias=False).run(
            ctx()
        )

        assert counted.details["length_mm"] == pytest.approx(30.0499, abs=1e-4)
        assert counted.details["via_height_counted"] is True
        assert tracks_only.details["length_mm"] == pytest.approx(26.8899, abs=1e-4)

    def test_target_length(self):
        assert (
            TraceLengthCheck(net="/SD_CS", target_length_mm=30.05, tolerance_mm=0.01)
            .run(ctx())
            .status
            == TestStatus.PASS
        )
        assert (
            TraceLengthCheck(net="/SD_CS", target_length_mm=25, tolerance_mm=1).run(ctx()).status
            == TestStatus.FAIL
        )

    def test_target_and_limits_do_not_mix(self):
        with pytest.raises(ValueError):
            TraceLengthCheck(net="GND", target_length_mm=5, tolerance_mm=1, max_length_mm=9)
        with pytest.raises(ValueError):
            TraceLengthCheck(net="GND", target_length_mm=5)


class TestNetRules:
    def test_via_count(self):
        result = NetViasCheck(net="GND", max_vias=139).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["vias"] == 139
        assert NetViasCheck(net="GND", max_vias=100).run(ctx()).status == TestStatus.FAIL

    def test_layers(self):
        assert NetLayersCheck(net="/SD_CLK", allowed_layers=["F.Cu"]).run(ctx()).status == (
            TestStatus.PASS
        )
        assert NetLayersCheck(net="/SD_CLK", required_layers=["B.Cu"]).run(ctx()).status == (
            TestStatus.FAIL
        )

    def test_length_match_over_the_sd_bus(self):
        # KiCad's skew rule on these nets reports /SD_CLK 22.0315 mm short of
        # the longest, /SD_CD.
        result = LengthMatchCheck(pattern="/SD_*", max_skew_mm=25).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["skew_mm"] == pytest.approx(22.0316, abs=1e-3)
        assert result.details["longest"] == "/SD_CD"
        assert result.details["skew_to_longest_mm"]["/SD_CLK"] == pytest.approx(-22.0316, abs=1e-3)
        assert LengthMatchCheck(pattern="/SD_*", max_skew_mm=5).run(ctx()).status == (
            TestStatus.FAIL
        )

    def test_length_match_tracks_only(self):
        result = LengthMatchCheck(pattern="/SD_*", max_skew_mm=25, include_vias=False).run(ctx())

        assert result.details["skew_mm"] == pytest.approx(18.8716, abs=1e-3)

    def test_length_match_to_a_target(self):
        result = LengthMatchCheck(pattern="/SD_*", target_length_mm=30, tolerance_mm=2).run(ctx())

        assert result.status == TestStatus.FAIL
        assert set(result.details["off_target"]) == {"/SD_CD", "/SD_CLK", "/SD_MOSI"}

    def test_length_match_needs_two_nets(self):
        with pytest.raises(ValueError):
            LengthMatchCheck(nets=["/SD_CLK"], max_skew_mm=1)
        with pytest.raises(ValueError):
            LengthMatchCheck(pattern="/SD_*")


class TestDiffPair:
    # The example board has no pair named as one, so the measuring is shown on
    # the UART lines /RX0 and /TX0, which run side by side to J2. KiCad's DRC
    # puts their closest tracks 0.3364 mm apart, as measured here.
    UART = [["/RX0", "/TX0"]]

    def test_no_pairs_on_the_board(self):
        result = DiffPairCheck().run(ctx(geometry=None))

        assert result.status == TestStatus.FAIL
        assert "No differential pairs" in result.message

    def test_measures_an_explicit_pair(self):
        result = DiffPairCheck(pairs=self.UART, gap_mm=0.34, gap_tolerance_mm=0.2).run(ctx())

        [pair] = result.details["pairs"]
        assert pair["lengths_mm"] == {
            "/RX0": pytest.approx(45.0995),
            "/TX0": pytest.approx(39.7295),
        }
        assert pair["skew_mm"] == pytest.approx(5.37, abs=1e-3)
        assert pair["gap_mm"]["min"] == pytest.approx(0.3364, abs=1e-3)
        assert pair["vias"] == {"/RX0": 3, "/TX0": 2}

    def test_reports_each_rule_broken(self):
        result = DiffPairCheck(
            pairs=self.UART,
            max_skew_mm=5,
            width_mm=0.2,
            gap_mm=0.34,
            gap_tolerance_mm=0.05,
            max_uncoupled_mm=5,
            max_via_mismatch=0,
        ).run(ctx())

        issues = result.details["pairs"][0]["issues"]
        assert result.status == TestStatus.FAIL
        assert any(i.startswith("skew 5.370mm") for i in issues)
        assert any("track width 0.3mm" in i for i in issues)
        assert any("gap widens" in i for i in issues)
        assert any("uncoupled" in i for i in issues)
        assert any("via counts differ by 1" in i for i in issues)

    def test_passes_within_loose_limits(self):
        result = DiffPairCheck(
            pairs=self.UART,
            max_skew_mm=6,
            width_mm=0.25,
            width_tolerance_mm=0.05,
            gap_mm=0.4,
            gap_tolerance_mm=0.1,
            coupling_distance_mm=0.5,
        ).run(ctx())

        assert result.status == TestStatus.PASS

    def test_gap_and_width_default_to_the_net_class(self):
        # Default class: diff-pair gap 0.25 mm, width 0.2 mm.
        result = DiffPairCheck(pairs=self.UART).run(ctx())

        pair = result.details["pairs"][0]
        assert pair["gap_mm"]["expected"] == 0.25
        assert any("track width" in i for i in pair["issues"])

    def test_net_class_without_kicad_is_an_error(self):
        assert DiffPairCheck(pairs=self.UART).run(ctx(geometry=None)).status == TestStatus.ERROR

    def test_explicit_values_need_no_kicad(self):
        result = DiffPairCheck(pairs=self.UART, gap_mm=0.4, width_mm=0.2).run(ctx(geometry=None))

        assert result.status == TestStatus.FAIL
        assert result.details["pairs"][0]["skew_mm"] == pytest.approx(5.37, abs=1e-3)

    def test_unknown_net(self):
        assert DiffPairCheck(pairs=[["A+", "A-"]], gap_mm=0.2).run(ctx()).status == TestStatus.FAIL

    def test_validation(self):
        with pytest.raises(ValueError):
            DiffPairCheck(pairs=[["A+"]])
        with pytest.raises(ValueError):
            DiffPairCheck(suffixes=[["_p", ""]])


class TestTuningRules:
    def test_passes_without_custom_rules(self):
        assert TuningRulesCheck().run(ctx()).status == TestStatus.PASS

    def test_reports_kicad_length_and_skew_rules(self):
        result = TuningRulesCheck().run(ctx("tuning"))

        assert result.status == TestStatus.FAIL
        assert sorted({f["type"] for f in result.details["findings"]}) == [
            "length_out_of_range",
            "skew_out_of_range",
        ]


class TestFootprintOverlap:
    def test_clean_board_has_no_overlap(self):
        assert FootprintOverlapCheck().run(ctx()).status == TestStatus.PASS

    def test_overlap_comes_from_drc(self):
        result = FootprintOverlapCheck().run(ctx("overlap"))

        pairs = {
            frozenset((v["footprint_a"], v["footprint_b"])) for v in result.details["violations"]
        }
        assert result.status == TestStatus.FAIL
        assert frozenset(("C4", "C5")) in pairs

    def test_min_clearance_measures_courtyard_gaps(self):
        # R10 and R4's courtyards are 0.03 mm apart on the real board.
        result = FootprintOverlapCheck(min_clearance_mm=0.2).run(ctx())

        pairs = {
            frozenset((v["footprint_a"], v["footprint_b"])): v["clearance_mm"]
            for v in result.details["violations"]
        }
        assert pairs[frozenset(("R10", "R4"))] == pytest.approx(0.03, abs=1e-3)

    def test_exclusions(self):
        assert FootprintOverlapCheck(exclude_refs=["C5"]).run(ctx("overlap")).status == (
            TestStatus.PASS
        )


class TestMechanical:
    def test_outline_is_the_50mm_square(self):
        result = BoardOutlineCheck(width_mm=50, height_mm=50).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["area_mm2"] == pytest.approx(2478.079, abs=0.01)

    def test_outline_size_mismatch(self):
        assert BoardOutlineCheck(max_width_mm=40).run(ctx()).status == TestStatus.FAIL

    def test_on_board_flags_the_moved_test_point(self):
        result = FootprintOnBoardCheck(exclude_refs=["U1"]).run(ctx(geometry="offboard"))

        assert result.status == TestStatus.FAIL
        assert [f["reference"] for f in result.details["footprints"]] == ["TP3"]

    def test_on_board_sees_the_module_antenna_overhang(self):
        # U1's courtyard (module plus antenna clearance) runs past the top edge.
        result = FootprintOnBoardCheck().run(ctx())

        assert [f["reference"] for f in result.details["footprints"]] == ["U1"]

    def test_placement(self):
        assert (
            FootprintPlacementCheck(reference="J2", side="front", rotation=270).run(ctx()).status
            == TestStatus.PASS
        )
        result = FootprintPlacementCheck(reference="J2", at_edge=True).run(ctx())
        assert result.status == TestStatus.FAIL
        assert result.details["edge_distance_mm"] == pytest.approx(2.205, abs=1e-3)

    def test_position(self):
        check = FootprintPlacementCheck(reference="U2", x_mm=113.0, y_mm=77.05)

        assert check.run(ctx()).status == TestStatus.PASS

    def test_mounting_holes(self):
        result = MountingHolesCheck(min_count=4, diameter_mm=3.2, min_edge_distance_mm=3).run(ctx())

        assert result.status == TestStatus.PASS
        assert MountingHolesCheck(diameter_mm=2.5).run(ctx()).status == TestStatus.FAIL


class TestProximity:
    def test_near(self):
        result = FootprintNearCheck(reference="C6", target="U1", max_distance_mm=2).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["distance_mm"] == pytest.approx(1.55, abs=1e-3)

    def test_near_a_pin(self):
        result = FootprintNearCheck(reference="C6", target="U1.3", max_distance_mm=1).run(ctx())

        assert result.status == TestStatus.FAIL

    def test_decoupling_pairs_caps_with_power_pins(self):
        result = FootprintNearCheck(decoupling="U2", max_distance_mm=1).run(ctx())

        assert result.status == TestStatus.PASS
        [pin] = result.details["power_pins"]
        assert (pin["net"], pin["nearest_capacitor"]) == ("+3.7V", "C3")

    def test_decoupling_too_far(self):
        result = FootprintNearCheck(decoupling="U1", max_distance_mm=1).run(ctx())

        assert result.status == TestStatus.FAIL

    def test_needs_kicad_python(self):
        result = FootprintNearCheck(decoupling="U1").run(ctx(geometry=None))

        assert result.status == TestStatus.ERROR


class TestCopper:
    def test_pour_area_and_connection(self):
        result = PowerPourCheck(net_name="GND", min_area_mm2=3000).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["connected_pads"] == 54

    def test_pour_layer_and_missing_net(self):
        assert (
            PowerPourCheck(net_name="GND", layer="B.Cu", min_area_mm2=2000).run(ctx()).status
            == TestStatus.FAIL
        )
        assert PowerPourCheck(net_name="+3.3V").run(ctx()).status == TestStatus.FAIL

    def test_plane_coverage(self):
        result = PlaneCoverageCheck(net="GND", layer="B.Cu", min_fraction=0.75).run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["fraction"] == pytest.approx(0.7882, abs=1e-3)

    def test_ipc2221_matches_kicads_calculator(self):
        # KiCad's PCB Calculator: 1 A, 10 °C rise, 35 µm outer copper -> 0.3 mm.
        assert ipc2221_width(1.0, 10.0, 0.035, external=True) == pytest.approx(0.3004, abs=1e-3)

    def test_current_capacity(self):
        assert CurrentCapacityCheck(net="+3.7V", current_a=0.5).run(ctx()).status == (
            TestStatus.PASS
        )
        assert CurrentCapacityCheck(net="+3.7V", current_a=2).run(ctx()).status == (TestStatus.FAIL)

    def test_vias_meet_the_board_s_own_rules(self):
        result = ViaRulesCheck().run(ctx())

        assert result.status == TestStatus.PASS
        assert result.details["rules"]["min_annular_ring_mm"] == 0.15

    def test_via_minimums(self):
        assert ViaRulesCheck(min_drill_mm=0.4).run(ctx()).status == TestStatus.FAIL

    def test_thermal_pad_without_vias(self):
        # U2's exposed pad has no net and no vias on this board.
        result = ThermalViasCheck(reference="U2", min_vias=1).run(ctx())

        assert result.status == TestStatus.FAIL
        assert (result.details["pad"], result.details["pad_net"]) == ("15", "")


class TestSilkscreen:
    def test_hidden_references(self):
        result = ReferenceVisibleCheck(exclude=["FID*"]).run(ctx())

        assert result.status == TestStatus.FAIL
        assert {"reference": "U2", "issue": "hidden"} in result.details["issues"]

    def test_text_on_board(self):
        assert TextOffBoardCheck().run(ctx()).status == TestStatus.PASS


class TestMatchesSchematic:
    def test_footprints_match_their_symbols(self):
        schematic = load_schematic(ESP_BOARD.with_suffix(".kicad_sch"))
        context = FakeContext(board=BOARD, schematic=schematic)

        result = FootprintMatchesSchematicCheck(check_pins=False).run(context)

        assert result.status == TestStatus.PASS
        assert result.details["checked"] == 46


class TestReviewRegressions:
    """One test per bug found in review, so none comes back."""

    def test_a_failed_drc_run_is_not_reused(self):
        # A first check's timeout or failure must not be handed to the next.
        context = FakeContext(
            board=BOARD,
            board_path=str(ESP_BOARD),
            cli_results=[cli_result(returncode=1, stderr="boom"), recorded_drc()],
        )

        assert RoutingCompleteCheck().run(context).status == TestStatus.ERROR
        assert RoutingCompleteCheck().run(context).status == TestStatus.PASS
        assert len(context.cli_calls) == 2

    def test_a_missing_drc_report_is_an_error_not_a_skip(self):
        # kicad-cli exits 0 but writes nothing: a broken run, never a pass.
        context = FakeContext(
            board=BOARD, board_path=str(ESP_BOARD), cli_results=cli_result(stdout="done")
        )

        assert DRCRunCheck().run(context).status == TestStatus.ERROR

    @pytest.mark.parametrize(
        ("net", "ground"),
        [
            ("GND", True),
            ("/AGND", True),
            ("PGND_1", True),
            ("GNDA", True),
            ("VSSA", True),
            ("0V", True),
            ("+10V", False),
            ("VCC_10V", False),
            ("+3.3V", False),
            ("/SD_CLK", False),
        ],
    )
    def test_ground_names(self, net, ground):
        from kicad_evaltor.checks.pcb.proximity import is_ground

        assert is_ground(net) is ground

    def test_kicad_9_net_codes(self, tmp_path):
        board = tmp_path / "old.kicad_pcb"
        board.write_text(
            '(kicad_pcb (version 20240108) (net 0 "") (net 1 "GND") (net 2 "/A{slash}B")'
            ' (segment (start 0 0) (end 3 4) (width 0.2) (layer "F.Cu") (net 1))'
            ' (via (at 3 4) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2))'
            ' (zone (net 1) (net_name "GND") (layer "F.Cu")))',
            encoding="utf-8",
        )
        old = load_board(board)

        assert old.tracks[0].net == "GND"
        assert old.routed_length("GND") == pytest.approx(5.0)
        assert old.vias[0].net == "/A/B"
        assert old.zones[0].net == "GND"

    def test_removing_tracks_matches_escaped_names(self):
        from kicad_evaltor.edit import load_document, remove_net_tracks

        document = load_document(ESP_BOARD)

        # Stored as Net-(U2-PS{slash}SYNC).
        assert remove_net_tracks(document, "Net-(U2-PS/SYNC)") > 0

    def test_a_t_junction_off_the_centreline_still_joins(self, tmp_path):
        from kicad_evaltor.board_connectivity import net_graph

        board = tmp_path / "t.kicad_pcb"
        # B ends 0.05 mm off A's centreline, inside A's 0.25 mm width.
        board.write_text(
            "(kicad_pcb (version 20260206)"
            ' (segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net "N"))'
            ' (segment (start 5 0.05) (end 5 5) (width 0.25) (layer "F.Cu") (net "N")))',
            encoding="utf-8",
        )
        graph = net_graph(load_board(board), "N")

        assert graph.shortest([(0.0, 0.0, "F.Cu")], [(5.0, 5.0, "F.Cu")]) == pytest.approx(
            5 + 0.05 + 4.95
        )

    def test_a_route_may_land_on_any_pad_of_a_number(self):
        from kicad_evaltor.board_connectivity import NetGraph, pad_nodes
        from kicad_evaltor.pcbnew_bridge import PadGeometry

        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        far = tuple((x + 10, y) for x, y in square)
        graph = NetGraph("N")
        graph.add((10.5, 0.5, "F.Cu"), (20.0, 0.5, "F.Cu"), 9.5)
        pads = [
            PadGeometry("33", "N", 0.5, 0.5, ("F.Cu",), None, square),
            PadGeometry("33", "N", 10.5, 0.5, ("F.Cu",), None, far),
        ]

        assert pad_nodes(graph, pads) == [(10.5, 0.5, "F.Cu")]

    def test_microvias_have_their_own_minimums(self, tmp_path):
        board = tmp_path / "hdi.kicad_pcb"
        board.write_text(
            "(kicad_pcb (version 20260206)"
            ' (via micro (at 1 1) (size 0.2) (drill 0.1) (layers "F.Cu" "In1.Cu") (net "N")))',
            encoding="utf-8",
        )
        context = FakeContext(board=load_board(board))

        result = ViaRulesCheck(
            min_drill_mm=0.3,
            min_diameter_mm=0.5,
            min_annular_ring_mm=0.05,
            min_microvia_drill_mm=0.1,
            min_microvia_diameter_mm=0.2,
        ).run(context)

        assert result.status == TestStatus.PASS

    def test_geometry_failure_is_not_retried_per_check(self, monkeypatch):
        from kicad_evaltor import pcbnew_bridge
        from kicad_evaltor.core.context import DesignContext
        from kicad_evaltor.pcbnew_bridge import GeometryUnavailable

        calls = []

        def hang(*_args, **_kwargs):
            calls.append(1)
            raise GeometryUnavailable("timed out", timed_out=True)

        monkeypatch.setattr(pcbnew_bridge, "dump_board", hang)
        context = DesignContext(board_path=ESP_BOARD)

        for check in (BoardOutlineCheck(), FootprintOnBoardCheck(), TextOffBoardCheck()):
            assert check.run(context).status == TestStatus.ERROR
        assert calls == [1]
