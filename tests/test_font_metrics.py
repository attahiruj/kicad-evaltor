"""Tests for stroke font text extents.

The expected values come from ``_stroke_font_data.json``, which was measured by
rendering probe text through the installed KiCad. These tests check the derived
behaviour (composition, justification, anchoring) rather than re-deriving the
measurements themselves.
"""

import pytest

from kicad_evaltor.font_metrics import (
    advance_width,
    descent_em,
    glyph,
    line_height,
    source_kicad_version,
    supported,
    text_bbox,
    text_box_width,
    text_cell_bbox,
    unescape,
    text_extents,
)

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
    def test_left_pins_the_cell_left_edge_and_right_its_right_edge(self):
        left = text_cell_bbox("Mg", SIZE, (10.0, 20.0), "left", "bottom")
        right = text_cell_bbox("Mg", SIZE, (10.0, 20.0), "right", "bottom")

        assert left.min_x == pytest.approx(10.0)
        assert right.max_x == pytest.approx(10.0)
        assert right.width == pytest.approx(left.width)

    def test_bottom_pins_the_cell_bottom_on_the_anchor(self):
        cell = text_cell_bbox("M", SIZE, (10.0, 20.0), "left", "bottom")

        assert cell.max_y == pytest.approx(20.0)
        assert cell.min_y == pytest.approx(20.0 - line_height(SIZE))

    def test_top_pins_the_cell_top_on_the_anchor(self):
        cell = text_cell_bbox("M", SIZE, (0.0, 0.0), "left", "top")

        assert cell.min_y == pytest.approx(0.0)
        assert cell.max_y == pytest.approx(line_height(SIZE))

    def test_default_centres_the_cell_on_the_anchor(self):
        cell = text_cell_bbox("Mg", SIZE, (5.0, 5.0))

        assert cell.center[0] == pytest.approx(5.0)
        assert cell.center[1] == pytest.approx(5.0)

    def test_explicit_center_matches_the_default(self):
        default = text_bbox("Mg", SIZE, (5.0, 5.0))
        centered = text_bbox("Mg", SIZE, (5.0, 5.0), "center", "middle")

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


class TestAnchorInteraction:
    def test_the_anchor_is_a_pure_translation(self):
        origin = text_bbox("U1", SIZE, (0.0, 0.0), "left", "bottom")
        moved = text_bbox("U1", SIZE, (50.0, 60.0), "left", "bottom")

        assert moved.min_x == pytest.approx(origin.min_x + 50.0)
        assert moved.min_y == pytest.approx(origin.min_y + 60.0)

    def test_the_anchor_does_not_resize_the_box(self):
        origin = text_bbox("MMMM", SIZE, (0.0, 0.0), "left", "bottom")
        moved = text_bbox("MMMM", SIZE, (25.0, 40.0), "left", "bottom")

        # KiCad draws field text flat at the position the field stores, so the
        # stored angle never reaches the glyphs.
        assert moved.width == pytest.approx(origin.width)
        assert moved.height == pytest.approx(origin.height)

    def test_ink_stays_above_the_cell_bottom(self):
        box = text_bbox("U1", SIZE, (0.0, 0.0), "left", "bottom")

        # Sheet y grows downward, so ink above the anchor has a negative min_y,
        # and bottom justification leaves the cell's descent room below the
        # baseline so the ink stops short of the anchor.
        assert box.min_y < 0
        assert box.max_y == pytest.approx(-descent_em() * SIZE)


class TestLineCell:
    """The cell is what KiCad reserves, so it sets every collision box."""

    def test_cell_is_one_stroke_thickness_wider_than_the_advances(self):
        cell = text_cell_bbox("Mg", SIZE, justify_h="left")

        assert cell.width == pytest.approx(advance_width("Mg", SIZE) + 0.2, abs=0.01)

    def test_cell_is_one_line_height_tall(self):
        cell = text_cell_bbox("Mg", SIZE)

        assert cell.height == pytest.approx(line_height(SIZE))

    def test_ink_is_inside_the_cell_across_its_width(self):
        # KiCad's line cell is 1.17 em tall, less than the 1.333 em a cap-to-
        # descender run needs, so ink overhangs the top and bottom edges. It
        # never does so sideways: the cell carries the advances plus a stroke.
        for justify_h in ("left", "center"):
            for justify_v in ("top", "middle", "bottom"):
                cell = text_cell_bbox("Mg", SIZE, (7.0, 11.0), justify_h, justify_v)
                ink = text_bbox("Mg", SIZE, (7.0, 11.0), justify_h, justify_v)

                assert ink.min_x >= cell.min_x, (justify_h, justify_v)
                assert ink.max_x <= cell.max_x, (justify_h, justify_v)

    def test_top_and_bottom_shift_ink_by_the_line_height(self):
        bottom = text_bbox("M", SIZE, (0.0, 0.0), "left", "bottom")
        top = text_bbox("M", SIZE, (0.0, 0.0), "left", "top")

        # This is the shift the calibration measured between the two probes. It
        # is a line height rather than a glyph height, which is how the cell shows
        # itself: a top-justified run hangs below its anchor, a bottom-justified
        # one above it.
        assert top.min_y - bottom.min_y == pytest.approx(line_height(SIZE))


class TestAgainstMeasuredProbe:
    """Values read straight off the calibration sheet's rendered SVG.

    Em units with y growing upward, from the ``justification`` block of
    ``_stroke_font_data.json``.
    """

    def test_left_bottom_ink_matches_the_rendered_run(self):
        # "MMg" at size 2.54, left/bottom justified.
        box = text_bbox("MMg", SIZE, (0.0, 0.0), "left", "bottom")

        assert box.min_x == pytest.approx(0.277520 * SIZE, abs=1e-3)
        assert box.max_x == pytest.approx(2.991850 * SIZE, abs=1e-3)
        assert box.min_y == pytest.approx(-1.223976 * SIZE, abs=1e-3)
        assert box.max_y == pytest.approx(0.109370 * SIZE, abs=1e-3)

    def test_default_ink_matches_the_rendered_run(self):
        # "Mg" at size 2.54 with no justification, centred on its anchor.
        box = text_bbox("Mg", SIZE, (0.0, 0.0))

        assert box.min_x == pytest.approx(-0.785748 * SIZE, abs=1e-3)
        assert box.max_x == pytest.approx(0.785709 * SIZE, abs=1e-3)
        assert box.min_y == pytest.approx(-0.638976 * SIZE, abs=1e-3)
        assert box.max_y == pytest.approx(0.694370 * SIZE, abs=1e-3)

    def test_the_descender_is_what_moves_an_unjustified_cell(self):
        # Centred text is justified by its cell, so a string with a descender
        # has its baseline below the anchor: "Mg" puts its baseline 0.361 em
        # under, against "M" alone at 0.285 em.
        assert text_bbox("Mg", SIZE).max_y - text_bbox("M", SIZE).max_y == pytest.approx(
            0.333346 * SIZE, abs=1e-3
        )


class TestEscapes:
    def test_a_slash_in_a_net_name_is_drawn_as_a_slash(self):
        assert unescape("PB9{slash}PC14") == "PB9/PC14"

    def test_every_escape_kicad_writes_is_undone(self):
        assert unescape("{dblquote}{lt}{gt}{backslash}{colon}{comma}{brace}") == '"<>\\:,{'

    def test_an_unknown_name_is_drawn_literally(self):
        assert unescape("A{nope}B") == "A{nope}B"


class TestMarkup:
    """Read back from kicad-cli: scripts at four fifths, an overbar above the caps."""

    def test_a_subscript_advances_four_fifths_as_far(self):
        plain = advance_width("M", SIZE)
        assert advance_width("M_{M}", SIZE) == pytest.approx(plain * 1.8)

    def test_a_subscript_drops_below_the_baseline(self):
        _, _, _, bottom = text_extents("_{M}", SIZE)
        assert bottom == pytest.approx(-0.1105 * SIZE)

    def test_a_superscript_rises_above_the_capitals(self):
        _, top, _, _ = text_extents("^{M}", SIZE)
        assert top == pytest.approx((0.8 + 0.2895) * SIZE)

    def test_an_overbar_sits_above_the_capitals_and_spans_the_run(self):
        x0, top, x1, _ = text_extents("~{RESET}", SIZE)
        assert top == pytest.approx(1.2775 * SIZE)
        assert x1 - x0 > text_extents("RESET", SIZE)[2] - text_extents("RESET", SIZE)[0]

    def test_markup_braces_take_no_room(self):
        assert advance_width("~{AB}", SIZE) == pytest.approx(advance_width("AB", SIZE))

    def test_an_unclosed_brace_is_drawn_as_written(self):
        assert advance_width("~{AB", SIZE) == pytest.approx(
            sum(glyph(c).advance for c in "~{AB") * SIZE
        )
