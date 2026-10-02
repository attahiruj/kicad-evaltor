"""Unit tests for PCB check run() logic, driven by fake kipy objects."""

from pathlib import Path

import pytest

from conftest import (
    BBox,
    FakeBoard,
    FakeContext,
    FakeFootprint,
    FakeNet,
    FakePad,
    FakeTrack,
    FakeZone,
    LengthAttrItem,
    LengthlessItem,
    cli_result,
    drc_report,
    mm,
    mm2,
    violation,
)
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.pcb.drc_check import DRCRunCheck
from kicad_evaltor.checks.pcb.footprint_exists import FootprintExistsCheck
from kicad_evaltor.checks.pcb.footprint_overlap import FootprintOverlapCheck
from kicad_evaltor.checks.pcb.power_pour import PowerPourCheck
from kicad_evaltor.checks.pcb.trace_length import TraceLengthCheck
from kicad_evaltor.checks.pcb.trace_width import TraceWidthCheck
from kicad_evaltor.core.runner import TestRunner


def ctx_with(board=None, cli=None) -> FakeContext:
    return FakeContext(board=board or FakeBoard(), cli_results=cli)


def run(check, ctx):
    return check.run(ctx)


class TestFootprintExistsCheck:
    def test_skips_without_board(self):
        assert run(FootprintExistsCheck(reference="R1"), FakeContext()).status == TestStatus.SKIP

    def test_matches_reference(self):
        board = FakeBoard(footprints=[FakeFootprint("R1"), FakeFootprint("C1")])
        result = run(FootprintExistsCheck(reference="R1"), ctx_with(board))

        assert result.is_pass
        assert result.details["references"] == ["R1"]

    def test_fails_on_unknown_reference(self):
        board = FakeBoard(footprints=[FakeFootprint("R1")])
        result = run(FootprintExistsCheck(reference="R9"), ctx_with(board))

        assert result.is_fail
        assert result.details["criteria"] == ["reference=R9"]

    def test_matches_lib_id(self):
        board = FakeBoard(
            footprints=[
                FakeFootprint("R1", "Resistor_SMD:R_0805"),
                FakeFootprint("C1", "Capacitor_SMD:C_0603"),
            ]
        )
        result = run(
            FootprintExistsCheck(lib_id="Capacitor_SMD:C_0603"),
            ctx_with(board),
        )
        assert result.is_pass
        assert result.details["references"] == ["C1"]

    def test_criteria_are_combined_with_and(self):
        board = FakeBoard(footprints=[FakeFootprint("R1", "Resistor_SMD:R_0805")])
        result = run(
            FootprintExistsCheck(reference="C1", lib_id="Resistor_SMD:R_0805"),
            ctx_with(board),
        )
        assert result.is_fail

    def test_empty_board_fails(self):
        assert run(FootprintExistsCheck(reference="R1"), ctx_with()).is_fail

    def test_lib_id_match_is_exact(self):
        board = FakeBoard(footprints=[FakeFootprint("R1", "Resistor_SMD:R_0805")])
        assert run(FootprintExistsCheck(lib_id="Resistor_SMD:R_*"), ctx_with(board)).is_fail


class TestTraceWidthCheck:
    def test_skips_without_board(self):
        assert (
            run(
                TraceWidthCheck(min_width_mm=0.2, net_name="VCC"),
                FakeContext(),
            ).status
            == TestStatus.SKIP
        )

    def test_passes_when_all_tracks_meet_minimum(self):
        board = FakeBoard(
            tracks=[FakeTrack(net=1, width=mm(0.25)), FakeTrack(net=1, width=mm(0.5))],
            nets=[FakeNet("VCC", 1)],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 2

    def test_fails_on_undersized_track(self):
        board = FakeBoard(
            tracks=[FakeTrack(net=1, width=mm(0.1))],
            nets=[FakeNet("VCC", 1)],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board))

        assert result.is_fail
        assert result.details["violations"][0]["width_mm"] == 0.1
        assert "0.100mm < min 0.2mm" in result.details["violations"][0]["issue"]

    def test_min_is_inclusive_at_the_boundary(self):
        board = FakeBoard(tracks=[FakeTrack(net=1, width=mm(0.2))], nets=[FakeNet("VCC", 1)])
        assert run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board)).is_pass

    def test_fails_on_oversized_track(self):
        board = FakeBoard(tracks=[FakeTrack(net=1, width=mm(2.0))], nets=[FakeNet("VCC", 1)])
        result = run(
            TraceWidthCheck(min_width_mm=0.2, max_width_mm=0.5, net_name="VCC"),
            ctx_with(board),
        )
        assert result.is_fail
        assert "> max" in result.details["violations"][0]["issue"]

    def test_single_track_outside_both_bounds_counts_once_per_rule(self):
        board = FakeBoard(tracks=[FakeTrack(net=1, width=mm(2.0))], nets=[FakeNet("VCC", 1)])
        result = run(
            TraceWidthCheck(min_width_mm=0.2, max_width_mm=0.5, net_name="VCC"),
            ctx_with(board),
        )
        assert len(result.details["violations"]) == 1

    def test_only_selected_net_tracks_are_checked(self):
        board = FakeBoard(
            tracks=[FakeTrack(net=1, width=mm(0.25)), FakeTrack(net=2, width=mm(0.05))],
            nets=[FakeNet("VCC", 1), FakeNet("GND", 2)],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 1

    def test_missing_net_fails_before_scanning_tracks(self):
        board = FakeBoard(tracks=[FakeTrack(net=1, width=mm(0.25))], nets=[FakeNet("GND", 2)])
        result = run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board))

        assert result.is_fail
        assert "not found on board" in result.message
        assert result.details["net_name"] == "VCC"

    def test_selected_net_with_no_tracks_passes(self):
        board = FakeBoard(nets=[FakeNet("VCC", 1)])
        result = run(TraceWidthCheck(min_width_mm=0.2, net_name="VCC"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 0

    def test_reference_selects_nets_from_footprint_pads(self):
        board = FakeBoard(
            footprints=[FakeFootprint("R1", pads=[FakePad("1", net=1), FakePad("2", net=None)])],
            tracks=[FakeTrack(net=1, width=mm(0.25)), FakeTrack(net=2, width=mm(0.05))],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, reference="R1"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 1

    def test_reference_includes_every_net_on_the_footprint(self):
        board = FakeBoard(
            footprints=[FakeFootprint("R1", pads=[FakePad("1", net=1), FakePad("2", net=2)])],
            tracks=[FakeTrack(net=1, width=mm(0.25)), FakeTrack(net=2, width=mm(0.25))],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, reference="R1"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 2

    def test_missing_footprint_fails(self):
        board = FakeBoard(footprints=[FakeFootprint("C1")], tracks=[])
        result = run(TraceWidthCheck(min_width_mm=0.2, reference="R1"), ctx_with(board))

        assert result.is_fail
        assert "Footprint 'R1' not found" in result.message

    def test_footprint_with_only_unconnected_pads_checks_nothing(self):
        board = FakeBoard(
            footprints=[FakeFootprint("R1", pads=[FakePad("1", net=None)])],
            tracks=[FakeTrack(net=1, width=mm(0.05))],
        )
        result = run(TraceWidthCheck(min_width_mm=0.2, reference="R1"), ctx_with(board))

        assert result.is_pass
        assert result.details["tracks_checked"] == 0


class TestTraceLengthCheck:
    def _board(self, pads, items, footprints=None):
        return FakeBoard(
            footprints=footprints or [FakeFootprint("R1", pads=pads)],
            items_by_net={1: items},
        )

    def test_skips_without_board(self):
        assert (
            run(
                TraceLengthCheck(reference="R1", max_length_mm=50),
                FakeContext(),
            ).status
            == TestStatus.SKIP
        )

    def test_passes_within_limits(self):
        board = self._board([FakePad("1", net=1)], [FakeTrack(net=1, width=0, length=mm(10))])
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))

        assert result.is_pass
        assert result.details["length_mm"] == 10.0

    def test_fails_above_maximum(self):
        board = self._board([FakePad("1", net=1)], [FakeTrack(net=1, width=0, length=mm(60))])
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))

        assert result.is_fail
        assert "60.000mm > max 50mm" in result.message
        assert result.details["length_mm"] == 60.0

    def test_fails_below_minimum(self):
        board = self._board([FakePad("1", net=1)], [FakeTrack(net=1, width=0, length=mm(5))])
        result = run(
            TraceLengthCheck(reference="R1", max_length_mm=50, min_length_mm=10),
            ctx_with(board),
        )

        assert result.is_fail
        assert "5.000mm < min 10mm" in result.message

    def test_minimum_is_inclusive_at_the_boundary(self):
        board = self._board([FakePad("1", net=1)], [FakeTrack(net=1, width=0, length=mm(10))])
        assert run(
            TraceLengthCheck(reference="R1", max_length_mm=50, min_length_mm=10),
            ctx_with(board),
        ).is_pass

    def test_sums_length_across_multiple_items(self):
        board = self._board(
            [FakePad("1", net=1)],
            [
                FakeTrack(net=1, width=0, length=mm(10)),
                LengthAttrItem(length=mm(15)),
            ],
        )
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))
        assert result.details["length_mm"] == 25.0

    def test_ignores_items_exposing_no_length(self):
        board = self._board(
            [FakePad("1", net=1)],
            [LengthlessItem(), FakeTrack(net=1, width=0, length=mm(10))],
        )
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))
        assert result.details["length_mm"] == 10.0

    def test_sums_length_across_multiple_nets_on_the_footprint(self):
        board = FakeBoard(
            footprints=[FakeFootprint("R1", pads=[FakePad("1", net=1), FakePad("2", net=2)])],
            items_by_net={
                1: [FakeTrack(net=1, width=0, length=mm(10))],
                2: [FakeTrack(net=2, width=0, length=mm(5))],
            },
        )
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))
        assert result.details["length_mm"] == 15.0

    def test_skips_when_footprint_has_no_connected_nets(self):
        board = self._board([FakePad("1", net=None)], [])
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))

        assert result.status == TestStatus.SKIP
        assert "no connected nets" in result.message

    def test_fails_when_footprint_absent(self):
        result = run(
            TraceLengthCheck(reference="R9", max_length_mm=50),
            ctx_with(FakeBoard(footprints=[FakeFootprint("R1")])),
        )
        assert result.is_fail
        assert "not found on board" in result.message

    def test_zero_length_net_passes(self):
        board = self._board([FakePad("1", net=1)], [])
        result = run(TraceLengthCheck(reference="R1", max_length_mm=50), ctx_with(board))

        assert result.is_pass
        assert result.details["length_mm"] == 0.0


class TestFootprintOverlapCheck:
    def _ctx(self, boxes, **params):
        footprints = [FakeFootprint(ref) for ref in boxes]
        return FakeContext(
            board=FakeBoard(
                footprints=footprints,
                bboxes={ref: box for ref, box in boxes.items()},
            ),
        )

    def test_skips_without_board(self):
        assert run(FootprintOverlapCheck(), FakeContext()).status == TestStatus.SKIP

    def test_single_footprint_never_overlaps(self):
        ctx = self._ctx({"R1": BBox.from_mm(0, 0, 1, 1)})
        result = run(FootprintOverlapCheck(), ctx)

        assert result.is_pass
        assert result.details["footprints_checked"] == 1

    def test_footprints_are_only_compared_in_pairs(self):
        # The inner loop starts at i+1, so a footprint is never compared to itself.
        ctx = self._ctx({"R1": BBox.from_mm(0, 0, 1, 1)})
        assert run(FootprintOverlapCheck(), ctx).details["footprints_checked"] == 1

    def test_exclude_refs_are_removed_before_comparison(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 1, 1),
                "C1": BBox.from_mm(0, 0, 1, 1),
            }
        )
        result = run(FootprintOverlapCheck(exclude_refs=["C1"]), ctx)

        assert result.details["footprints_checked"] == 1

    def test_interior_overlap_is_flagged_when_clearance_required(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 2, 2),
                "C1": BBox.from_mm(1, 1, 3, 3),
            }
        )
        result = run(FootprintOverlapCheck(min_clearance_mm=0.1), ctx)

        assert result.is_fail
        assert result.details["violations"][0]["clearance_mm"] == 0.0

    def test_well_separated_footprints_pass(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 1, 1),
                "C1": BBox.from_mm(50, 50, 51, 51),
            }
        )
        assert run(FootprintOverlapCheck(min_clearance_mm=0.1), ctx).is_pass

    def test_too_close_footprints_are_flagged_with_their_gap(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 1, 1),
                "C1": BBox.from_mm(1.05, 1.05, 2, 2),
            }
        )
        result = run(FootprintOverlapCheck(min_clearance_mm=0.1), ctx)

        assert result.is_fail
        assert result.details["violations"][0]["clearance_mm"] == 0.05

    def test_clearance_uses_the_smaller_axis_gap(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 1, 1),
                "C1": BBox.from_mm(1.05, 5, 2, 6),
            }
        )
        result = run(FootprintOverlapCheck(min_clearance_mm=0.1), ctx)
        assert result.details["violations"][0]["clearance_mm"] == 0.05

    def test_interior_overlap_is_not_flagged_at_zero_clearance(self):
        # Overlap is clamped to a 0.0 gap, so a min_clearance_mm of 0.0 treats an
        # interior overlap the same as exact abutment and reports nothing.
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 2, 2),
                "C1": BBox.from_mm(1, 1, 3, 3),
            }
        )
        assert run(FootprintOverlapCheck(), ctx).is_pass

    def test_violation_names_both_footprints(self):
        ctx = self._ctx(
            {
                "R1": BBox.from_mm(0, 0, 1, 1),
                "C1": BBox.from_mm(50, 0, 51, 1),
            }
        )
        violation_detail = run(
            FootprintOverlapCheck(min_clearance_mm=0.1),
            ctx,
        ).details["violations"][0]

        assert violation_detail["footprint_a"] == "R1"
        assert violation_detail["footprint_b"] == "C1"
        assert violation_detail["required_mm"] == 0.1

    def test_empty_board_passes(self):
        result = run(FootprintOverlapCheck(), FakeContext(board=FakeBoard()))
        assert result.is_pass
        assert result.details["footprints_checked"] == 0


class TestPowerPourCheck:
    def test_skips_without_board(self):
        assert run(PowerPourCheck(net_name="VCC"), FakeContext()).status == TestStatus.SKIP

    def test_passes_when_zone_exists(self):
        board = FakeBoard(zones=[FakeZone("VCC")])
        result = run(PowerPourCheck(net_name="VCC"), ctx_with(board))

        assert result.is_pass
        assert result.details["zone_count"] == 1

    def test_counts_multiple_matching_zones(self):
        board = FakeBoard(zones=[FakeZone("VCC"), FakeZone("VCC"), FakeZone("GND")])
        result = run(PowerPourCheck(net_name="VCC"), ctx_with(board))

        assert result.details["zone_count"] == 2

    def test_fails_when_no_zone_for_net(self):
        board = FakeBoard(zones=[FakeZone("GND")])
        result = run(PowerPourCheck(net_name="VCC"), ctx_with(board))

        assert result.is_fail
        assert "No copper pour" in result.message
        assert result.details["net_name"] == "VCC"

    def test_fails_when_no_zones_at_all(self):
        assert run(PowerPourCheck(net_name="VCC"), ctx_with()).is_fail

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "power_pour.py converts nm2 to mm2 with to_mm(area_nm2) ** 2, which "
            "divides by 1e6 and then squares, instead of dividing by 1e12. A "
            "250mm2 pour is reported as 6.25e16mm2, so min_area_mm2 can never be "
            "satisfied. Fix: max_area_nm2 / 1e12."
        ),
    )
    def test_passes_area_requirement_when_satisfied(self):
        board = FakeBoard(zones=[FakeZone("VCC", areas=[mm2(250)])])
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=100), ctx_with(board))

        assert result.is_pass
        assert result.details["area_mm2"] == 250.0

    @pytest.mark.xfail(strict=True, reason="area unit conversion bug, see above")
    def test_reports_area_in_square_millimetres(self):
        board = FakeBoard(zones=[FakeZone("VCC", areas=[mm2(250)])])
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=1), ctx_with(board))

        assert result.details["area_mm2"] == 250.0

    def test_area_requirement_is_met_for_a_large_pour(self):
        # Passes today, but only because the unit bug inflates the computed
        # area; see the xfail'd tests above for the real expectation.
        board = FakeBoard(zones=[FakeZone("VCC", areas=[mm2(100)])])
        assert run(PowerPourCheck(net_name="VCC", min_area_mm2=100), ctx_with(board)).is_pass

    @pytest.mark.xfail(strict=True, reason="area unit conversion bug, see above")
    def test_fails_when_area_below_requirement(self):
        board = FakeBoard(zones=[FakeZone("VCC", areas=[mm2(50)])])
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=100), ctx_with(board))

        assert result.is_fail
        assert "area < 100" in result.message

    @pytest.mark.xfail(strict=True, reason="area unit conversion bug, see above")
    def test_uses_largest_polygon_across_zones(self):
        board = FakeBoard(
            zones=[
                FakeZone("VCC", areas=[mm2(10)]),
                FakeZone("VCC", areas=[mm2(500)]),
            ]
        )
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=100), ctx_with(board))
        assert result.details["area_mm2"] == 500.0

    def test_zone_with_no_filled_polygons_fails_area_requirement(self):
        board = FakeBoard(zones=[FakeZone("VCC", areas=[])])
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=1), ctx_with(board))
        assert result.is_fail

    def test_zone_raising_on_fill_is_skipped_and_area_fails(self):
        board = FakeBoard(zones=[FakeZone("VCC", raises=True)])
        result = run(PowerPourCheck(net_name="VCC", min_area_mm2=1), ctx_with(board))

        assert result.is_fail
        assert "area < 1" in result.message


class TestDRCRunCheck:
    def _ctx(self, cli=None, schematic=None):
        from conftest import FakeSchematic

        return FakeContext(
            schematic=schematic if schematic is not None else FakeSchematic(),
            board=FakeBoard(),
            cli_results=cli,
        )

    def test_skips_without_board(self):
        assert run(DRCRunCheck(), FakeContext()).status == TestStatus.SKIP

    def test_passes_with_no_violations(self):
        result = run(DRCRunCheck(), self._ctx(drc_report()))
        assert result.is_pass
        assert result.details["total_violations"] == 0

    def test_fails_on_error_violations(self):
        result = run(DRCRunCheck(), self._ctx(drc_report(violation("error", "short circuit"))))

        assert result.is_fail
        assert result.details["filtered_violations"] == 1
        assert result.details["violations"][0]["type"] == "unconnected"

    def test_severity_all_counts_warnings(self):
        cli = drc_report(violation("error", "e"), violation("warning", "w"))
        result = run(DRCRunCheck(severity="all"), self._ctx(cli))
        assert result.details["filtered_violations"] == 2

    def test_command_failure_skips_by_default(self):
        result = run(DRCRunCheck(), self._ctx(cli_result(returncode=1, stderr="boom")))

        assert result.status == TestStatus.SKIP
        assert "boom" in result.message

    def test_command_failure_errors_when_strict(self):
        result = run(
            DRCRunCheck(strict=True),
            self._ctx(cli_result(returncode=1, stderr="boom")),
        )
        assert result.status == TestStatus.ERROR

    def test_includes_schematic_parity_by_default(self):
        ctx = self._ctx(drc_report())
        run(DRCRunCheck(), ctx)

        args = ctx.cli_calls[0]
        assert args[:2] == ["pcb", "drc"]
        assert args[-1] == str(ctx.board_path)
        assert "--schematic-parity" in args
        assert args[args.index("--schematic-parity") + 1] == str(ctx.schematic_path)
        # DRC violations arrive in a -o file, not on stdout.
        assert Path(args[args.index("-o") + 1]).name == "report.json"

    def test_omits_schematic_parity_when_disabled(self):
        ctx = self._ctx(drc_report())
        run(DRCRunCheck(schematic_parity=False), ctx)

        assert "--schematic-parity" not in ctx.cli_calls[0]

    def test_omits_schematic_parity_without_schematic(self):
        ctx = FakeContext(board=FakeBoard(), cli_results=drc_report())
        run(DRCRunCheck(), ctx)

        assert "--schematic-parity" not in ctx.cli_calls[0]

    def test_explicit_board_path_overrides_context(self):
        ctx = self._ctx(drc_report())
        run(DRCRunCheck(board_path="other.kicad_pcb"), ctx)

        assert ctx.cli_calls[0][-1] == "other.kicad_pcb"


class TestPCBChecksThroughRunner:
    def test_runner_reports_mixed_outcomes(self):
        board = FakeBoard(
            footprints=[FakeFootprint("R1")],
            tracks=[FakeTrack(net=1, width=mm(0.05))],
            nets=[FakeNet("VCC", 1)],
        )
        report = TestRunner(
            [
                FootprintExistsCheck(reference="R1"),
                FootprintExistsCheck(reference="R9"),
                TraceWidthCheck(min_width_mm=0.2, net_name="VCC"),
            ]
        ).run(ctx_with(board))

        assert report.passed_count == 1
        assert report.failed_count == 2

    def test_board_checks_skip_cleanly_without_a_board(self):
        checks = [
            FootprintExistsCheck(reference="R1"),
            FootprintOverlapCheck(),
            TraceWidthCheck(min_width_mm=0.2, net_name="VCC"),
            TraceLengthCheck(reference="R1", max_length_mm=50),
            PowerPourCheck(net_name="VCC"),
            DRCRunCheck(),
        ]
        report = TestRunner(checks).run(FakeContext())

        assert report.skipped_count == len(checks)
        assert report.error_count == 0
