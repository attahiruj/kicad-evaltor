import math

import pytest

from kicad_evaltor.font_metrics import advance_width, text_bbox
from kicad_evaltor.geometry import Placement
from kicad_evaltor.schematic_file import load_schematic
from kicad_evaltor.schematic_items import extract
from conftest import demo_schematic_path

DEMO = demo_schematic_path()

# A capacitor with the properties a real library part has: two standard ones,
# two user-defined, and a hidden footprint.
WITH_USER_PROPERTY = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols
    (symbol "Device:C"
      (property "Reference" "C?")
      (symbol "C_0_1" (rectangle (start -1 1) (end 1 -1)))
    )
  )
  (symbol (lib_id "Device:C") (at 50 50)
    (property "Reference" "C1" (at 48 48))
    (property "Value" "100nF" (at 48 52))
    (property "Footprint" "Capacitor_SMD:C_0603" (at 48 56) (hide yes))
    (property "MPN" "CL10B104KB8NNNC" (at 48 60))
    (property "LCSC" "C1525" (at 48 64))
  )
)
"""


@pytest.fixture(scope="module")
def scene():
    return extract(load_schematic(DEMO))


class TestWholeSheet:
    def test_inventory_matches_the_demo_schematic(self, scene):
        assert len(scene.components) == 34
        assert len(scene.wires) == 58
        assert len(scene.junctions) == 10

    def test_texts_split_into_component_fields_and_labels(self, scene):
        assert len(scene.standalone_texts) == 4
        assert {t.content for t in scene.standalone_texts} == {"SDA", "SCL"}

    def test_hidden_fields_are_not_extracted(self, scene):
        u1 = scene.component("U1")
        assert {t.field for t in u1.texts} == {"Reference", "Value"}

    def test_power_symbols_are_flagged_and_carry_no_footprint(self, scene):
        power = scene.power_components
        assert len(power) == 19
        assert all(c.lib_id.startswith("power:") for c in power)


class TestComponents:
    def test_lookup_by_reference(self, scene):
        assert scene.component("R1") is not None
        assert scene.component("nope") is None

    def test_body_and_pins_are_recovered_from_lib_symbols(self, scene):
        u1 = scene.component("U1")
        assert u1.body_bbox is not None
        assert len(u1.pin_bboxes) == 33

    def test_component_bbox_encloses_body_and_pins(self, scene):
        u1 = scene.component("U1")
        box = u1.bbox
        assert box is not None
        assert box.width >= u1.body_bbox.width
        assert box.height >= u1.body_bbox.height

    def test_symbol_origin_sits_inside_its_own_bbox(self, scene):
        for comp in scene.components:
            box = comp.bbox
            if box is None:
                continue
            assert box.contains_point(comp.at.x, comp.at.y), comp.reference

    def test_placement_is_carried_through(self, scene):
        assert scene.component("U1").at == Placement(99.06, 88.9, rotation=0.0)

    def test_the_visible_properties_are_carried_through(self, scene):
        r1 = scene.component("R1")
        assert r1.properties["Reference"] == "R1"
        assert r1.properties["Value"] == "10k"
        assert r1.value == "10k"

    def test_hidden_properties_are_not_reported(self, scene):
        for comp in scene.components:
            for text in comp.texts:
                assert text.field in comp.properties, f"{comp.reference}.{text.field}"

    def test_a_user_defined_property_is_kept_like_any_other(self, tmp_path):
        path = tmp_path / "lcsc.kicad_sch"
        path.write_text(WITH_USER_PROPERTY, encoding="utf-8")
        component = extract(load_schematic(path)).component("C1")
        assert component.properties == {
            "Reference": "C1",
            "Value": "100nF",
            "MPN": "CL10B104KB8NNNC",
            "LCSC": "C1525",
        }
        assert component.properties.get("Footprint") is None


class TestTextGeometry:
    def test_cap_height_text_is_exactly_the_font_size_tall(self, scene):
        u1 = scene.component("U1")
        reference = next(t for t in u1.texts if t.field == "Reference")
        assert reference.rotation == 0.0
        assert reference.bbox.height == pytest.approx(reference.size)

    def test_text_with_a_descender_is_taller_than_the_font_size(self, scene):
        u1 = scene.component("U1")
        value = next(t for t in u1.texts if t.field == "Value")
        assert "g" in value.content
        assert value.bbox.height > value.size

    def test_rotated_fields_render_flat(self, scene):
        # SW1's reference is stored at 90 degrees on a symbol placed at 270, but
        # KiCad draws the text flat.
        sw1 = scene.component("SW1")
        reference = next(t for t in sw1.texts if t.field == "Reference")
        assert reference.rotation == 0.0
        flat = text_bbox(reference.content, reference.size, Placement(0.0, 0.0))
        assert reference.bbox.width == pytest.approx(flat.width)
        assert reference.bbox.height == pytest.approx(flat.height)

    def test_ink_is_never_wider_than_the_advance_width(self, scene):
        for item in scene.component_texts:
            assert item.bbox.width <= advance_width(item.content, item.size) + 1e-9


class TestWiresAndJunctions:
    def test_wire_thickness_defaults_when_the_file_says_zero(self, scene):
        assert all(w.width > 0 for w in scene.wires)

    def test_wire_bbox_is_the_segment_grown_by_half_the_width(self, scene):
        wire = scene.wires[0]
        half = wire.width / 2
        assert wire.bbox.width == pytest.approx(abs(wire.end[0] - wire.start[0]) + 2 * half)

    def test_junction_points_land_on_the_demo_grid(self, scene):
        # The demo sheet is laid out on KiCad's 1.27 mm grid.
        for junction in scene.junctions:
            x, y = junction.at
            assert x == pytest.approx(round(x / 1.27) * 1.27, abs=1e-6)
            assert y == pytest.approx(round(y / 1.27) * 1.27, abs=1e-6)


class TestPlacementTransforms:
    def test_identical_symbols_offset_with_their_origins(self, scene):
        # Two instances of the same library body must differ by exactly the
        # offset between their placement origins.
        resistors = [c for c in scene.components if c.lib_id == "Device:R"]
        first, second = resistors[0], resistors[1]
        assert (second.body_bbox.min_x - first.body_bbox.min_x) == pytest.approx(
            second.at.x - first.at.x
        )
        assert (second.body_bbox.min_y - first.body_bbox.min_y) == pytest.approx(
            second.at.y - first.at.y
        )

    def test_quarter_turn_rotates_the_pin_direction(self):
        from kicad_evaltor.schematic_items import SymbolPin

        pin = SymbolPin(name="1", number="1", at=(0.0, 0.0), angle=0.0, length=2.54)
        _start, end = pin.segment()
        assert end == pytest.approx((2.54, 0.0))

        turned = Placement(0.0, 0.0, rotation=90.0)
        assert turned.apply(*end) == pytest.approx((0.0, 2.54), abs=1e-9)

    def test_mirror_reflects_local_geometry_about_the_anchor(self):
        mirrored = Placement(10.0, 20.0, mirror="y")
        assert mirrored.apply(3.0, 4.0) == pytest.approx((7.0, 24.0))

    def test_apply_box_is_conservative_off_axis(self):
        box = Placement(0.0, 0.0, rotation=45.0).apply_box(_box(0.0, 0.0, 10.0, 0.0))
        # A 10 mm horizontal segment turned 45 degrees spans 10/sqrt(2) on each
        # axis, so the axis-aligned box is the bounding box of the corners.
        assert box.width == pytest.approx(10.0 / math.sqrt(2), abs=1e-9)
        assert box.height == pytest.approx(10.0 / math.sqrt(2), abs=1e-9)


def _box(min_x, min_y, max_x, max_y):
    from kicad_evaltor.geometry import BBox

    return BBox(min_x, min_y, max_x, max_y)


class TestDegenerateInput:
    def test_text_with_no_value_is_dropped(self):
        from kicad_evaltor.schematic_items import _text_item

        assert _text_item("", 1.27, Placement(0, 0), "center", "center", "R1", "Value") is None

    def test_symptomless_pins_get_the_kicad_default_length(self):
        from kicad_evaltor.sexpr import parse
        from kicad_evaltor.schematic_items import _symbol_pins

        sub = parse("(symbol (pin passive line (at 0 0 180) (name P) (number 1)))")
        pins = _symbol_pins(sub)
        assert len(pins) == 1
        assert pins[0].length == 2.54
        assert pins[0].angle == 180.0
