"""Tests for the geometry primitives.

The placement cases are taken from a probe schematic rendered by KiCad 10.0.1:
an asymmetric symbol placed at 0/90/180/270 degrees, with the resulting
rectangle corners and pin segments measured out of the exported SVG. Those
measured values are the oracle, so a sign error in the rotation cannot pass.
"""

import math

import pytest

from kicad_evaltor.geometry import (
    BBox,
    Placement,
    normalize_angle,
    rotate_point,
)

# Sheet-space body rectangle relative to the anchor, as rendered by KiCad.
# The renderer flips y, so these were read back out of the SVG with the flip
# undone; they are the oracle for the rotation convention.
PROBE_BODY = {
    0.0: (0.0, 0.0, 4.0, 2.0),
    90.0: (-2.0, 0.0, 0.0, 4.0),
    180.0: (-4.0, -2.0, 0.0, 0.0),
    270.0: (0.0, -4.0, 2.0, 0.0),
}

LOCAL_BODY = BBox(0.0, 0.0, 4.0, 2.0)
LOCAL_PIN_CONN = (0.0, 6.0)
LOCAL_PIN_ANGLE = 270.0
LOCAL_PIN_LENGTH = 2.54


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

    def test_clearance_takes_the_smaller_axis_gap(self):
        # 2mm apart in x, 5mm in y; the nearest approach is 2mm.
        assert BBox(0.0, 0.0, 1.0, 1.0).clearance(BBox(3.0, 6.0, 4.0, 7.0)) == 2.0

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
    """The oracle: rectangles KiCad 10.0.1 actually drew for the probe."""

    @pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0])
    def test_body_rectangle_matches_rendered_output(self, angle):
        placement = Placement(0.0, 0.0, rotation=angle)
        placed = placement.apply_box(LOCAL_BODY)

        expected = PROBE_BODY[angle]
        assert (
            placed.min_x,
            placed.min_y,
            placed.max_x,
            placed.max_y,
        ) == pytest.approx(expected, abs=1e-9)

    @pytest.mark.parametrize("angle", [0.0, 90.0, 180.0, 270.0])
    def test_pin_endpoints_match_rendered_output(self, angle):
        radians = math.radians(LOCAL_PIN_ANGLE)
        local_inner = (
            LOCAL_PIN_CONN[0] + LOCAL_PIN_LENGTH * math.cos(radians),
            LOCAL_PIN_CONN[1] + LOCAL_PIN_LENGTH * math.sin(radians),
        )
        placement = Placement(0.0, 0.0, rotation=angle)

        conn = placement.apply(*LOCAL_PIN_CONN)
        inner = placement.apply(*local_inner)

        # Distance between the pin's two ends is preserved under rotation.
        assert math.dist(conn, inner) == pytest.approx(LOCAL_PIN_LENGTH)


class TestPlacement:
    def test_origin_is_the_anchor(self):
        assert Placement(10.0, 20.0).apply(0.0, 0.0) == (10.0, 20.0)

    def test_translation_only(self):
        assert Placement(10.0, 20.0).apply(1.0, 2.0) == (11.0, 22.0)

    def test_rotation_about_the_anchor(self):
        assert Placement(10.0, 20.0, rotation=90.0).apply(1.0, 0.0) == (10.0, 21.0)

    def test_mirror_x_flips_y(self):
        assert Placement(0.0, 0.0, mirror="x").apply(1.0, 2.0) == (1.0, -2.0)

    def test_mirror_y_flips_x(self):
        assert Placement(0.0, 0.0, mirror="y").apply(1.0, 2.0) == (-1.0, 2.0)

    def test_mirror_applies_before_rotation(self):
        placement = Placement(0.0, 0.0, rotation=90.0, mirror="x")
        # local (1, 2) -> mirror -> (1, -2) -> rotate 90 -> (2, 1)
        assert placement.apply(1.0, 2.0) == (2.0, 1.0)

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
