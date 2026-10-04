import math

import pytest

from kicad_evaltor.font_metrics import line_height, text_extents
from kicad_evaltor.geometry import BBox, Placement
from kicad_evaltor.hierarchy import tree_for
from kicad_evaltor.schematic_file import FileSchematic, load_schematic
from kicad_evaltor.schematic_items import DEFAULT_LINE_WIDTH, SchematicScene, TextItem, extract
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
def tree():
    return tree_for(FileSchematic(DEMO))


@pytest.fixture(scope="module")
def scenes(tree):
    """Every sheet's geometry in the demo, by sheet name.

    The demo is a hierarchy, and a sheet is its own coordinate space, so there is
    no longer one scene for the design: the root draws only sheet blocks, ``Main``
    holds the parts, ``Power`` the power symbols and ``Shared`` the connector.
    """
    return {sheet.name: tree.scene_for(sheet) for sheet in tree.sheets()}


@pytest.fixture(scope="module")
def scene(scenes):
    """The ``Main`` sheet, where the demo keeps the parts these tests read.

    The properties and geometry under test belong to the design, not to one sheet,
    so the assertions are unchanged; only where the design now lives has moved.
    """
    return scenes["Main"]


def _extract(tmp_path, body: str, name: str = "probe.kicad_sch"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return extract(load_schematic(path))


# A body rectangle that declares a stroke, plus one pin on its left. The symbol
# is placed at (50, 50) unrotated, so library-local (x, y) lands at
# (50 + x, 50 - y).
STROKED_BODY = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols
    (symbol "Test:Boxed"
      (property "Reference" "B?")
      (symbol "Boxed_0_1"
        (rectangle (start -1 1) (end 1 -1)
          (stroke (width 0.254) (type default))
          (fill (type background))
        )
        (pin passive line (at -3.81 0 180) (length 2.54) (name P) (number 1))
      )
    )
  )
  (symbol (lib_id "Test:Boxed") (at 50 50)
    (property "Reference" "B1" (at 48 48))
  )
)
"""

# The same body left at KiCad's default width, and a body drawn by a sub-symbol
# that carries its own offset.
DEFAULT_WIDTH_BODY = STROKED_BODY.replace(
    "(width 0.254) (type default)", "(width 0) (type default)"
)
OFFSET_BODY = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols
    (symbol "Test:Stacked"
      (property "Reference" "B?")
      (symbol "Stacked_0_1" (offset 2.54 -1.27)
        (rectangle (start -1 1) (end 1 -1) (stroke (width 0.254) (type default)))
        (pin passive line (at 0 0 180) (length 2.54) (name P) (number 1) (offset 0 1.27))
      )
    )
  )
  (symbol (lib_id "Test:Stacked") (at 50 50)
    (property "Reference" "B1" (at 48 48))
  )
)
"""


class TestSymbolBodyGeometry:
    def test_a_declared_stroke_widens_the_body_box(self, tmp_path):
        # KiCad strokes the path down its centre line, so the ink reaches half a
        # width past it on every side.
        component = _extract(tmp_path, STROKED_BODY).component("B1")
        assert component.body_bbox == BBox(48.873, 48.873, 51.127, 51.127)

    def test_a_zero_stroke_falls_back_to_the_sheet_default(self, tmp_path):
        component = _extract(tmp_path, DEFAULT_WIDTH_BODY).component("B1")
        half = DEFAULT_LINE_WIDTH / 2.0
        assert component.body_bbox == BBox(50 - 1 - half, 50 - 1 - half, 51 + half, 51 + half)

    def test_a_sub_symbol_offset_moves_its_whole_body(self, tmp_path):
        plain = _extract(tmp_path, STROKED_BODY, "plain.kicad_sch").component("B1")
        offset = _extract(tmp_path, OFFSET_BODY, "offset.kicad_sch").component("B1")

        # The sub-symbol offset is in library coordinates, where y grows upwards,
        # so (offset 2.54 -1.27) lands 2.54 right and 1.27 below. The pin's own
        # offset of (0 1.27) pushes it back up by the same amount.
        assert offset.body_bbox.min_x == pytest.approx(plain.body_bbox.min_x + 2.54)
        assert offset.body_bbox.min_y == pytest.approx(plain.body_bbox.min_y + 1.27)
        # The pin sits at local (0, 0) with its own (0 1.27) offset, so the two
        # offsets cancel on the sheet and only the 2.54 of x survives.
        assert offset.pin_connections[0].at == pytest.approx((52.54, 50.0))

    def test_a_pin_offset_shifts_only_that_pin(self, tmp_path):
        plain = _extract(tmp_path, STROKED_BODY, "plain.kicad_sch").component("B1")
        offset = _extract(tmp_path, OFFSET_BODY, "offset.kicad_sch").component("B1")

        assert offset.pin_connections[0].at != plain.pin_connections[0].at
        assert offset.pin_connections[0].number == "1"


class TestPinGeometry:
    def test_a_pin_box_carries_the_drawn_line_width(self, scene):
        u1 = scene.component("U1")
        for box in u1.pin_bboxes:
            assert box.height > 0, "a degenerate pin box can never collide"

    def test_a_pin_can_be_hit_just_off_its_centreline(self, scene):
        u1 = scene.component("U1")
        pin = u1.pin_bboxes[0]
        middle = (pin.min_y + pin.max_y) / 2.0
        sliver = BBox(pin.min_x, middle - 0.02, pin.min_x + 0.01, middle - 0.019)

        assert sliver.intersects(pin)

    def test_connection_points_sit_at_the_far_end_of_their_pin(self, tmp_path):
        component = _extract(tmp_path, STROKED_BODY).component("B1")
        connection = component.pin_connections[0]

        assert connection.at == pytest.approx((46.19, 50.0))
        assert connection.label == "B1.1"
        # The connection point is the outer end of the drawn line, so it is
        # inside the inflated box rather than on its edge.
        assert component.pin_bboxes[0].contains_point(*connection.at)

    def test_the_scene_reports_one_connection_per_pin(self, scenes):
        for sheet_scene in scenes.values():
            for component in sheet_scene.components:
                assert len(component.pin_connections) == len(component.pin_bboxes)


class TestWholeSheet:
    def test_inventory_matches_the_demo_schematic(self, scenes):
        assert {name: len(s.components) for name, s in scenes.items()} == {
            "simple_circuit_test": 0,
            "Power": 19,
            "Main": 14,
            "Shared": 1,
        }
        assert sum(len(s.components) for s in scenes.values()) == 34
        assert sum(len(s.no_connects) for s in scenes.values()) == 37

    def test_the_split_needs_no_wires_for_its_own_connectivity(self, scenes):
        # The 58 wires the flat demo drew all survive, spread over Main and Shared.
        # The 27 extra are the ones the hierarchy needs and the flat design did
        # not: eight joining the sheet pins on the root, and one stub under each of
        # the 19 power symbols so its hierarchical label has somewhere to sit.
        assert {name: len(s.wires) for name, s in scenes.items()} == {
            "simple_circuit_test": 8,
            "Power": 19,
            "Main": 54,
            "Shared": 4,
        }
        assert len(scenes["Main"].wires) + len(scenes["Shared"].wires) == 58

    def test_the_root_sheet_draws_only_the_sheet_links(self, scenes):
        root = scenes["simple_circuit_test"]
        assert root.components == []
        assert root.standalone_texts == []
        assert len(root.junctions) == 4

    def test_texts_split_into_component_fields_and_labels(self, scene):
        local = [t for t in scene.standalone_texts if t.field == "label"]
        assert {t.content for t in local} == {"SDA", "SCL"}

    def test_hierarchical_labels_are_standalone_texts_of_their_own(self, scenes):
        # The net names the split had to add where a power symbol used to be. They
        # are the only standalone text on Power, and 17 of the 19 sit on Main: the
        # other two are J1's, which went to Shared with it.
        assert {t.content for t in scenes["Power"].standalone_texts} == {"GND", "+3.3V"}
        assert len(scenes["Power"].standalone_texts) == 19
        assert (
            len([t for t in scenes["Main"].standalone_texts if t.field == "hierarchical_label"])
            == 17
        )
        assert {t.content for t in scenes["Shared"].standalone_texts} == {"GND", "+3.3V"}

    def test_hidden_fields_are_not_extracted(self, scene):
        u1 = scene.component("U1")
        assert {t.field for t in u1.texts} == {"Reference", "Value"}

    def test_power_symbols_are_flagged_and_carry_no_footprint(self, scenes):
        power = scenes["Power"].power_components
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

    def test_symbol_origin_sits_inside_its_own_bbox(self, scenes):
        for name, sheet_scene in scenes.items():
            for comp in sheet_scene.components:
                box = comp.bbox
                if box is None:
                    continue
                assert box.contains_point(comp.at.x, comp.at.y), f"{name}:{comp.reference}"

    def test_placement_is_carried_through(self, scene):
        assert scene.component("U1").at == Placement(99.06, 88.9, rotation=0.0)

    def test_the_visible_properties_are_carried_through(self, scene):
        r1 = scene.component("R1")
        assert r1.properties["Reference"] == "R1"
        assert r1.properties["Value"] == "10k"
        assert r1.value == "10k"

    def test_hidden_properties_are_not_reported(self, scenes):
        for sheet_scene in scenes.values():
            for comp in sheet_scene.components:
                for text in comp.texts:
                    assert text.field in comp.properties, f"{comp.reference}.{text.field}"

    def test_a_user_defined_property_is_kept_like_any_other(self, tmp_path):
        path = tmp_path / "lcsc.kicad_sch"
        path.write_text(WITH_USER_PROPERTY, encoding="utf-8")
        component = extract(load_schematic(path)).component("C1")
        assert component is not None
        assert component.properties == {
            "Reference": "C1",
            "Value": "100nF",
            "MPN": "CL10B104KB8NNNC",
            "LCSC": "C1525",
        }
        assert component.properties.get("Footprint") is None


# Boxes read back from kicad-cli 10.0.1's SVG export of the demo's Main sheet:
# the ink of each run of text, stroke width included.
KICAD_DRAWN = {
    "U1": (101.5406, 126.2333, 103.6888, 127.6557),
    "SW1": (128.0389, 104.3893, 131.5779, 105.8117),
}


class TestTextGeometry:
    @staticmethod
    def _reference(scene: SchematicScene, ref: str) -> TextItem:
        component = scene.component(ref)
        assert component is not None
        return next(t for t in component.texts if t.field == "Reference")

    def test_a_field_is_boxed_where_kicad_draws_it(self, scene):
        reference = self._reference(scene, "U1")
        assert reference.rotation == 0.0
        box = reference.bbox
        assert (box.min_x, box.min_y, box.max_x, box.max_y) == pytest.approx(
            KICAD_DRAWN["U1"], abs=0.001
        )

    def test_a_box_is_as_tall_as_its_letters_not_the_line(self, scene):
        # Capitals and digits are one em tall, plus the stroke; the line cell
        # KiCad justifies by is 1.17 em and would overstate it.
        reference = self._reference(scene, "U1")
        assert reference.bbox.height == pytest.approx(reference.size + DEFAULT_LINE_WIDTH)
        assert reference.bbox.height < line_height(reference.size)

    def test_rotated_fields_render_flat(self, scene):
        # SW1's reference is stored at 90 degrees on a symbol placed at 270, and
        # the two turns cancel: KiCad draws the text flat.
        reference = self._reference(scene, "SW1")
        assert reference.rotation == 0.0
        box = reference.bbox
        assert (box.min_x, box.min_y, box.max_x, box.max_y) == pytest.approx(
            KICAD_DRAWN["SW1"], abs=0.001
        )

    def test_drawn_width_is_the_ink_plus_a_stroke(self, scenes):
        for sheet_scene in scenes.values():
            for item in sheet_scene.component_texts:
                x0, _, x1, _ = text_extents(item.content, item.size)
                across = item.bbox.width if item.rotation == 0.0 else item.bbox.height
                assert across == pytest.approx(x1 - x0 + DEFAULT_LINE_WIDTH), item.content


class TestWiresAndJunctions:
    def test_wire_thickness_defaults_when_the_file_says_zero(self, scenes):
        assert all(w.width > 0 for s in scenes.values() for w in s.wires)

    def test_wire_bbox_is_the_segment_grown_by_half_the_width(self, scenes):
        wires = [w for s in scenes.values() for w in s.wires]
        for wire in wires:
            half = wire.width / 2
            assert wire.bbox.width == pytest.approx(abs(wire.end[0] - wire.start[0]) + 2 * half)

    def test_junction_points_land_on_the_demo_grid(self, scenes):
        # The demo sheets are laid out on KiCad's 1.27 mm grid.
        for sheet_scene in scenes.values():
            for junction in sheet_scene.junctions:
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

        # A symbol turned 90 degrees counter-clockwise on screen sends a pin
        # that pointed right in the library to one that points up.
        turned = Placement(0.0, 0.0, rotation=90.0)
        assert turned.apply(*end) == pytest.approx((0.0, -2.54), abs=1e-9)

    def test_mirror_reflects_local_geometry_about_the_anchor(self):
        mirrored = Placement(10.0, 20.0, mirror="y")
        assert mirrored.apply(3.0, 4.0) == pytest.approx((7.0, 16.0))

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


# Free-standing text and labels, one visible and one hidden of each kind. A
# symbol field carries `(hide yes)` beside itself; these carry it inside the
# effects block, which is a different place to look.
VISIBLE_AND_HIDDEN_TEXT = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols)
  (text "drawn note" (at 50 50 0) (effects (font (size 1.27 1.27))))
  (text "hidden note" (at 52 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (label "DRAWN" (at 54 50 0) (effects (font (size 1.27 1.27))))
  (label "HIDDEN" (at 56 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (global_label "GDRAWN" (shape input) (at 58 50 0)
    (effects (font (size 1.27 1.27))))
  (global_label "GHIDDEN" (shape input) (at 60 50 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
  (hierarchical_label "HDRAWN" (shape input) (at 62 50 0)
    (effects (font (size 1.27 1.27))))
  (hierarchical_label "HHIDDEN" (shape input) (at 64 50 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
)
"""


class TestHiddenText:
    """Hidden text puts no ink on the sheet, so it is not part of the geometry.

    Every kind here carries ``(hide yes)`` inside its effects block. Reading only
    a direct child would find none of them, and the scene would claim text no
    reader can see.
    """

    @pytest.fixture
    def scene(self, tmp_path):
        path = tmp_path / "hidden_text.kicad_sch"
        path.write_text(VISIBLE_AND_HIDDEN_TEXT, encoding="utf-8")
        return extract(load_schematic(path))

    def test_every_hidden_kind_is_dropped(self, scene):
        contents = {item.content for item in scene.texts}

        assert not {"hidden note", "HIDDEN", "GHIDDEN", "HHIDDEN"} & contents

    def test_every_visible_kind_is_kept(self, scene):
        contents = {item.content for item in scene.texts}

        assert {"drawn note", "DRAWN", "GDRAWN", "HDRAWN"} <= contents

    def test_the_field_keeps_saying_which_kind_it_was(self, scene):
        kinds = {item.field for item in scene.texts}

        assert kinds == {"text", "label", "global_label", "hierarchical_label"}

    def test_hidden_text_is_not_left_as_a_standalone_item(self, scene):
        # standalone_texts is what the off-sheet check walks, so a hidden note
        # parked off the page must not turn into a finding.
        assert all("hidden" not in item.content.lower() for item in scene.standalone_texts)


# Rotated text, with every box checked against kicad-cli 10.0.1's rendering of
# this exact sheet. The probe symbol draws nothing, so only its fields matter.
# Both fields of R1 sit 2.032 mm apart across the symbol, which reads as two
# clear columns when the text is vertical and as an overlap if it were flat.
ROTATED_TEXT = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols
    (symbol "Test:Probe"
      (property "Reference" "R?")
      (property "Value" "?")
      (symbol "Probe_0_1")))
  (symbol (lib_id "Test:Probe") (at 50 50 0) (unit 1)
    (property "Reference" "R1" (at 52.032 50 90) (effects (font (size 1.27 1.27))))
    (property "Value" "10k" (at 50 50 90) (effects (font (size 1.27 1.27)))))
  (symbol (lib_id "Test:Probe") (at 80 50 90) (mirror x) (unit 1)
    (property "Reference" "R2" (at 80 50 0) (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 80 50 0) (effects (font (size 1.27 1.27)) (hide yes))))
  (label "UP" (at 100 50 90) (effects (font (size 1.27 1.27)) (justify left bottom)))
  (label "DOWN" (at 110 50 270) (effects (font (size 1.27 1.27)) (justify right bottom)))
  (label "LEFT" (at 120 50 180) (effects (font (size 1.27 1.27)) (justify right bottom)))
)
"""

ROTATED_DRAWN = {
    "R1": (51.2653, 48.9257, 52.6877, 51.0134),
    "10k": (49.2333, 48.4420, 50.6557, 51.5576),
    "R2": (79.2333, 50.2767, 80.6557, 52.3644),
    "UP": (98.1410, 47.4650, 99.5634, 49.6736),
    "DOWN": (108.1410, 50.3264, 109.5634, 55.2564),
    "LEFT": (115.8926, 48.1410, 119.8550, 49.5634),
}


class TestRotatedText:
    @pytest.fixture
    def scene(self, tmp_path):
        path = tmp_path / "rotated_text.kicad_sch"
        path.write_text(ROTATED_TEXT, encoding="utf-8")
        return extract(load_schematic(path))

    @staticmethod
    def _text(scene: SchematicScene, content: str) -> TextItem:
        return next(item for item in scene.texts if item.content == content)

    @pytest.mark.parametrize("content", sorted(ROTATED_DRAWN))
    def test_each_box_is_where_kicad_draws_it(self, scene, content):
        box = self._text(scene, content).bbox
        assert (box.min_x, box.min_y, box.max_x, box.max_y) == pytest.approx(
            ROTATED_DRAWN[content], abs=0.001
        )

    def test_vertical_fields_get_vertical_boxes(self, scene):
        r1 = scene.component("R1")
        assert r1 is not None
        for item in r1.texts:
            assert item.rotation == 90.0
            assert item.bbox.height > item.bbox.width

    def test_side_by_side_vertical_fields_do_not_overlap(self, scene):
        r1 = scene.component("R1")
        assert r1 is not None
        reference, value = sorted(r1.texts, key=lambda t: t.field)
        assert not reference.bbox.intersects(value.bbox)

    def test_a_field_turns_and_mirrors_with_its_symbol(self, scene):
        # Left-justified flat text on a symbol at 90 degrees runs up from its
        # anchor; mirroring about x then sends it down.
        reference = self._text(scene, "R2")
        assert reference.rotation == 90.0
        assert reference.bbox.min_y > 50.0

    def test_vertical_labels_run_the_way_their_justification_says(self, scene):
        up = self._text(scene, "UP")
        down = self._text(scene, "DOWN")
        assert up.rotation == down.rotation == 90.0
        assert up.bbox.max_y < 50.0 < down.bbox.min_y
        # A label is drawn clear of its wire, which for vertical text is to its
        # left.
        assert up.bbox.max_x < 100.0
        assert down.bbox.max_x < 110.0

    def test_a_label_at_180_reads_flat_and_extends_left(self, scene):
        left = self._text(scene, "LEFT")
        assert left.rotation == 0.0
        assert left.bbox.max_x < 120.0
        assert left.bbox.max_y < 50.0

    def test_a_label_is_anchored_where_it_connects(self, scene):
        assert self._text(scene, "UP").anchor == (100.0, 50.0)
