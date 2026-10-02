"""Tests for stroke font text extents.

The expected values come from ``_stroke_font_data.json``, which was measured by
rendering probe text through the installed KiCad. These tests check the derived
behaviour (composition, justification, rotation) rather than re-deriving the
measurements themselves.
"""

import math

import pytest

from kicad_evaltor.font_metrics import (
    advance_width,
    glyph,
    line_height,
    source_kicad_version,
    supported,
    text_bbox,
    text_box_width,
    text_extents,
)
from kicad_evaltor.geometry import Placement

SIZE = 2.54


class TestSourceData:
    def test_reports_which_kicad_it_came_from(self):
        assert source_kicad_version().startswith("10.")

    def test_covers_printable_ascii(self):
        for code in range(0x20, 0x7F):
            assert supported(chr(code)), chr(code)

    def test_space_has_no_ink_but_does_advance(self):
        space = glyph(" ")
        assert space.advance > 0
        assert not space.has_ink

    def test_capital_m_is_one_em_tall(self):
        m = glyph("M")
        assert m.y_top - m.y_bottom == pytest.approx(1.0, abs=0.01)

    def test_advances_land_on_the_twenty_one_unit_grid(self):
        for code in range(0x21, 0x7F):
            units = glyph(chr(code)).advance * 21.0
            assert units == pytest.approx(round(units), abs=0.02), chr(code)

    def test_unknown_character_gets_a_generous_fallback(self):
        assert not supported("\u00b5")
        fallback = glyph("\u00b5")
        assert fallback.advance > 0
        assert fallback.has_ink


class TestExtents:
    def test_single_character_matches_its_glyph(self):
        x0, y_top, x1, y_bottom = text_extents("M", SIZE)
        m = glyph("M")

        assert x0 == pytest.approx(m.x0 * SIZE)
        assert x1 == pytest.approx(m.x1 * SIZE)
        # Vertical values are measured from the baseline, so the glyph's own
        # height survives the baseline shift intact.
        assert y_top - y_bottom == pytest.approx((m.y_top - m.y_bottom) * SIZE)

    def test_ink_grows_with_the_number_of_characters(self):
        one = text_extents("M", SIZE)
        two = text_extents("MM", SIZE)

        assert two[2] - two[0] > one[2] - one[0]
        assert two[0] == pytest.approx(one[0])

    def test_second_character_offsets_by_one_advance(self):
        single = text_extents("i", SIZE)
        doubled = text_extents("ii", SIZE)

        # The left edge stays put; the right edge moves out by one advance.
        assert doubled[0] == pytest.approx(single[0])
        assert doubled[2] - single[2] == pytest.approx(glyph("i").advance * SIZE)

    def test_leading_space_shifts_ink_right(self):
        plain = text_extents("M", SIZE)
        spaced = text_extents(" M", SIZE)

        assert spaced[0] - plain[0] == pytest.approx(glyph(" ").advance * SIZE)

    def test_descender_extends_below_the_baseline(self):
        assert text_extents("g", SIZE)[3] < 0

    def test_whitespace_only_reports_the_advance(self):
        _x0, y_top, x1, y_bottom = text_extents("   ", SIZE)

        assert (y_top, y_bottom) == (0.0, 0.0)
        assert x1 == pytest.approx(3 * glyph(" ").advance * SIZE)

    def test_empty_string_has_no_extent(self):
        assert text_extents("", SIZE) == (0.0, 0.0, 0.0, 0.0)

    def test_extents_scale_linearly_with_size(self):
        single = text_bbox("MM", SIZE)
        double = text_bbox("MM", SIZE * 2)

        assert double.width == pytest.approx(single.width * 2)
        assert double.height == pytest.approx(single.height * 2)

    def test_wider_glyphs_widen_the_ink(self):
        assert (text_extents("W", SIZE)[2] - text_extents("W", SIZE)[0]) > (
            text_extents("i", SIZE)[2] - text_extents("i", SIZE)[0]
        )


class TestAdvanceAndBoxWidth:
    def test_advance_width_is_the_sum_of_glyph_advances(self):
        expected = (glyph("M").advance + glyph("g").advance) * SIZE
        assert advance_width("Mg", SIZE) == pytest.approx(expected)

    def test_box_width_exceeds_the_advance_by_the_stroke(self):
        extra = text_box_width("M", SIZE) - advance_width("M", SIZE)
        assert extra == pytest.approx(0.2, abs=0.01)

    def test_line_height_is_constant_in_em(self):
        assert line_height(SIZE) == pytest.approx(1.17 * SIZE)


class TestJustification:
    def test_left_bottom_puts_the_baseline_at_the_anchor(self):
        box = text_bbox("M", SIZE, Placement(10.0, 20.0), "left", "bottom")
        extents = text_extents("M", SIZE)

        # "M" has no descender, so its baseline-relative bottom sits on zero and
        # the box lands exactly on the anchor line.
        assert box.min_x == pytest.approx(10.0 + extents[0])
        assert box.max_y == pytest.approx(20.0)

    def test_right_justification_renders_like_left(self):
        # KiCad pins right-justified fields at the left edge of the text cell:
        # measured against KiCad 10.0.1's renderer, Y1's right-justified
        # reference and value carry the same left padding as left-justified
        # fields.
        left = text_bbox("Mg", SIZE, Placement(0.0, 0.0), "left", "bottom")
        right = text_bbox("Mg", SIZE, Placement(0.0, 0.0), "right", "bottom")

        assert right == left

    def test_top_pins_the_glyph_top_on_the_anchor(self):
        bottom = text_bbox("M", SIZE, Placement(0.0, 0.0), "left", "bottom")
        top = text_bbox("M", SIZE, Placement(0.0, 0.0), "left", "top")

        assert top.min_y == pytest.approx(0.0)
        assert bottom.max_y == pytest.approx(0.0)
        assert top.max_y == pytest.approx(text_extents("M", SIZE)[1])

    def test_default_centres_the_box_on_the_anchor(self):
        box = text_bbox("Mg", SIZE, Placement(0.0, 0.0))

        # Centring aligns the text box, not the ink, so the ink centre can sit
        # a hair off zero where the glyph bearings are asymmetric.
        assert box.center[0] == pytest.approx(0.0, abs=1e-3)

    def test_explicit_center_matches_the_default(self):
        default = text_bbox("Mg", SIZE, Placement(5.0, 5.0))
        centered = text_bbox("Mg", SIZE, Placement(5.0, 5.0), "center", "middle")

        assert default == centered

    def test_rejects_unknown_horizontal_justification(self):
        with pytest.raises(ValueError, match="horizontal"):
            text_bbox("M", SIZE, justify_h="sideways")

    def test_rejects_unknown_vertical_justification(self):
        with pytest.raises(ValueError, match="vertical"):
            text_bbox("M", SIZE, justify_v="upways")

    def test_rejects_non_positive_size(self):
        with pytest.raises(ValueError, match="size"):
            text_bbox("M", 0.0)


class TestPlacementInteraction:
    def test_translation_only(self):
        origin = text_bbox("U1", SIZE, Placement(0.0, 0.0), "left", "bottom")
        moved = text_bbox("U1", SIZE, Placement(50.0, 60.0), "left", "bottom")

        assert moved.min_x == pytest.approx(origin.min_x + 50.0)
        assert moved.min_y == pytest.approx(origin.min_y + 60.0)

    def test_rotation_swaps_the_box_dimensions_for_upright_text(self):
        upright = text_bbox("MMMM", SIZE, Placement(0.0, 0.0), "left", "bottom")
        turned = text_bbox("MMMM", SIZE, Placement(0.0, 0.0, rotation=90.0), "left", "bottom")

        assert turned.width == pytest.approx(upright.height)
        assert turned.height == pytest.approx(upright.width)

    def test_rotation_keeps_the_same_area(self):
        upright = text_bbox("R1", SIZE, Placement(10.0, 10.0), "left", "bottom")
        turned = text_bbox("R1", SIZE, Placement(10.0, 10.0, rotation=270.0), "left", "bottom")

        assert turned.area == pytest.approx(upright.area)

    def test_mirror_x_reflects_about_the_anchor(self):
        anchor_y = 100.0
        plain = text_bbox("Mg", SIZE, Placement(100.0, anchor_y), "left", "bottom")
        mirrored = text_bbox("Mg", SIZE, Placement(100.0, anchor_y, mirror="x"), "left", "bottom")

        # Reflection is about the anchor's horizontal line, not about zero.
        assert mirrored.center[1] == pytest.approx(2 * anchor_y - plain.center[1])
        assert mirrored.center[0] == pytest.approx(plain.center[0])

    def test_ink_stays_above_the_anchor_for_bottom_justification(self):
        box = text_bbox("U1", SIZE, Placement(0.0, 0.0), "left", "bottom")
        # Sheet y grows downward, so ink above the anchor has a negative min_y.
        assert box.min_y < 0
        assert box.max_y == pytest.approx(0.0)


class TestAgainstMeasuredProbe:
    """Values read straight off the calibration sheet's rendered SVG."""

    def test_mg_ink_matches_the_rendered_run(self):
        # "Mg" at size 2.54, left/bottom justified, measured in the calibration.
        box = text_bbox("Mg", SIZE, Placement(0.0, 0.0), "left", "bottom")

        assert box.min_x == pytest.approx(0.2775 * SIZE, abs=0.01)
        assert box.max_x == pytest.approx(1.8490 * SIZE, abs=0.01)
        # "bottom" pins the baseline on the anchor, so the descender of "g" is
        # the only ink beyond it.
        assert box.max_y == pytest.approx(0.0)
        assert box.min_y == pytest.approx(-1.3333 * SIZE, abs=0.01)

    def test_mg_right_justified_matches_the_rendered_run(self):
        box = text_bbox("Mg", SIZE, Placement(0.0, 0.0), "right", "bottom")

        assert box.min_x == pytest.approx(0.2775 * SIZE, abs=0.01)
        assert box.max_x == pytest.approx(1.8490 * SIZE, abs=0.01)

    def test_mg_default_is_centred_like_the_rendered_run(self):
        box = text_bbox("Mg", SIZE, Placement(0.0, 0.0))

        assert box.min_x == pytest.approx(-0.7857 * SIZE, abs=0.01)
        assert box.max_x == pytest.approx(0.7857 * SIZE, abs=0.01)

    def test_m_left_top_matches_the_rendered_run(self):
        box = text_bbox("M", SIZE, Placement(0.0, 0.0), "left", "top")

        # "top" pins the glyph's top edge on the anchor.
        assert box.min_y == pytest.approx(0.0)
        assert box.max_y == pytest.approx(1.0000 * SIZE, abs=0.01)

    def test_line_height_matches_the_rendered_shift(self):
        bottom = text_bbox("M", SIZE, Placement(0.0, 0.0), "left", "bottom")
        top = text_bbox("M", SIZE, Placement(0.0, 0.0), "left", "top")

        assert math.isclose(top.max_y - bottom.min_y, 2.0 * SIZE, rel_tol=0.02)
