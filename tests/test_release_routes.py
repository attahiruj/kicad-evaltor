"""Routes through pad copper, multi-net path lengths, and silkscreen ink.

The board is the real KiCad 10 imu design under ``tests/fixtures/boards/imu``
and copies of it changed one way by ``tools/record_pcb_fixtures.py``
(``IMU_VARIANTS``):

* ``base``: USB D+/D- routed from the USB-C connector J1 *through* a
  USBLC6-2P6 ESD array (U1) to the ESP32-C6 module (track into one ESD pin, a
  track under the package to the other pin, a track out). KiCad sees a
  complete route; the copper joining the pieces is the pads.
* ``via``: D+'s link from J1.A6 to J1.B6 dropped to B.Cu through two vias,
  with via height counted, as another agent routed it.
* ``silk``: field report #9's "LiPo 1S" (0.8 mm, 0.14 mm stroke) over the
  bottom edge, whose frame box crosses the edge by 0.0137 mm while its printed
  ink stays 0.2672 mm inside, DRC clean; ``text_off`` moves it past the edge.
* ``cut``: the track under U1 removed.

Unit tests replay the pcbnew geometry recorded from them (``geometry.json``,
and ``texts_<variant>.json`` for the variants that only add board text); the
integration tests re-run KiCad.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeContext
from kicad_evaltor.board_connectivity import (
    ROUTE_METHOD,
    TRACK_VIA_METHOD,
    PathMeasurement,
    RouteMeasurement,
    measure_path_length,
    measure_route,
    pad_to_pad_length,
)
from kicad_evaltor.board_file import FileBoard, load_board
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.pcb import TextOffBoardCheck, TraceLengthCheck
from kicad_evaltor.netlist import parse_netlist
from kicad_evaltor.pcbnew_bridge import BoardGeometry, geometry_from_dict

BOARDS = Path(__file__).resolve().parent / "fixtures" / "boards"
TOOLS = Path(__file__).resolve().parent.parent / "tools"

IMU = BOARDS / "imu"
BOARD = IMU / "imu.kicad_pcb"
NETLIST = IMU / "netlist_imu.xml"

pytestmark = pytest.mark.skipif(
    not BOARD.exists(), reason="the imu design under tests/fixtures/boards is not in this checkout"
)


def recorded(variant: str = "base") -> dict[str, Any]:
    """pcbnew's dump of a variant: the base recording, with its own board texts."""
    data: dict[str, Any] = json.loads((IMU / "geometry.json").read_text("utf-8"))
    if variant != "base":
        data["texts"] = json.loads((IMU / f"texts_{variant}.json").read_text("utf-8"))["texts"]
    return data


def geometry(variant: str = "base") -> BoardGeometry:
    return geometry_from_dict(recorded(variant))


def recorder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "record_pcb_fixtures", TOOLS / "record_pcb_fixtures.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def board_of(variant: str, tmp_path: Path) -> Path:
    """The variant's board, built from the imu design in ``tmp_path``."""
    if variant == "base":
        return BOARD
    built: Path = recorder().make_imu_variant(variant, tmp_path / variant)
    return built


def route(board: FileBoard, geo: BoardGeometry, net: str, start: str, end: str, **kw: Any):
    def pads(name: str):
        ref, number = name.split(".", 1)
        fp = geo.footprint(ref)
        assert fp is not None
        return fp.pads_numbered(number)

    return measure_route(board, geo, net, pads(start), pads(end), **kw)


# (variant, net, start pad, end pad, ESD pads crossed, measured length, KiCad net total)
USB_ROUTES = [
    ("base", "/USB_D+", "J1.A6", "U4.18", ("U1.3", "U1.4"), 30.6066, 30.6061),
    ("base", "/USB_D-", "J1.A7", "U4.17", ("U1.1", "U1.6"), 29.6028, 30.6068),
    # Two vias, each counted as the 1.6 mm board thickness: 3.2 mm more.
    ("via", "/USB_D+", "J1.A6", "U4.18", ("U1.3", "U1.4"), 33.8066, 33.8061),
]


class TestRouteThroughPadCopper:
    @pytest.mark.parametrize(
        ("variant", "net", "start", "end", "esd", "length", "kicad_total"), USB_ROUTES
    )
    def test_usb_routed_through_the_esd_array_measures(
        self, tmp_path, variant, net, start, end, esd, length, kicad_total
    ):
        board = load_board(board_of(variant, tmp_path))
        result = route(board, geometry(), net, start, end)

        assert result.status == "measured"
        assert result.method == ROUTE_METHOD
        assert result.reason is None
        assert result.pieces == 1
        assert set(esd) <= set(result.pads_crossed)
        assert result.length_mm == pytest.approx(length, abs=1e-3)
        # KiCad's net length (30.606 mm in the field report).
        assert board.net_length(net) == pytest.approx(kicad_total, abs=1e-3)
        assert pad_to_pad_length(board, geometry(), net, *_pads(geometry(), start, end)) == (
            pytest.approx(length, abs=1e-3)
        )

    def test_the_difference_from_kicads_net_total_is_explained(self, tmp_path):
        # KiCad's net total counts every track and via of the net; the route
        # counts the copper between the two pads. On D+ every track is on the
        # path (J1.A6 to J1.B6 included), so the route is the net total plus
        # the two 0.5 µm steps it crosses inside U1's pads.
        board = load_board(BOARD)
        geo = geometry()
        plus = route(board, geo, "/USB_D+", "J1.A6", "U4.18")
        assert plus.length_mm - board.net_length("/USB_D+") == pytest.approx(0.0005, abs=2e-4)
        # D-: the 1.005 mm stub from J1.B7 joins the route beside J1.A7 and is
        # not on the path from J1.A7.
        minus = route(board, geo, "/USB_D-", "J1.A7", "U4.17")
        stub = 141.755 - 140.75
        assert board.net_length("/USB_D-") - minus.length_mm == pytest.approx(
            stub - 0.001, abs=2e-4
        )
        # With the J1.A6-J1.B6 link on B.Cu, both vias are on the path and
        # their height (2 x 1.6 mm) is in the route and in KiCad's total alike.
        via = load_board(board_of("via", tmp_path))
        measured = route(via, geo, "/USB_D+", "J1.A6", "U4.18")
        assert measured.via_height_counted is True
        assert via.uses_via_height is True
        assert measured.length_mm - plus.length_mm == pytest.approx(2 * 1.6, abs=1e-6)
        assert via.net_length("/USB_D+") - via.routed_length("/USB_D+") == pytest.approx(
            2 * 1.6, abs=1e-6
        )

    def test_without_pad_copper_the_route_splits_into_three(self):
        board = load_board(BOARD)
        result = route(board, geometry(), "/USB_D+", "J1.A6", "U4.18", through_pads=False)

        assert result.status == "unresolved"
        assert result.method == TRACK_VIA_METHOD
        assert result.reason_code == "split"
        assert result.pieces == 3
        assert "splits into 3 pieces" in (result.reason or "")

    def test_a_missing_track_under_the_esd_stays_unresolved(self, tmp_path):
        # The track under U1 from pin 3 to pin 4 removed: the two pads are no
        # longer joined by copper, only by the part, which does not conduct here.
        cut = board_of("cut", tmp_path)
        board = load_board(cut)
        geo = geometry()

        result = route(board, geo, "/USB_D+", "J1.A6", "U4.18")

        assert result.status == "unresolved"
        assert result.length_mm is None
        assert result.reason_code == "split"
        assert result.pieces == 2
        assert pad_to_pad_length(board, geo, "/USB_D+", *_pads(geo, "J1.A6", "U4.18")) is None

    def test_a_pad_no_track_reaches_is_named(self):
        board = load_board(BOARD)
        geo = geometry()

        start = route(board, geo, "/USB_D+", "U1.2", "U4.18")  # U1.2 is GND
        end = route(board, geo, "/USB_D+", "J1.A6", "U1.5")  # U1.5 is VBUS

        assert (start.reason_code, end.reason_code) == ("start_not_reached", "end_not_reached")
        assert (start.reason or "").startswith("Start pad not reached")
        assert (end.reason or "").startswith("End pad not reached")

    def test_older_constructor_still_works(self):
        old = RouteMeasurement(1.0, "N", "measured", False, (0.0, 0.0))

        assert (old.method, old.reason, old.pads_crossed) == (TRACK_VIA_METHOD, None, ())

    def test_trace_length_check_reports_the_crossed_pads(self):
        board = load_board(BOARD)
        context = FakeContext(board=board, geometry=geometry())

        result = TraceLengthCheck(from_pad="J1.A6", to_pad="U4.18", max_length_mm=31).run(context)

        assert result.status == TestStatus.PASS
        assert result.details["length_mm"] == pytest.approx(30.6066, abs=1e-3)
        assert {"U1.3", "U1.4"} <= set(result.details["pads_crossed"])

    def test_trace_length_check_says_why_it_cannot_measure(self, tmp_path):
        cut = board_of("cut", tmp_path)
        context = FakeContext(board=load_board(cut), geometry=geometry())

        result = TraceLengthCheck(from_pad="J1.A6", to_pad="U4.18", max_length_mm=31).run(context)

        assert result.status == TestStatus.ERROR
        assert "splits into 2 pieces" in result.message
        assert result.details["route_reason"] == "split"


def _pads(geo: BoardGeometry, start: str, end: str):
    found = []
    for name in (start, end):
        ref, number = name.split(".", 1)
        fp = geo.footprint(ref)
        assert fp is not None
        found.append(fp.pads_numbered(number))
    return found


class TestPathLength:
    @pytest.mark.parametrize(
        ("start", "resistor", "first_net", "lengths"),
        [
            # ESP32 IO2 -> 100R -> blue LED anode.
            ("U4.5", "R14", "Net-(U4-IO2)", (7.9228, 1.7025)),
        ],
    )
    def test_a_signal_through_a_series_resistor(self, start, resistor, first_net, lengths):
        board = load_board(BOARD)

        result = measure_path_length(board, geometry(), start, "D3.2", through=[resistor])

        assert isinstance(result, PathMeasurement)
        assert result.status == "measured"
        assert result.nets == (first_net, "Net-(D3-A)")
        assert result.hops == (resistor,)
        assert [s.length_mm for s in result.segments] == pytest.approx(lengths, abs=1e-3)
        assert result.length_mm == pytest.approx(sum(lengths), abs=1e-3)
        # Each net is one straight route here, so KiCad's net total agrees.
        for segment in result.segments:
            assert segment.kicad_net_length_mm == pytest.approx(segment.length_mm, abs=1e-6)
        assert result.kicad_net_length_mm == pytest.approx(sum(lengths), abs=1e-3)
        assert result.segments[0].start == start
        assert result.segments[0].end.startswith(f"{resistor}.")
        assert result.segments[1].end == "D3.2"

    def test_a_flow_through_part_needs_no_hop(self):
        board = load_board(BOARD)

        result = measure_path_length(board, geometry(), "J1.A6", "U4.18", through=["U1"])

        assert result.status == "measured"
        assert result.nets == ("/USB_D+",)
        assert result.hops == ()
        assert result.length_mm == pytest.approx(30.6066, abs=1e-3)
        assert {"U1.3", "U1.4"} <= set(result.segments[0].route.pads_crossed)
        assert result.kicad_net_length_mm == pytest.approx(30.6061, abs=1e-3)

    def test_no_chain_without_the_part(self):
        board = load_board(BOARD)

        result = measure_path_length(board, geometry(), "U4.5", "D3.2")

        assert result.status == "unresolved"
        assert result.length_mm is None
        assert "No chain of nets" in (result.reason or "")
        # The pads' nets are reported even when no path is.
        assert (result.start_net, result.end_net) == ("Net-(U4-IO2)", "Net-(D3-A)")

    def test_expected_nets_from_the_schematic_agree(self):
        board = load_board(BOARD)
        netlist = parse_netlist(NETLIST.read_text("utf-8"), "kicadxml")
        expected = (netlist.net_of("U4", "5"), netlist.net_of("D3", "2"))

        result = measure_path_length(
            board, geometry(), "U4.5", "D3.2", through=["R14"], expect_nets=expected
        )

        assert result.status == "measured"
        assert (result.start_net, result.end_net) == expected

    def test_a_board_out_of_step_with_the_schematic_is_not_measured(self):
        # Stand-in for a stale board: the schematic's net for U4.6 is expected
        # at U4.5, as if the board still carried an older pin assignment.
        board = load_board(BOARD)
        netlist = parse_netlist(NETLIST.read_text("utf-8"), "kicadxml")
        stale = (netlist.net_of("U4", "6"), netlist.net_of("D3", "2"))
        assert stale[0] != "Net-(U4-IO2)"

        result = measure_path_length(
            board, geometry(), "U4.5", "D3.2", through=["R14"], expect_nets=stale
        )

        assert result.status == "unresolved"
        assert result.length_mm is None
        assert f"U4.5 is on Net-(U4-IO2), expected {stale[0]}" in (result.reason or "")
        assert "D3.2" not in (result.reason or "")
        assert result.start_net == "Net-(U4-IO2)"

    @pytest.mark.parametrize(
        ("start", "through", "reason"),
        [
            ("U4.999", ["R14"], "not found"),
            ("U4", ["R14"], "REF.NUMBER"),
            ("U4.5", ["R99"], "R99 not found"),
        ],
    )
    def test_bad_input_is_a_reason_not_an_exception(self, start, through, reason):
        board = load_board(BOARD)

        result = measure_path_length(board, geometry(), start, "D3.2", through=through)

        assert result.status == "unresolved"
        assert reason in (result.reason or "")

    def test_a_broken_segment_names_its_net(self, tmp_path):
        cut = board_of("cut", tmp_path)

        result = measure_path_length(load_board(cut), geometry(), "J1.A6", "U4.18", through=["U1"])

        assert result.status == "unresolved"
        assert result.length_mm is None
        assert (result.reason or "").startswith("/USB_D+ (J1.A6 to U4.18): The copper")


def silk_context(
    tmp_path: Path, variant: str = "silk", data: dict[str, Any] | None = None
) -> FakeContext:
    return FakeContext(
        board=load_board(board_of(variant, tmp_path)),
        geometry=geometry_from_dict(data or recorded(variant)),
    )


class TestTextInk:
    def test_ink_matches_kicads_render(self):
        # On silk_text_edge kicad-cli pcb export svg drew "LiPo 1S" from x
        # 62.7776 to 67.2605 and y 98.7928 to 99.7328 (stroke centre lines plus
        # half the 0.14 stroke), over a bottom edge at y 100. The variant moves
        # the same text by (46, 50) over this board's bottom edge at y 150.
        text = next(t for t in geometry("silk").all_texts() if t.text == "LiPo 1S")

        assert text.ink_bbox == pytest.approx((108.7776, 148.7928, 113.2605, 149.7328), abs=1e-4)
        assert text.bbox[3] == pytest.approx(150.0137, abs=1e-4)

    def test_the_base_board_has_no_text_near_an_edge(self):
        base = FakeContext(board=load_board(BOARD), geometry=geometry())
        for method in ("ink", "bbox"):
            assert TextOffBoardCheck(method=method).run(base).status == TestStatus.PASS

    def test_text_whose_frame_box_crosses_the_edge_passes(self, tmp_path):
        result = TextOffBoardCheck().run(silk_context(tmp_path))

        assert result.status == TestStatus.PASS
        assert result.details["method"] == "ink"
        assert result.details["bbox_fallback"] == 0

    def test_the_frame_box_method_is_still_available(self, tmp_path):
        strict = TextOffBoardCheck(method="bbox").run(silk_context(tmp_path))
        loose = TextOffBoardCheck(method="bbox", tolerance_mm=0.02).run(silk_context(tmp_path))

        assert strict.status == TestStatus.FAIL
        (entry,) = strict.details["texts"]
        assert (entry["text"], entry["overhang_mm"]) == ("LiPo 1S", pytest.approx(0.0137))
        assert loose.status == TestStatus.PASS
        assert loose.details["within_tolerance"][0]["overhang_mm"] == pytest.approx(0.0137)

    def test_text_moved_half_a_millimetre_past_the_edge_fails(self, tmp_path):
        result = TextOffBoardCheck().run(silk_context(tmp_path, "text_off"))

        assert result.status == TestStatus.FAIL
        (entry,) = result.details["texts"]
        assert entry["text"] == "LiPo 1S"
        assert entry["measured"] == "ink"
        assert entry["overhang_mm"] == pytest.approx(0.5, abs=1e-3)
        assert TextOffBoardCheck(tolerance_mm=0.6).run(silk_context(tmp_path, "text_off")).is_pass

    def test_a_recording_without_ink_falls_back_to_the_frame_box(self, tmp_path):
        data = recorded("silk")

        def strip(texts: list[dict[str, Any]]) -> None:
            for text in texts:
                text.pop("ink", None)

        strip(data["texts"])
        for footprint in data["footprints"]:
            strip(footprint["texts"])

        result = TextOffBoardCheck().run(silk_context(tmp_path, data=data))

        assert result.status == TestStatus.FAIL
        assert result.details["bbox_fallback"] == result.details["checked"]
        assert result.details["texts"][0]["measured"] == "bbox"

    @pytest.mark.parametrize("kwargs", [{"method": "glyphs"}, {"tolerance_mm": -0.1}])
    def test_bad_parameters(self, kwargs):
        with pytest.raises(ValueError):
            TextOffBoardCheck(**kwargs)


@pytest.fixture(scope="module")
def kicad_python():
    from kicad_evaltor.utils.kicad_python import find_kicad_python

    python = find_kicad_python()
    if python is None:
        pytest.skip("KiCad's Python (with pcbnew) not found")
    return python


@pytest.mark.integration
class TestAgainstKicad:
    """Re-run KiCad: the recordings still hold, and the numbers match KiCad's own."""

    @pytest.mark.parametrize("variant", ["base", "silk", "text_off"])
    def test_geometry_recordings_match_pcbnew(self, kicad_python, tmp_path, variant):
        from kicad_evaltor.pcbnew_bridge import dump_board_json

        board = recorder().make_imu_variant(variant, tmp_path / variant)
        fresh = dump_board_json(board, python=kicad_python, fill_zones=True)

        # A text variant's recording is the base one with its own board texts.
        assert json.loads(json.dumps(fresh, sort_keys=True)) == recorded(variant)

    @pytest.mark.parametrize(
        ("variant", "net", "start", "end", "esd", "length", "kicad_total"), USB_ROUTES
    )
    def test_net_totals_match_pcbnew(
        self, kicad_python, tmp_path, variant, net, start, end, esd, length, kicad_total
    ):
        from kicad_evaltor.utils.kicad_python import run_kicad_python

        script = tmp_path / "net_length.py"
        script.write_text(
            "import sys, pcbnew\n"
            "board = pcbnew.LoadBoard(sys.argv[1])\n"
            "tracks = [t for t in board.GetTracks() if t.GetNetname() == sys.argv[2]]\n"
            "print(sum(t.GetLength() for t in tracks if not isinstance(t, pcbnew.PCB_VIA)) / 1e6,"
            " sum(isinstance(t, pcbnew.PCB_VIA) for t in tracks))\n",
            encoding="utf-8",
        )
        path = board_of(variant, tmp_path)
        result = run_kicad_python(kicad_python, script, [str(path), net])
        tracks, vias = result.stdout.split()[-2:]
        board = load_board(path)

        # KiCad's track sum, plus 1.6 mm of via height per via where the
        # project counts via height, is the net total the report quotes.
        assert float(tracks) == pytest.approx(board.routed_length(net), abs=1e-6)
        assert float(tracks) + 1.6 * int(vias) == pytest.approx(kicad_total, abs=1e-3)

    def test_kicad_sees_the_cut_route_as_incomplete(self, kicad_cli, kicad_python, tmp_path):
        from kicad_evaltor.pcbnew_bridge import dump_board

        if kicad_cli is None:
            pytest.skip("kicad-cli not found")
        cut = board_of("cut", tmp_path)
        report = tmp_path / "drc.json"
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "drc",
                "--refill-zones",
                "--format",
                "json",
                "-o",
                str(report),
                str(cut),
            ],
            check=True,
            capture_output=True,
        )
        unconnected = json.loads(report.read_text("utf-8"))["unconnected_items"]
        geo = dump_board(cut, python=kicad_python)

        # KiCad's DRC: the track that ended on U1 pin 3 is no longer joined to it.
        assert any(
            "Pad 3 [/USB_D+] of U1" in i["description"] for u in unconnected for i in u["items"]
        )
        assert route(load_board(cut), geo, "/USB_D+", "J1.A6", "U4.18").status == "unresolved"

    def test_ink_matches_the_svg_render(self, kicad_cli, tmp_path):
        board = board_of("silk", tmp_path)
        if kicad_cli is None:
            pytest.skip("kicad-cli not found")
        svg = tmp_path / "silk.svg"
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "export",
                "svg",
                "--layers",
                "F.Silkscreen,Edge.Cuts",
                "--mode-single",
                "--exclude-drawing-sheet",
                "--page-size-mode",
                "2",
                "-o",
                str(svg),
                str(board),
            ],
            check=True,
            capture_output=True,
        )
        drawn = svg.read_text("utf-8")
        start = drawn.index('<g class="stroked-text"><desc>LiPo 1S</desc>')
        width = float(re.findall(r"stroke-width:([0-9.]+)", drawn[:start])[-1])
        glyphs = drawn[start : drawn.index("</g>", start)]
        bottom = max(float(y) for y in re.findall(r"[ML]-?[\d.]+ (-?[\d.]+)", glyphs))
        # Silkscreen stays on the board here, so the lowest point drawn is the
        # board's bottom edge (y 150 on the board).
        edge = max(float(y) for y in re.findall(r"[ML]-?[\d.]+ (-?[\d.]+)", drawn))
        text = next(t for t in geometry("silk").all_texts() if t.text == "LiPo 1S")
        ink = text.ink_bbox
        assert ink is not None

        # The stroke's round end reaches half its width past the centre line.
        assert edge - (bottom + width / 2) == pytest.approx(150.0 - ink[3], abs=1e-3)
        # As on silk_text_edge, whose edge was at y 100.
        assert 150.0 - ink[3] == pytest.approx(0.2672, abs=1e-3)
