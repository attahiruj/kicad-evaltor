"""PCB authoring measurements against the existing real-board recordings."""

import math

import pytest

from conftest import ESP_BOARD, recorded_geometry
from kicad_evaltor import decoupling_candidates, load_board, pad_distance
from kicad_evaltor.board_connectivity import _point_at, _position_along
from kicad_evaltor.board_file import BoardTrack


def test_arc_interior_parameter_and_exact_arc_position():
    # An analytic track primitive, not a generated PCB fixture.
    arc = BoardTrack((1, 0), (-1, 0), 0.2, "F.Cu", "signal", mid=(0, 1))
    assert arc.length == pytest.approx(math.pi)
    assert _position_along(arc, (0, 1)) == pytest.approx(0.5)
    assert _point_at(arc, 0.5) == pytest.approx((0, 1))
    assert _position_along(arc, (0, 1.5)) is None
    reversed_arc = BoardTrack((-1, 0), (1, 0), 0.2, "F.Cu", "signal", mid=(0, 1))
    assert _position_along(reversed_arc, (0, 1)) == pytest.approx(0.5)


def test_real_board_pad_measurement_has_no_acceptance_limit():
    geometry = recorded_geometry()
    gap = pad_distance(geometry, "C4", "C5")
    assert gap is not None
    assert gap >= 0
    assert gap == pad_distance(geometry, "C5", "C4")
    assert pad_distance(geometry, "missing", "C4") is None


def test_real_board_candidates_respect_explicit_return_and_part_selection():
    board, geometry = load_board(ESP_BOARD), recorded_geometry()
    ic = board.footprint("U2")
    pins = [p.number for p in ic.pads if p.pin_type == "power_in" and p.net != "GND"]
    caps = [fp.reference for fp in board.footprints if fp.reference.startswith("C")]
    candidates = decoupling_candidates(
        board,
        geometry,
        "U2",
        capacitor_references=iter(caps),
        return_nets=("GND",),
        power_pins=pins,
    )
    assert candidates
    assert all(c.capacitor in caps and c.return_nets == ("GND",) for c in candidates)
    assert (
        decoupling_candidates(
            board,
            geometry,
            "U2",
            capacitor_references=caps,
            return_nets=("unrelated_return",),
            power_pins=pins,
        )
        == ()
    )
