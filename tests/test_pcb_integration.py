"""Board checks against real KiCad: kicad-cli and pcbnew on the example board.

Two jobs. First, prove the recordings the unit tests replay still say what
KiCad says now. Second, run every check end to end through a real
DesignContext, on the board and on copies broken by ``tools/record_pcb_fixtures.py``.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from conftest import ESP_BOARD, PCB_RECORDINGS
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.pcb import (
    BoardOutlineCheck,
    DRCRunCheck,
    FabExportsCheck,
    FootprintMatchesSchematicCheck,
    FootprintNearCheck,
    FootprintOnBoardCheck,
    FootprintOverlapCheck,
    PositionFileCheck,
    PowerPourCheck,
    RoutingCompleteCheck,
    SchematicParityCheck,
    TraceLengthCheck,
)
from kicad_evaltor.checks.schematic import ConsistencyCheck
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.board_file import load_board
from kicad_evaltor.diff_pairs import DiffPair, measure_pair
from kicad_evaltor.pcbnew_bridge import dump_board_json
from kicad_evaltor.utils.kicad_python import find_kicad_python

pytestmark = pytest.mark.integration

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _recorder():
    spec = importlib.util.spec_from_file_location(
        "record_pcb_fixtures", TOOLS / "record_pcb_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorder():
    return _recorder()


@pytest.fixture(scope="module")
def kicad_python():
    python = find_kicad_python()
    if python is None:
        pytest.skip("KiCad's Python (with pcbnew) not found")
    return python


@pytest.fixture(scope="module")
def variants(recorder, kicad_cli, kicad_python, tmp_path_factory):
    if kicad_cli is None:
        pytest.skip("kicad-cli not found")
    root = tmp_path_factory.mktemp("pcb-variants")
    return {name: recorder.make_variant(name, root / name) for name in recorder.VARIANTS}


def context(board: Path) -> DesignContext:
    return DesignContext(project_path=board.with_suffix(".kicad_pro"))


class TestRecordingsMatchKicad:
    @pytest.mark.parametrize(
        "name", ["base", "unrouted", "parity", "overlap", "offboard", "tuning"]
    )
    def test_drc_recording(self, recorder, variants, kicad_cli, name):
        fresh = recorder.run_drc(kicad_cli, variants[name])
        recorded = json.loads((PCB_RECORDINGS / f"{name}.drc.json").read_text("utf-8"))

        def item(description):
            # Between equally near track segments KiCad picks either one to end
            # an unrouted connection, so the "length ..." it quotes varies.
            return re.sub(r", length [0-9.]+ mm", "", description)

        def findings(report):
            return sorted(
                # KiCad names the two items of a pair in either order.
                (
                    key,
                    e["type"],
                    e["severity"],
                    tuple(sorted(item(i["description"]) for i in e["items"])),
                )
                for key in ("violations", "unconnected_items", "schematic_parity")
                for e in report[key]
            )

        assert findings(fresh) == findings(recorded)

    @pytest.mark.parametrize("name", ["base", "offboard"])
    def test_geometry_recording(self, variants, kicad_python, name):
        fresh = dump_board_json(variants[name], python=kicad_python, fill_zones=True)
        recorded = json.loads((PCB_RECORDINGS / f"{name}.geometry.json").read_text("utf-8"))

        assert json.loads(json.dumps(fresh, sort_keys=True)) == recorded


class TestChecksEndToEnd:
    def test_the_clean_board(self, kicad_python):
        ctx = context(ESP_BOARD)
        results = {
            check.id: check.run(ctx)
            for check in (
                DRCRunCheck(severity="all"),
                RoutingCompleteCheck(),
                SchematicParityCheck(),
                BoardOutlineCheck(width_mm=50, height_mm=50),
                PowerPourCheck(net_name="GND", min_area_mm2=3000),
                FootprintNearCheck(decoupling="U2", max_distance_mm=1),
                TraceLengthCheck(from_pad="U2.1", to_pad="C9.1", max_length_mm=5),
                FootprintMatchesSchematicCheck(),
                ConsistencyCheck(),
                FabExportsCheck(),
                PositionFileCheck(),
            )
        }

        assert {k: r.status for k, r in results.items()} == {k: TestStatus.PASS for k in results}

    def test_relative_paths_work(self, kicad_python, monkeypatch):
        # kicad-cli runs from a scratch folder, so the board path must be
        # resolved before it gets there.
        monkeypatch.chdir(ESP_BOARD.parent.parent)
        ctx = DesignContext(board_path=Path(ESP_BOARD.parent.name) / ESP_BOARD.name)

        assert DRCRunCheck(schematic_parity=False).run(ctx).status == TestStatus.PASS

    def test_unrouted_net_is_reported(self, variants):
        result = RoutingCompleteCheck().run(context(variants["unrouted"]))

        assert result.status == TestStatus.FAIL
        assert [f["net"] for f in result.details["findings"]] == ["/SD_CLK"]

    def test_missing_footprint_is_reported(self, variants):
        result = SchematicParityCheck().run(context(variants["parity"]))

        assert [f["type"] for f in result.details["findings"]] == ["missing_footprint"]

    def test_overlap_is_reported(self, variants):
        assert FootprintOverlapCheck().run(context(variants["overlap"])).status == TestStatus.FAIL

    def test_off_board_part_is_reported(self, variants):
        result = FootprintOnBoardCheck(exclude_refs=["U1"]).run(context(variants["offboard"]))

        assert [f["reference"] for f in result.details["footprints"]] == ["TP3"]


LENGTH_PROBE = """(version 1)
(rule all (condition "A.Type == 'Track' || A.Type == 'Via'")
  (constraint length (max 0.001mm)))
"""

GAP_PROBE = """(version 1)
(rule gap
  (condition "A.Type == 'Track' && B.Type == 'Track' && A.NetName == '/RX0' && B.NetName == '/TX0'")
  (constraint clearance (min 1mm)))
"""


def _probe(recorder, kicad_cli, tmp_path, rules: str) -> list[dict]:
    """KiCad's DRC on a copy of the example board with custom rules added.

    A rule that cannot be met makes DRC report KiCad's own measurement in the
    violation, which is the reference our numbers are held to.
    """
    board = recorder.make_variant("base", tmp_path / "probe")
    board.with_suffix(".kicad_dru").write_text(rules, encoding="utf-8")
    return recorder.run_drc(kicad_cli, board)["violations"]


class TestMeasurementsMatchKicad:
    def test_every_net_length(self, recorder, kicad_cli, tmp_path):
        if kicad_cli is None:
            pytest.skip("kicad-cli not found")
        found = _probe(recorder, kicad_cli, tmp_path, LENGTH_PROBE)
        board = load_board(ESP_BOARD)
        kicad = {}
        for v in found:
            if v["type"] == "length_out_of_range":
                net = re.search(r"\[([^\]]+)\]", v["items"][0]["description"]).group(1)
                kicad[net] = float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))

        assert len(kicad) == 21
        # KiCad prints lengths to 0.0001 mm.
        assert {n: board.net_length(n) for n in kicad} == pytest.approx(kicad, abs=1e-3)

    def test_pair_gap(self, recorder, kicad_cli, tmp_path):
        if kicad_cli is None:
            pytest.skip("kicad-cli not found")
        found = _probe(recorder, kicad_cli, tmp_path, GAP_PROBE)
        clearances = [
            float(re.search(r"actual ([0-9.]+) mm", v["description"]).group(1))
            for v in found
            if v["type"] == "clearance"
        ]
        measured = measure_pair(
            load_board(ESP_BOARD), DiffPair("", "/RX0", "/TX0"), coupling_distance=1.0
        )

        assert measured.gap_min == pytest.approx(min(clearances), abs=1e-3)
