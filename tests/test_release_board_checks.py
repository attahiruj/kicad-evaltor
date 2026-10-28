"""Board checks added or fixed for 0.6.1, on a real board and copies of it.

The board is the real KiCad project from the imu-data-logger task under
``tests/fixtures/boards/imu``. ``geometry.json`` beside it is pcbnew's dump of
that board, recorded with :func:`kicad_evaltor.pcbnew_bridge.dump_board_json`;
:class:`TestRecordingsMatchPcbnew` re-dumps it under KiCad.

Every failing case is a copy of the real board changed with
:mod:`kicad_evaltor.edit`: a library nickname stripped, a footprint renamed,
pads mirrored, corners squared. Nothing here is drawn from scratch.
"""

from __future__ import annotations

import itertools
import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import ESP_BOARD, FakeContext, FakeSchematic, recorded_geometry
from kicad_evaltor.board_file import load_board
from kicad_evaltor.board_queries import board_corners, edge_cut_arcs
from kicad_evaltor.checks import (
    BoardOutlineCheck,
    DecouplingCoverageCheck,
    FootprintMatchesSchematicCheck,
    MountingHolesCheck,
)
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.connectivity import series_parts
from kicad_evaltor.edit import board_footprint, load_document, save_document
from kicad_evaltor.footprint_library import (
    FootprintLibraries,
    chip_copper,
    chip_size_from_copper,
    compare_copper,
    compare_to_library,
    copper_pads,
    footprint_node,
    read_fp_lib_table,
)
from kicad_evaltor.netlist import parse_netlist
from kicad_evaltor.pcbnew_bridge import geometry_from_dict
from kicad_evaltor.schematic_file import load_schematic
from kicad_evaltor.sexpr import Quoted, SExpr, child, children, value_of

IMU = Path(__file__).resolve().parent / "fixtures" / "boards" / "imu"
BOARD = IMU / "imu.kicad_pcb"


def recorded():
    return geometry_from_dict(json.loads((IMU / "geometry.json").read_text()))


def variant(root: Path, change: Callable[[list[SExpr]], object]) -> Path:
    """A copy of the real project whose board ``change`` edits in place."""
    root.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro"):
        shutil.copy(IMU / f"imu{suffix}", root / f"imu{suffix}")
    board = root / "imu.kicad_pcb"
    document = load_document(board)
    change(document)
    save_document(document, board)
    return board


def rename_footprints(match: Callable[[str], bool], rename: Callable[[str], str]):
    def change(document: list[SExpr]) -> None:
        for node in children(document, "footprint"):
            if match(value_of(node)):
                node[1] = Quoted(rename(value_of(node)))

    return change


def bare(lib_id: str) -> str:
    return lib_id.split(":", 1)[-1]


def file_context(board: Path, geometry=None, schematic=None) -> FakeContext:
    return FakeContext(
        board=load_board(board), geometry=geometry, schematic=schematic, board_path=str(board)
    )


# ------------------------------------------------------------ mounting holes


class TestMountingHoles:
    def test_holes_saved_without_nickname_are_found(self, tmp_path):
        # The v6 failure: a script wrote H1-H4 as "MountingHole_2.7mm_M2.5";
        # here the same is done to this board's M3 holes.
        board = variant(
            tmp_path,
            rename_footprints(lambda name: name.startswith("MountingHole:"), bare),
        )
        context = file_context(board)
        assert {fp.lib_id for fp in context.board.footprints if fp.reference.startswith("H")} == {
            "MountingHole_3.2mm_M3"
        }

        result = MountingHolesCheck(min_count=4, diameter_mm=3.2).run(context)

        assert result.status == TestStatus.PASS, result.message
        assert sorted(h["reference"] for h in result.details["holes"]) == ["H1", "H2", "H3", "H4"]
        # The 0.6.0 default needed the nickname.
        old = MountingHolesCheck(min_count=4, match="MountingHole:*").run(context)
        assert old.status == TestStatus.FAIL

    def test_default_still_matches_named_and_plated_holes(self):
        # The demo board's holes are the plated "..._Pad_TopBottom" kind.
        context = FakeContext(board=load_board(ESP_BOARD), board_path=str(ESP_BOARD))
        result = MountingHolesCheck(min_count=4, max_count=4, diameter_mm=3.2).run(context)
        assert result.status == TestStatus.PASS, result.message
        check = MountingHolesCheck()
        holes = [fp for fp in context.board.footprints if check._is_hole(fp)]
        assert len(holes) == 4

    def test_pad_via_hole_counts_by_its_largest_drill(self):
        libraries = FootprintLibraries()
        if libraries.library_path("MountingHole") is None:
            pytest.skip("KiCad's stock footprint libraries are not installed")
        pads = copper_pads(libraries.load("MountingHole_3.2mm_M3_Pad_Via"))
        assert max(p.drill or 0 for p in pads) == pytest.approx(3.2)
        assert len(pads) > 1  # one hole plus its via ring
        assert MountingHolesCheck.Params().match == "*MountingHole*"

    def test_an_unnamed_footprint_counts_by_drill(self, tmp_path):
        board = variant(
            tmp_path,
            rename_footprints(
                lambda name: name.startswith("MountingHole:"), lambda _: "Logger:Hole_M3"
            ),
        )
        context = file_context(board)

        assert MountingHolesCheck(min_count=1).run(context).status == TestStatus.FAIL
        by_drill = MountingHolesCheck(min_count=4, max_count=4, match=None, match_drill_min_mm=2.0)
        result = by_drill.run(context)

        # Connector and switch holes (0.75 mm at most) stay out.
        assert result.status == TestStatus.PASS, result.message
        assert {h["footprint"] for h in result.details["holes"]} == {"Logger:Hole_M3"}

    def test_max_edge_distance_from_the_drill_edge(self):
        context = file_context(BOARD, geometry=recorded())

        result = MountingHolesCheck(min_count=4, max_edge_distance_mm=2.0).run(context)

        # 3.2 mm holes on a 50 mm square with R5 corners. H1/H2 sit 4 mm from a
        # straight edge (4 - 1.6 = 2.4); H3/H4 sit 1.414 mm off the corner
        # centre (5 - 1.414 - 1.6 = 1.986).
        gaps = {h["reference"]: h["edge_distance_mm"] for h in result.details["holes"]}
        assert gaps["H1"] == pytest.approx(2.4, abs=0.01)
        assert gaps["H2"] == pytest.approx(2.4, abs=0.01)
        assert gaps["H3"] == pytest.approx(5 - math.sqrt(2) - 1.6, abs=0.01)
        assert gaps["H4"] == pytest.approx(5 - math.sqrt(2) - 1.6, abs=0.01)
        assert result.status == TestStatus.FAIL
        assert "H1" in result.message
        assert "H4" not in result.message
        assert (
            MountingHolesCheck(min_count=4, max_edge_distance_mm=2.5).run(context).status
            == TestStatus.PASS
        )

    def test_rejects_no_way_to_match(self):
        with pytest.raises(ValueError, match="match"):
            MountingHolesCheck(match=None)


# ------------------------------------------------------------ corner radius


class TestCornerRadius:
    def test_arc_radii_come_from_the_file(self):
        # The demo board: three R5 corners and one R5.2.
        corners = board_corners(recorded_geometry().outline, edge_cut_arcs(ESP_BOARD))
        assert sorted(c.radius_mm for c in corners) == [5.0, 5.0, 5.0, 5.2]
        assert {c.source for c in corners} == {"arc"}
        assert all(c.convex and c.turn_deg == pytest.approx(90) for c in corners)

    def test_fit_without_the_file_agrees_with_the_arcs(self):
        corners = board_corners(recorded_geometry().outline)
        assert {c.source for c in corners} == {"fit"}
        assert sorted(c.radius_mm for c in corners) == pytest.approx([5.0, 5.0, 5.0, 5.2], abs=0.01)

    def test_check_reports_and_judges_each_corner(self):
        context = FakeContext(
            board=load_board(ESP_BOARD), geometry=recorded_geometry(), board_path=str(ESP_BOARD)
        )

        exact = BoardOutlineCheck(corner_radius_mm=5).run(context)
        loose = BoardOutlineCheck(corner_radius_mm=5, corner_radius_tolerance_mm=0.25).run(context)

        assert exact.status == TestStatus.FAIL
        assert "R5.2 at (148.277, 98.277)" in exact.message
        assert loose.status == TestStatus.PASS
        assert len(loose.details["corners"]) == 4

    def test_imu_board_corners(self):
        context = file_context(BOARD, geometry=recorded())

        result = BoardOutlineCheck(width_mm=50, height_mm=50, corner_radius_mm=5).run(context)

        assert result.status == TestStatus.PASS, result.message
        assert [c["radius_mm"] for c in result.details["corners"]] == [5.0] * 4
        assert BoardOutlineCheck(corner_radius_mm=0).run(context).status == TestStatus.FAIL

    def test_rejects_unknown_corner_selection(self):
        with pytest.raises(ValueError, match="corners"):
            BoardOutlineCheck(corners="concave")


def square_corners(document: list[SExpr]) -> None:
    """Replace each rounded Edge.Cuts corner with the two edges it cut off."""
    _replace_arcs(document, lambda arc: [arc.start, _corner_point(arc), arc.end])


def segment_corners(document: list[SExpr]) -> None:
    """Replace each Edge.Cuts arc with 8 straight segments along it."""

    def points(arc):
        circle_start = math.atan2(arc.start[1] - arc.center[1], arc.start[0] - arc.center[0])
        circle_end = math.atan2(arc.end[1] - arc.center[1], arc.end[0] - arc.center[0])
        sweep = (circle_end - circle_start + math.pi) % (2 * math.pi) - math.pi
        return [
            (
                arc.center[0] + arc.radius * math.cos(circle_start + sweep * k / 8),
                arc.center[1] + arc.radius * math.sin(circle_start + sweep * k / 8),
            )
            for k in range(9)
        ]

    _replace_arcs(document, points)


def _corner_point(arc):
    return (
        arc.start[0] + arc.end[0] - arc.center[0],
        arc.start[1] + arc.end[1] - arc.center[1],
    )


def _replace_arcs(document: list[SExpr], points_of) -> None:
    from kicad_evaltor.board_queries import EdgeArc
    from kicad_evaltor.board_file import arc_circle

    for node in list(children(document, "gr_arc")):
        if value_of(child(node, "layer")) != "Edge.Cuts":
            continue
        start, mid, end = (
            tuple(float(v) for v in child(node, key)[1:3]) for key in ("start", "mid", "end")
        )
        cx, cy, radius, _, sweep = arc_circle(start, mid, end)
        arc = EdgeArc((cx, cy), radius, abs(math.degrees(sweep)), start, mid, end)
        path = points_of(arc)
        at = document.index(node)
        document.remove(node)
        for k, (a, b) in enumerate(itertools.pairwise(path)):
            document.insert(
                at + k,
                [
                    "gr_line",
                    ["start", f"{a[0]:.6f}", f"{a[1]:.6f}"],
                    ["end", f"{b[0]:.6f}", f"{b[1]:.6f}"],
                    ["stroke", ["width", "0.1"], ["type", "default"]],
                    ["layer", Quoted("Edge.Cuts")],
                ],
            )


# ------------------------------------------------------------- decoupling

# Supply pins of each powered part, read from the boards' pad nets.
IMU_PINS = {
    "U2": ["13", "2", "3", "5", "10", "11"],  # BQ24074: VBUS, BAT, OUT
    "U3": ["5", "1", "10"],  # TPS63001: VIN, VOUT
    "U4": ["3"],  # ESP32-C6-MINI-1: 3V3
    "U5": ["5", "8", "12"],  # LSM6DSL: VDDIO, VDD
}


def imu_netlist() -> FakeSchematic:
    """The schematic's netlist as kicad-cli exported it (``netlist_imu.xml``)."""
    text = (IMU / "netlist_imu.xml").read_text("utf-8")
    return FakeSchematic(netlist=parse_netlist(text, "kicadxml"))


class TestDecouplingCoverage:
    def test_imu_board_is_covered_both_ways(self):
        context = file_context(BOARD, geometry=recorded())

        result = DecouplingCoverageCheck(
            pins=IMU_PINS,
            return_nets=["GND"],
            max_distance_mm=10,
        ).run(context)

        assert result.status == TestStatus.PASS, result.message
        assert result.details["uncovered_pins"] == []
        assert result.details["stray_caps"] == []
        nearest = {
            (e["reference"], e["pin"]): e["nearest_capacitor"] for e in result.details["pins"]
        }
        assert nearest[("U2", "13")] == "C1"
        assert nearest[("U5", "8")] == "C13"

    def test_tighter_limit_finds_uncovered_pins_and_stray_caps(self):
        context = file_context(BOARD, geometry=recorded())

        result = DecouplingCoverageCheck(
            pins=IMU_PINS,
            return_nets=["GND"],
            max_distance_mm=3,
        ).run(context)

        assert result.status == TestStatus.FAIL
        uncovered = {(e["reference"], e["pin"]) for e in result.details["uncovered_pins"]}
        assert uncovered == {("U2", "5")}  # C2 is 3.95 mm from BQ24074 OUT pin 5
        stray = {e["reference"] for e in result.details["stray_caps"]}
        assert {"C14", "C15"} <= stray  # +3V3 caps 8-9 mm from the IMU

    def test_a_single_stray_cap_with_every_pin_covered(self):
        # At 8.5 mm every pin is covered (the farthest, U2 pin 5, is 3.95 mm
        # from C2) and only C14 (+3V3), 8.90 mm from the nearest covered +3V3
        # pin, is stray; C15 is 8.30 mm away.
        context = file_context(BOARD, geometry=recorded())

        result = DecouplingCoverageCheck(
            pins=IMU_PINS,
            return_nets=["GND"],
            max_distance_mm=8.5,
        ).run(context)

        assert result.status == TestStatus.FAIL
        assert result.details["uncovered_pins"] == []
        [stray] = result.details["stray_caps"]
        assert stray["reference"] == "C14"
        assert stray["supply_nets"] == ["+3V3"]
        assert stray["distance_mm"] == pytest.approx(8.90, abs=0.01)

    def test_caps_on_a_supply_no_covered_pin_uses_are_not_judged(self):
        # Without the charger's battery pins no covered pin is on +BATT, so C3
        # (+BATT to GND) is not taken for a decoupling cap gone astray, as an
        # RC delay cap on an EN net is not. This board has no such RC cap; C3
        # is its one capacitor on a net outside the covered supplies.
        context = file_context(BOARD, geometry=recorded())
        pins = {**IMU_PINS, "U2": ["13", "5", "10", "11"]}

        result = DecouplingCoverageCheck(pins=pins, return_nets=["GND"], max_distance_mm=10).run(
            context
        )

        assert result.status == TestStatus.PASS, result.message
        assert result.details["supply_nets"] == ["+3V3", "/VSYS", "VBUS"]
        assert result.details["capacitors_checked"] == 8  # every cap but C3

    def test_named_supply_nets_are_judged_even_without_a_covered_pin(self):
        context = file_context(BOARD, geometry=recorded())
        pins = {**IMU_PINS, "U2": ["13", "5", "10", "11"]}

        result = DecouplingCoverageCheck(
            pins=pins,
            return_nets=["GND"],
            max_distance_mm=10,
            supply_nets=["+BATT", "VBUS", "/VSYS", "+3V3"],
        ).run(context)

        assert result.status == TestStatus.FAIL
        [stray] = result.details["stray_caps"]
        assert stray["reference"] == "C3"
        assert stray["supply_nets"] == ["+BATT"]
        assert stray["nearest_part"] is None
        assert result.details["capacitors_checked"] == 9

    def test_a_cap_may_sit_next_to_a_served_pin(self):
        # C1 (VBUS) sits 1.48 mm from the charger's VBUS pin U2.13. With that
        # pin left out of pins, nothing covered is on VBUS and C1 is stray.
        # Serving U1.5, the USBLC6's VBUS pin 8.99 mm away, places it without
        # requiring a cap for U1.5.
        context = file_context(BOARD, geometry=recorded())
        pins = {**IMU_PINS, "U2": ["2", "3", "5", "10", "11"]}
        supplies = ["+BATT", "VBUS", "/VSYS", "+3V3"]

        alone = DecouplingCoverageCheck(
            pins=pins, return_nets=["GND"], max_distance_mm=10, supply_nets=supplies
        ).run(context)
        served = DecouplingCoverageCheck(
            pins=pins,
            return_nets=["GND"],
            max_distance_mm=10,
            supply_nets=supplies,
            serves={"U1": ["5"]},
        ).run(context)
        too_far = DecouplingCoverageCheck(
            pins=pins,
            return_nets=["GND"],
            max_distance_mm=8.5,
            supply_nets=supplies,
            serves={"U1": ["5"]},
        ).run(context)

        assert alone.status == TestStatus.FAIL
        assert [e["reference"] for e in alone.details["stray_caps"]] == ["C1"]
        assert served.status == TestStatus.PASS, served.message
        assert "U1" not in {e["reference"] for e in served.details["pins"]}
        # C14 (+3V3), 8.90 mm from its nearest pin, is stray at 8.5 mm too.
        stray = {e["reference"]: e for e in too_far.details["stray_caps"]}
        assert sorted(stray) == ["C1", "C14"]
        assert stray["C1"]["nearest_pin"] == "U1.5"
        assert stray["C1"]["distance_mm"] == pytest.approx(8.99, abs=0.01)
        assert too_far.details["uncovered_pins"] == []

    def test_bridge_merges_nets_joined_through_a_part(self):
        # This board has no 0 ohm link or ferrite between supplies; R11, the
        # 100 ohm resistor from /VSYS to the TPS63001's EN pins (U3.6, U3.8),
        # stands in for one, bounded at 100 ohm. Joined, the /VSYS caps C5
        # and C2 count for the EN pins as caps on /3V3 count for a regulator
        # output on /REG_3V3 across a 0 ohm link.
        context = file_context(BOARD, geometry=recorded(), schematic=imu_netlist())
        params = {"pins": {"U3": ["6", "8"]}, "return_nets": ["GND"], "max_distance_mm": 10}

        apart = DecouplingCoverageCheck(**params).run(context)
        joined = DecouplingCoverageCheck(**params, bridge={"R": 100}).run(context)
        as_callable = DecouplingCoverageCheck(**params, bridge=series_parts("R", max_ohms=100)).run(
            context
        )
        zero_ohm_only = DecouplingCoverageCheck(**params, bridge={"R": 0, "FB": None}).run(context)

        assert {(e["reference"], e["pin"]) for e in apart.details["uncovered_pins"]} == {
            ("U3", "6"),
            ("U3", "8"),
        }
        assert apart.details["capacitors_checked"] == 0
        assert joined.details["uncovered_pins"] == []
        nearest = {
            e["pin"]: (e["nearest_capacitor"], e["distance_mm"]) for e in joined.details["pins"]
        }
        assert nearest["6"] == ("C5", pytest.approx(3.14, abs=0.01))
        assert nearest["8"] == ("C5", pytest.approx(3.83, abs=0.01))
        assert joined.details["supply_nets"] == ["/VSYS"]
        assert joined.details["merged_nets"] == {"/VSYS": ["/VSYS", "Net-(U3-EN)"]}
        # C2 serves the charger's /VSYS pins, 11.66 mm from the nearest EN pin.
        [stray] = joined.details["stray_caps"]
        assert (stray["reference"], stray["nearest_pin"]) == ("C2", "U3.6")
        assert stray["distance_mm"] == pytest.approx(11.66, abs=0.01)
        assert as_callable.details == joined.details
        # Bounded at 0 ohm, R11 joins nothing.
        assert zero_ohm_only.details["uncovered_pins"] == apart.details["uncovered_pins"]

    def test_bridge_without_a_netlist_is_an_error(self):
        context = file_context(BOARD, geometry=recorded())
        result = DecouplingCoverageCheck(
            pins=IMU_PINS, return_nets=["GND"], max_distance_mm=10, bridge={"R": 0}
        ).run(context)
        assert result.status == TestStatus.ERROR
        assert "netlist" in result.message

    def test_missing_part_fails(self):
        context = file_context(BOARD, geometry=recorded())
        result = DecouplingCoverageCheck(
            pins={"U9": ["1"]}, return_nets=["GND"], max_distance_mm=5
        ).run(context)
        assert result.status == TestStatus.FAIL
        assert "U9" in result.message

    def test_params_are_validated(self):
        with pytest.raises(ValueError, match="return_nets"):
            DecouplingCoverageCheck(pins={"U1": ["1"]}, return_nets=[], max_distance_mm=5)
        with pytest.raises(ValueError, match="measure_to"):
            DecouplingCoverageCheck(
                pins={"U1": ["1"]}, return_nets=["GND"], max_distance_mm=5, measure_to="net"
            )
        with pytest.raises(ValueError, match="bridge prefix"):
            DecouplingCoverageCheck(
                pins={"U1": ["1"]}, return_nets=["GND"], max_distance_mm=5, bridge={"R1": 0}
            )
        with pytest.raises(ValueError, match="max_ohms"):
            DecouplingCoverageCheck(
                pins={"U1": ["1"]}, return_nets=["GND"], max_distance_mm=5, bridge={"R": -1}
            )
        with pytest.raises(ValueError, match="serves"):
            DecouplingCoverageCheck(
                pins={"U1": ["1"]}, return_nets=["GND"], max_distance_mm=5, serves={"U2": []}
            )


# ------------------------------------------------------- footprint library


@pytest.fixture(scope="module")
def libraries() -> FootprintLibraries:
    found = FootprintLibraries()
    if found.library_path("Resistor_SMD") is None:
        pytest.skip("KiCad's stock footprint libraries are not installed")
    return found


class TestFootprintLibrary:
    def test_bare_name_resolves_to_its_stock_library(self, libraries):
        [(nickname, path)] = libraries.find("R_0603_1608Metric")
        assert nickname == "Resistor_SMD"
        assert path.name == "R_0603_1608Metric.kicad_mod"
        assert libraries.find("Capacitor_SMD:C_0402_1005Metric")[0][0] == "Capacitor_SMD"
        assert libraries.find("No_Such_Footprint") == []

    def test_project_table_takes_a_nickname_first(self, libraries, tmp_path):
        stock = libraries.library_path("Resistor_SMD")
        (tmp_path / "fp-lib-table").write_text(
            '(fp_lib_table (version 7)\n  (lib (name "Logger") (type "KiCad") '
            f'(uri "{stock}") (options "") (descr ""))\n)\n'
        )
        local = FootprintLibraries(libraries.dirs, project_dir=tmp_path)
        assert local.find("R_0603_1608Metric")[0][0] == "Logger"
        assert read_fp_lib_table(tmp_path / "fp-lib-table") == {"Logger": stock}

    def test_real_board_matches_stock_copper_by_bare_name(self, libraries):
        board = load_board(BOARD)
        for fp in board.footprints:
            result = compare_to_library(
                board, fp.reference, libraries=libraries, name=bare(fp.lib_id)
            )
            assert result.matches, (fp.reference, result.differences)
            assert result.library_footprint.endswith(bare(fp.lib_id))

    def test_a_different_footprint_differs_pad_by_pad(self, libraries):
        board = BOARD  # R1 is an 0402
        result = compare_to_library(board, "R1", libraries=libraries, name="R_0603_1608Metric")
        assert not result.matches
        assert {d.issue for d in result.differences} == {"position", "size"}
        assert {d.number for d in result.differences} == {"1", "2"}

    def test_a_mirrored_part_matches_only_when_allowed(self, libraries, tmp_path):
        def mirror_u1(document: list[SExpr]) -> None:
            for pad in children(board_footprint(document, "U1"), "pad"):
                at = child(pad, "at")
                at[1] = str(-float(at[1]))

        board = variant(tmp_path, mirror_u1)  # U1 is a SOT-666

        strict = compare_to_library(board, "U1", libraries=libraries)
        flipped = compare_to_library(board, "U1", libraries=libraries, allow_flipped=True)

        assert {d.issue for d in strict.differences} == {"position"}
        assert flipped.matches
        assert flipped.mirrored == "x"

    @pytest.mark.integration
    def test_footprints_flipped_by_pcbnew_match_unmirrored(self, libraries, kicad_python, tmp_path):
        # pcbnew stores a footprint flipped to the back with its pads mirrored
        # top to bottom in its own frame; that is placement, not a wrong part.
        import subprocess

        board = variant(tmp_path / "src", lambda _doc: None)
        flipped = tmp_path / "flipped.kicad_pcb"
        script = (
            "import sys, pcbnew\n"
            "b = pcbnew.LoadBoard(sys.argv[1])\n"
            "for ref, way in (('U1', pcbnew.FLIP_DIRECTION_TOP_BOTTOM),\n"
            "                 ('U3', pcbnew.FLIP_DIRECTION_LEFT_RIGHT),\n"
            "                 ('C10', pcbnew.FLIP_DIRECTION_TOP_BOTTOM)):\n"
            "    fp = b.FindFootprintByReference(ref)\n"
            "    fp.Flip(fp.GetPosition(), way)\n"
            "b.Save(sys.argv[2])\n"
        )
        subprocess.run(
            [str(kicad_python), "-c", script, str(board), str(flipped)], check=True, timeout=120
        )
        sides = {fp.reference: fp.side for fp in load_board(flipped).footprints}
        assert (sides["U1"], sides["U3"], sides["C10"]) == ("back", "back", "back")

        for ref in ("U1", "U3", "C10"):
            result = compare_to_library(flipped, ref, libraries=libraries)
            assert result.matches, (ref, result.differences)
            assert result.mirrored == ""
        node = footprint_node(flipped, "C10")
        assert compare_copper(node, libraries.load("C_0402_1005Metric")).matches

    def test_chip_size_from_copper(self, libraries):
        r1 = chip_size_from_copper(footprint_node(BOARD, "R1"), libraries=libraries)
        assert r1.size == "0402"
        c8 = chip_size_from_copper(footprint_node(BOARD, "C8"), libraries=libraries)
        assert (c8.size, c8.metric, c8.error_mm) == ("0603", "1608", 0.0)
        assert chip_size_from_copper(footprint_node(BOARD, "U1"), libraries=libraries) is None

    @pytest.mark.parametrize("name", ["L_1008_2520Metric", "L_1210_3225Metric"])
    def test_chip_size_covers_inductor_only_sizes(self, libraries, name):
        # 1008 and 1210 inductors were seen on real boards; only Inductor_SMD
        # has a 1008, so before inductors were scanned it sized as None.
        found = chip_size_from_copper(libraries.load(f"Inductor_SMD:{name}"), libraries=libraries)
        assert found is not None
        assert (found.size, found.metric) == (name[2:6], name[7:11])
        assert found.error_mm == 0.0

    def test_chip_size_ignores_the_name(self, libraries, tmp_path):
        # An 0603 capacitor saved under a custom name is still an 0603.
        board = variant(
            tmp_path,
            rename_footprints(lambda n: n.endswith("C_0603_1608Metric"), lambda _: "Logger:C"),
        )
        capacitor = next(
            fp.reference for fp in load_board(board).footprints if fp.lib_id == "Logger:C"
        )
        found = chip_size_from_copper(footprint_node(board, capacitor), libraries=libraries)
        assert found.size == "0603"
        assert found.footprint == "Capacitor_SMD:C_0603_1608Metric"

    def test_chip_copper_measures_along_the_pads(self, libraries):
        # R_0603_1608Metric: pads 0.8 x 0.95 at +/-0.825.
        copper = chip_copper(libraries.load("R_0603_1608Metric"))
        assert copper.length_mm == pytest.approx(2.45)
        assert copper.gap_mm == pytest.approx(0.85)
        assert copper.width_mm == pytest.approx(0.95)
        # Rotation does not change it: C10 sits at 90 degrees on the board.
        rotated = chip_copper(footprint_node(BOARD, "C10"))
        assert rotated == chip_copper(libraries.load("C_0402_1005Metric"))


# ------------------------------------------------- footprint vs schematic


class TestFootprintMatchesSchematic:
    def _context(self, board: Path) -> FakeContext:
        schematic = load_schematic(board.with_suffix(".kicad_sch"))
        return file_context(board, schematic=schematic)

    def test_nickname_only_differences_are_classified(self, tmp_path):
        board = variant(
            tmp_path,
            rename_footprints(lambda n: n.startswith(("Resistor_SMD:", "Capacitor_SMD:")), bare),
        )
        context = self._context(board)

        strict = FootprintMatchesSchematicCheck(check_pins=False).run(context)
        relaxed = FootprintMatchesSchematicCheck(
            check_pins=False, ignore_library_nickname=True
        ).run(context)

        assert strict.status == TestStatus.FAIL
        assert {m["kind"] for m in strict.details["mismatches"]} == {"nickname_only"}
        # 19 resistors and 9 capacitors.
        assert len(strict.details["mismatches"]) == 28
        assert relaxed.status == TestStatus.PASS, relaxed.message
        assert len(relaxed.details["ignored"]) == 28

    def test_a_different_footprint_still_fails(self, tmp_path):
        board = variant(
            tmp_path,
            rename_footprints(
                lambda n: n == "Resistor_SMD:R_0402_1005Metric", lambda _: "R_0603_1608Metric"
            ),
        )
        result = FootprintMatchesSchematicCheck(check_pins=False, ignore_library_nickname=True).run(
            self._context(board)
        )

        assert result.status == TestStatus.FAIL
        assert {m["kind"] for m in result.details["mismatches"]} == {"name"}


@pytest.mark.integration
class TestFootprintMatchesSchematicPads:
    def test_missing_pad_fails_even_when_nicknames_are_ignored(self, kicad_cli_required, tmp_path):
        from kicad_evaltor.core.context import DesignContext

        def strip(document: list[SExpr]) -> None:
            rename_footprints(lambda n: n.startswith("Resistor_SMD:"), bare)(document)
            u5 = board_footprint(document, "U5")
            u5.remove(next(p for p in children(u5, "pad") if value_of(p) == "8"))

        board = variant(tmp_path, strip)
        context = DesignContext(project_path=board.with_suffix(".kicad_pro"))

        result = FootprintMatchesSchematicCheck(ignore_library_nickname=True).run(context)

        assert result.status == TestStatus.FAIL
        [pads] = result.details["mismatches"]
        assert pads["kind"] == "pads"
        assert pads["reference"] == "U5"
        assert pads["missing_pads"] == ["8"]
        assert {m["kind"] for m in result.details["ignored"]} == {"nickname_only"}


# ------------------------------------------------------- against pcbnew


@pytest.fixture(scope="module")
def kicad_python():
    from kicad_evaltor.utils.kicad_python import find_kicad_python

    python = find_kicad_python()
    if python is None:
        pytest.skip("KiCad's Python (with pcbnew) not found")
    return python


@pytest.mark.integration
class TestRecordingsMatchPcbnew:
    def test_recorded_outline_and_pads(self, kicad_python):
        from kicad_evaltor.pcbnew_bridge import dump_board

        live = dump_board(BOARD, python=kicad_python, fill_zones=False)
        saved = recorded()
        assert live.outline == saved.outline
        assert {f.reference: f.pads for f in live.footprints} == {
            f.reference: f.pads for f in saved.footprints
        }


@pytest.mark.integration
class TestCornersOnEditedBoards:
    def _outline(self, board: Path, kicad_python):
        from kicad_evaltor.pcbnew_bridge import dump_board

        return dump_board(board, python=kicad_python, fill_zones=False)

    def test_square_corners_measure_zero(self, kicad_python, tmp_path):
        board = variant(tmp_path, square_corners)
        geometry = self._outline(board, kicad_python)
        context = file_context(board, geometry=geometry)

        result = BoardOutlineCheck(corner_radius_mm=0).run(context)

        assert result.status == TestStatus.PASS, result.message
        corners = result.details["corners"]
        assert [(c["radius_mm"], c["source"], c["turn_deg"]) for c in corners] == [
            (0.0, "vertex", 90.0)
        ] * 4
        assert BoardOutlineCheck(corner_radius_mm=5).run(context).status == TestStatus.FAIL

    def test_segment_corners_are_fitted(self, kicad_python, tmp_path):
        board = variant(tmp_path, segment_corners)
        assert edge_cut_arcs(board) == ()
        geometry = self._outline(board, kicad_python)
        context = file_context(board, geometry=geometry)

        result = BoardOutlineCheck(corner_radius_mm=5, corner_radius_tolerance_mm=0.05).run(context)

        assert result.status == TestStatus.PASS, result.message
        corners = result.details["corners"]
        assert len(corners) == 4
        assert {c["source"] for c in corners} == {"fit"}
        assert all(c["radius_mm"] == pytest.approx(5, abs=0.01) for c in corners)
