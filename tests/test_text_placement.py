"""Where KiCad draws each kind of text, against numbers read from kicad-cli 10.0.1."""

import pytest

from kicad_evaltor.font_metrics import text_bbox, text_extents
from kicad_evaltor.geometry import BBox
from kicad_evaltor.text_placement import (
    DEFAULT_PEN,
    FIELD,
    LINE_PITCH_EM,
    LABEL_SHAPES,
    Pen,
    field_parts,
    ink,
    outline,
    source_kicad_version,
)

SIZE = 1.27


class TestPen:
    def test_default_text_draws_with_the_sheet_line_width(self):
        assert Pen.for_font(SIZE).stroke == DEFAULT_PEN
        assert Pen.for_font(2.54).stroke == DEFAULT_PEN

    def test_default_text_is_laid_out_by_an_eighth_of_its_size(self):
        assert Pen.for_font(2.54).layout == pytest.approx(2.54 / 8)

    def test_bold_draws_with_a_fifth_of_the_size(self):
        assert Pen.for_font(SIZE, bold=True) == Pen(0.254, 0.254)

    def test_thickness_is_used_as_given(self):
        assert Pen.for_font(SIZE, thickness=0.2) == Pen(0.2, 0.2)

    def test_thickness_is_capped_at_a_quarter_of_the_size(self):
        assert Pen.for_font(SIZE, thickness=0.5).stroke == pytest.approx(SIZE / 4)


class TestInk:
    def test_ink_is_as_wide_as_the_letters_plus_a_stroke(self):
        box = ink("GND", SIZE, Pen.for_font(SIZE), "label", None, 0, "left", "bottom")
        x0, _, x1, _ = text_extents("GND", SIZE)
        assert box.width == pytest.approx(x1 - x0 + DEFAULT_PEN)

    def test_a_net_label_is_drawn_clear_of_its_wire(self):
        # Bottom-justified at y = 0: the letters sit above the wire, with a gap.
        box = ink("GND", SIZE, Pen.for_font(SIZE), "label", None, 0, "left", "bottom")
        assert box.max_y < 0.0

    def test_a_label_starts_past_its_anchor(self):
        box = ink("GND", SIZE, Pen.for_font(SIZE), "label", None, 0, "left", "bottom")
        assert box.min_x > 0.0

    def test_a_global_label_starts_past_its_flag(self):
        pen = Pen.for_font(SIZE)
        label = ink("GND", SIZE, pen, "label", None, 0, "left", "center")
        glob = ink("GND", SIZE, pen, "global_label", "input", 0, "left", "center")
        assert glob.min_x > label.min_x + 1.0

    def test_unknown_kind_falls_back_to_the_font_model(self):
        model = text_bbox("X", SIZE, None, "left", "bottom")
        box = ink("X", SIZE, Pen(0.0, 0.0), "sheet_pin", None, 0, "left", "bottom")
        assert box == model

    def test_a_field_lands_where_kicad_draws_it(self):
        # U1's reference on the demo is anchored at (101.2033, 127) and kicad-cli
        # draws it over (101.5406, 126.2333)-(103.6888, 127.6557).
        box = ink("U1", SIZE, Pen.for_font(SIZE), FIELD, None, 0, "left", "center")
        assert (box.min_x, box.min_y, box.max_x, box.max_y) == pytest.approx(
            (0.3373, -0.7667, 2.4855, 0.6557), abs=0.001
        )


class TestFieldParts:
    def test_the_parts_add_back_up_to_the_measured_ink(self):
        pen = Pen.for_font(SIZE)
        parts = field_parts("R1", SIZE, pen, "left", "bottom", 0)
        whole = ink("R1", SIZE, pen, FIELD, None, 0, "left", "bottom")
        x = parts.centre[0] + parts.offset[0]
        y = parts.centre[1] + parts.offset[1]
        assert x + parts.extent.min_x == pytest.approx(whole.min_x)
        assert y + parts.extent.max_y == pytest.approx(whole.max_y)

    def test_the_extent_is_centred(self):
        extent = field_parts("R1", SIZE, Pen.for_font(SIZE), "right", "top", 90).extent
        assert extent.min_x == pytest.approx(-extent.max_x)
        assert extent.min_y == pytest.approx(-extent.max_y)


class TestOutline:
    def test_plain_text_has_no_outline(self):
        assert outline("label", None, "GND", SIZE, Pen.for_font(SIZE)) is None

    @pytest.mark.parametrize("shape", LABEL_SHAPES)
    def test_a_global_label_flag_grows_with_its_text(self, shape):
        pen = Pen.for_font(SIZE)
        short = outline("global_label", shape, "A", SIZE, pen)
        long = outline("global_label", shape, "AAAA", SIZE, pen)
        assert short is not None
        assert long is not None
        assert long.width > short.width

    def test_a_global_label_flag_is_as_tall_as_twice_the_text_plus_a_stroke(self):
        flag = outline("global_label", "input", "GND", SIZE, Pen.for_font(SIZE))
        assert flag is not None
        assert flag.height == pytest.approx(2 * SIZE + DEFAULT_PEN, abs=0.01)

    def test_a_right_justified_flag_extends_left(self):
        flag = outline("global_label", "input", "GND", SIZE, Pen.for_font(SIZE), "right")
        assert flag is not None
        assert flag.max_x == pytest.approx(DEFAULT_PEN / 2, abs=0.01)

    def test_a_hierarchical_flag_is_fixed_whatever_the_text(self):
        pen = Pen.for_font(SIZE)
        short = outline("hierarchical_label", "input", "A", SIZE, pen)
        long = outline("hierarchical_label", "input", "AAAAAAAA", SIZE, pen)
        assert short == long


class TestLines:
    """Multi-line text, as kicad-cli lays it out."""

    @staticmethod
    def _ink(content: str, v: str) -> BBox:
        return ink(content, SIZE, Pen.for_font(SIZE), "text", None, 0, "left", v)

    def test_lines_are_a_fixed_pitch_apart(self):
        one = self._ink("M", "top")
        two = self._ink("M\nM", "top")
        assert two.max_y - one.max_y == pytest.approx(LINE_PITCH_EM * SIZE)

    def test_top_keeps_the_first_line_where_a_single_line_sits(self):
        assert self._ink("M\nM", "top").min_y == pytest.approx(self._ink("M", "top").min_y)

    def test_bottom_keeps_the_last_line_where_a_single_line_sits(self):
        assert self._ink("M\nM", "bottom").max_y == pytest.approx(self._ink("M", "bottom").max_y)

    def test_one_trailing_newline_adds_no_line(self):
        assert self._ink("M\n", "bottom") == self._ink("M", "bottom")

    def test_a_second_trailing_newline_does(self):
        drop = self._ink("M", "bottom").max_y - self._ink("M\n\n", "bottom").max_y
        assert drop == pytest.approx(LINE_PITCH_EM * SIZE)

    def test_the_widest_line_sets_the_width(self):
        widest = self._ink("MMMM", "top").width
        assert self._ink("M\nMMMM", "top").width == pytest.approx(widest, abs=0.001)

    def test_escapes_are_drawn_as_their_characters(self):
        pen = Pen.for_font(SIZE)
        escaped = ink("A{slash}B", SIZE, pen, "label", None, 0, "left", "bottom")
        plain = ink("A/B", SIZE, pen, "label", None, 0, "left", "bottom")
        assert escaped == plain


def test_the_table_records_the_kicad_it_came_from():
    assert source_kicad_version().startswith("10.")
