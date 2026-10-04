"""Tests for the geometry primitives.

The placement cases are the oracle for the transform: a copy of the demo sheet's
J1 connector was rendered by the installed KiCad with each orientation, and the
drawn pin lines were read back out of the exported SVG. Those measured endpoints
are hard-coded below, so a sign error in the y flip or the rotation cannot pass.
"""

import math

import pytest

from kicad_evaltor.geometry import (
    BBox,
    Placement,
    normalize_angle,
    rotate_point,
)

# Connector_Generic:Conn_01x02, as the demo sheet uses it: both pins on the
# library-local left at y 0 and y -2.54, each 3.81 mm long and running toward
# the body. Measured pin lines in sheet coordinates, for a symbol placed at
# (102.87, 104.14).
ANCHOR = (102.87, 104.14)
LOCAL_PIN_1 = (-5.08, 0.0)
LOCAL_PIN_2 = (-5.08, -2.54)
PIN_LENGTH = 3.81

PROBE_PINS = {
    0.0: (
        ((97.79, 104.14), (101.6, 104.14)),
        ((97.79, 106.68), (101.6, 106.68)),
    ),
    90.0: (
        ((102.87, 109.22), (102.87, 105.41)),
        ((105.41, 109.22), (105.41, 105.41)),
    ),
    180.0: (
        ((107.95, 104.14), (104.14, 104.14)),
        ((107.95, 101.6), (104.14, 101.6)),
    ),
    270.0: (
        ((102.87, 99.06), (102.87, 102.87)),
        ((100.33, 99.06), (100.33, 102.87)),
    ),
}

PROBE_PINS_MIRRORED = {
    "y": (
        ((107.95, 104.14), (104.14, 104.14)),
        ((107.95, 106.68), (104.14, 106.68)),
    ),
    "x": (
        ((97.79, 104.14), (101.6, 104.14)),
        ((97.79, 101.6), (101.6, 101.6)),
    ),
}

LOCAL_BODY = BBox(-1.27, -3.81, 1.27, 1.27)


def _pin_line(placement, local):
    """The line KiCad draws for a pin: its connection point, then its body end.

    Both connector pins run along their own angle of 0, so the body end is the
    connection point plus the pin length in x.
    """
    connection = placement.apply(*local)
    body = placement.apply(local[0] + PIN_LENGTH, local[1])
    return (connection, body)


class TestRotatePoint:
    def test_zero_degrees_is_identity(self):
        assert rotate_point(3.0, 4.0, 0.0) == (3.0, 4.0)

    def test_ninety_degrees(self):
        assert rotate_point(1.0, 0.0, 90.0) == (0.0, 1.0)

    def test_one_hundred_eighty_degrees(self):
        assert rotate_point(2.0, 5.0, 180.0) == pytest.approx((-2.0, -5.0))

    def test_two_seventy_degrees(self):
        assert rotate_point(0.0, 1.0, 270.0) == (1.0, 0.0)

    def test_orthogonal_angles_are_exact(self):
        # No floating point residue, so repeated placement cannot drift.
        assert rotate_point(0.1, 0.2, 90.0) == (-0.2, 0.1)
        assert rotate_point(0.1, 0.2, 180.0) == (-0.1, -0.2)
        assert rotate_point(0.1, 0.2, 270.0) == (0.2, -0.1)

    def test_negative_angles(self):
        assert rotate_point(1.0, 0.0, -270.0) == (0.0, 1.0)

    def test_angles_above_360_wrap(self):
        assert rotate_point(1.0, 0.0, 450.0) == (0.0, 1.0)

    def test_non_orthogonal_angle_is_consistent(self):
        x, y = rotate_point(1.0, 0.0, 30.0)
        assert x == pytest.approx(math.cos(math.radians(30)))
        assert y == pytest.approx(math.sin(math.radians(30)))

    def test_rotation_preserves_length(self):
        x, y = rotate_point(3.0, 4.0, 37.0)
        assert math.hypot(x, y) == pytest.approx(5.0)

    def test_rejects_non_finite_input(self):
        with pytest.raises(ValueError, match="finite"):
            rotate_point(float("nan"), 0.0, 0.0)


class TestNormalizeAngle:
    def test_wraps_into_range(self):
        assert normalize_angle(450.0) == 90.0
        assert normalize_angle(-90.0) == 270.0


class TestBBox:
    def test_from_points(self):
        box = BBox.from_points([(1.0, 2.0), (-3.0, 5.0)])
        assert box == BBox(-3.0, 2.0, 1.0, 5.0)

    def test_from_points_rejects_empty(self):
        with pytest.raises(ValueError, match="no points"):
            BBox.from_points([])

    def test_rejects_inverted_bounds(self):
        with pytest.raises(ValueError, match="min_x"):
            BBox(5.0, 0.0, 1.0, 2.0)
        with pytest.raises(ValueError, match="min_y"):
            BBox(0.0, 5.0, 1.0, 2.0)

    def test_rejects_non_finite(self):
        with pytest.raises(ValueError, match="finite"):
            BBox(0.0, 0.0, float("inf"), 1.0)

    def test_width_height_area_center(self):
        box = BBox(0.0, 0.0, 4.0, 2.0)
        assert (box.width, box.height, box.area) == (4.0, 2.0, 8.0)
        assert box.center == (2.0, 1.0)

    def test_union(self):
        assert BBox(0.0, 0.0, 1.0, 1.0).union(BBox(2.0, 3.0, 4.0, 5.0)) == BBox(0.0, 0.0, 4.0, 5.0)

    def test_union_all(self):
        merged = BBox.union_all([BBox(0.0, 0.0, 1.0, 1.0), BBox(5.0, 5.0, 6.0, 6.0)])
        assert merged == BBox(0.0, 0.0, 6.0, 6.0)

    def test_union_all_of_nothing_is_none(self):
        assert BBox.union_all([]) is None

    def test_intersection(self):
        assert BBox(0.0, 0.0, 4.0, 4.0).intersection(BBox(2.0, 2.0, 6.0, 6.0)) == BBox(
            2.0, 2.0, 4.0, 4.0
        )

    def test_touching_boxes_do_not_intersect(self):
        assert BBox(0.0, 0.0, 1.0, 1.0).intersection(BBox(1.0, 0.0, 2.0, 1.0)) is None

    def test_disjoint_boxes_do_not_intersect(self):
        assert not BBox(0.0, 0.0, 1.0, 1.0).intersects(BBox(5.0, 5.0, 6.0, 6.0))

    def test_containment_is_an_intersection(self):
        assert BBox(0.0, 0.0, 10.0, 10.0).intersects(BBox(2.0, 2.0, 3.0, 3.0))

    def test_clearance_of_overlapping_boxes_is_zero(self):
        assert BBox(0.0, 0.0, 4.0, 4.0).clearance(BBox(2.0, 2.0, 6.0, 6.0)) == 0.0

    def test_clearance_of_touching_boxes_is_zero(self):
        assert BBox(0.0, 0.0, 1.0, 1.0).clearance(BBox(1.0, 0.0, 2.0, 1.0)) == 0.0

    def test_clearance_along_one_axis_is_that_gap(self):
        assert BBox(0.0, 0.0, 1.0, 1.0).clearance(BBox(3.0, 0.0, 4.0, 1.0)) == 2.0

    def test_clearance_across_both_axes_is_the_nearest_corner(self):
        # 2mm apart in x and 5mm in y, so the closest points are 5.385mm apart.
        # Taking the smaller gap would report two boxes 5mm up as nearly touching.
        assert BBox(0.0, 0.0, 1.0, 1.0).clearance(BBox(3.0, 6.0, 4.0, 7.0)) == pytest.approx(
            math.hypot(2.0, 5.0)
        )

    def test_inflate(self):
        assert BBox(0.0, 0.0, 1.0, 1.0).inflate(0.5) == BBox(-0.5, -0.5, 1.5, 1.5)

    def test_inflate_shrinks_with_a_negative_margin(self):
        assert BBox(0.0, 0.0, 10.0, 10.0).inflate(-1.0) == BBox(1.0, 1.0, 9.0, 9.0)

    def test_shrinking_past_collapse_is_rejected(self):
        # The bounds check catches nonsense rather than returning an inverted box.
        with pytest.raises(ValueError):
            BBox(0.0, 0.0, 1.0, 1.0).inflate(-1.0)

    def test_contains_point(self):
        box = BBox(0.0, 0.0, 2.0, 2.0)
        assert box.contains_point(1.0, 1.0)
        assert box.contains_point(0.0, 0.0)
        assert not box.contains_point(3.0, 1.0)


class TestPlacementMatchesRenderedKicad:
    """The oracle: pin lines KiCad actually drew for this connector."""

    @pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0])
    def test_pin_lines_match_rendered_output(self, angle):
        placement = Placement(*ANCHOR, rotation=angle)

        drawn = PROBE_PINS[angle]
        for local, expected in zip((LOCAL_PIN_1, LOCAL_PIN_2), drawn):
            connection, body = _pin_line(placement, local)
            assert connection == pytest.approx(expected[0], abs=1e-9)
            assert body == pytest.approx(expected[1], abs=1e-9)

    @pytest.mark.parametrize("mirror", ["x", "y"])
    def test_mirrored_placement_matches_rendered_output(self, mirror):
        placement = Placement(*ANCHOR, mirror=mirror)

        drawn = PROBE_PINS_MIRRORED[mirror]
        for local, expected in zip((LOCAL_PIN_1, LOCAL_PIN_2), drawn):
            connection, body = _pin_line(placement, local)
            assert connection == pytest.approx(expected[0], abs=1e-9)
            assert body == pytest.approx(expected[1], abs=1e-9)

    @pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0])
    def test_a_body_box_lands_where_its_own_corners_do(self, angle):
        # The outline itself is not in the plot, but it has to travel by the same
        # transform as the pins that terminate on it.
        placement = Placement(*ANCHOR, rotation=angle)
        placed = placement.apply_box(LOCAL_BODY)
        corners = [
            placement.apply(x, y)
            for x in (LOCAL_BODY.min_x, LOCAL_BODY.max_x)
            for y in (LOCAL_BODY.min_y, LOCAL_BODY.max_y)
        ]
        assert placed == BBox.from_points(corners)

    def test_every_measured_pin_line_is_the_declared_length(self):
        for lines in PROBE_PINS.values():
            for connection, body in lines:
                assert math.dist(connection, body) == pytest.approx(PIN_LENGTH)


class TestPlacement:
    def test_origin_is_the_anchor(self):
        assert Placement(10.0, 20.0).apply(0.0, 0.0) == (10.0, 20.0)

    def test_translation_only(self):
        assert Placement(10.0, 20.0).apply(1.0, 2.0) == (11.0, 18.0)

    def test_local_y_is_flipped_onto_the_sheet(self):
        # Library-local y grows upwards, so a point below the library origin
        # lands below the anchor on the sheet, where y also grows downwards.
        assert Placement(10.0, 20.0).apply(1.0, -2.0) == (11.0, 22.0)

    def test_rotation_about_the_anchor(self):
        # The file's angle is counter-clockwise on screen, which in a y-down
        # frame is the negated rotation.
        assert Placement(10.0, 20.0, rotation=90.0).apply(1.0, 0.0) == (10.0, 19.0)

    def test_mirror_x_flips_local_y(self):
        # Mirroring about the local x axis cancels the library-to-sheet flip.
        assert Placement(0.0, 0.0, mirror="x").apply(1.0, 2.0) == (1.0, 2.0)

    def test_mirror_y_flips_local_x(self):
        assert Placement(0.0, 0.0, mirror="y").apply(1.0, 2.0) == (-1.0, -2.0)

    def test_mirror_applies_before_rotation(self):
        placement = Placement(0.0, 0.0, rotation=90.0, mirror="y")
        # local (1, 2) -> mirror -> (-1, 2) -> flip -> (-1, -2) -> rotate -90 -> (-2, 1)
        assert placement.apply(1.0, 2.0) == (-2.0, 1.0)

    def test_rejects_unknown_mirror(self):
        with pytest.raises(ValueError, match="mirror"):
            Placement(0.0, 0.0, mirror="z")

    def test_apply_box_is_conservative_for_odd_angles(self):
        # A 45 degree turn makes the box non axis-aligned, so the result is
        # the bounding box of the corners, which is at least as large.
        placed = Placement(0.0, 0.0, rotation=45.0).apply_box(BBox(0.0, 0.0, 4.0, 2.0))
        assert placed.width >= 4.0
        assert placed.height >= 2.0
        assert placed.width == pytest.approx(placed.height, abs=1e-9)
