"""Independent vector examples and KiCad-rendered regressions for ink isolation."""

import hashlib

import pytest

from kicad_evaltor import DesignContext, InkRenderer, InkUnavailable, ink_distance, svg_ink
from kicad_evaltor.checks.schematic.overlaps import TextSymbolOverlapCheck
from kicad_evaltor.sexpr import dumps
from kicad_evaltor.symbol_library import SymbolLibraries


def drawing(body):
    return f'<svg xmlns="http://www.w3.org/2000/svg"><g style="fill:none;stroke:black;stroke-width:0.1">{body}</g></svg>'


def test_hollow_filled_and_white_background_are_distinct():
    text = svg_ink(drawing('<path d="M 4 5 L 6 5"/>'))
    empty = svg_ink(drawing('<rect x="0" y="0" width="10" height="10"/>'))
    filled = svg_ink(drawing('<rect x="0" y="0" width="10" height="10" fill="black"/>'))
    white = svg_ink(drawing('<rect x="0" y="0" width="10" height="10" fill="white"/>'))
    assert not ink_distance(text, empty).intersects
    assert ink_distance(text, empty).distance_mm == pytest.approx(3.9)
    assert ink_distance(text, filled).intersects
    assert not ink_distance(text, white).intersects


def test_empty_polyline_corner_does_not_become_drawn_material():
    ground = svg_ink(drawing('<path d="M 0 0 L 0 2 L -1 2 L 0 3 L 1 2 L 0 2"/>'))
    beside = svg_ink(drawing('<path d="M .8 .5 L 2 .5"/>'))
    crossing = svg_ink(drawing('<path d="M -1 1 L 1 1"/>'))
    assert not ink_distance(ground, beside).intersects
    assert ink_distance(ground, crossing).intersects


def test_svg_rotations_and_implicit_moveto_lines():
    ink = svg_ink(drawing('<g transform="translate(10 20) rotate(90)"><path d="M 0,0 2,0"/></g>'))
    assert ink[0].points[0] == pytest.approx((10, 20))
    assert ink[0].points[1] == pytest.approx((10, 22))
    with pytest.raises(InkUnavailable, match="Unsupported"):
        svg_ink(drawing('<use href="#unknown"/>'))


def test_curve_sampling_reports_drawn_circle_and_cubic():
    circle = svg_ink(drawing('<circle cx="0" cy="0" r="10"/>'))
    middle = svg_ink(drawing('<path d="M -1 0 L 1 0"/>'))
    cubic = svg_ink(drawing('<path d="M 0 0 C 0 5 5 5 5 0"/>'))
    assert not ink_distance(circle, middle).intersects
    assert len(cubic[0].points) > 4


@pytest.mark.parametrize(
    ("clearance", "expected"), [(0, "error"), (0.0004, "error"), (0.003, "fail")]
)
def test_approximated_curve_contact_is_not_a_definite_collision(
    monkeypatch, tmp_path, clearance, expected
):
    from kicad_evaltor import TextTextOverlapCheck

    # At x=0 the symmetric cubic is at y=0.000675. Its 0.0001-wide
    # stroke is separated from the probe's equally wide stroke by 0.0005 mm.
    # Default subdivision flattens it to y=0, producing a false intersection.
    drawings = {
        "curve": '<path stroke-width="0.0001" d="M -1 0 C -0.3333333333333333 .0009 .3333333333333333 .0009 1 0"/>',
        "probe": '<path stroke-width="0.0001" d="M -.000001 .000075 L .000001 .000075"/>',
    }

    def object_ink(renderer, *, uuid, **kwargs):
        return svg_ink(drawing(drawings[uuid]), resolution_mm=renderer.resolution_mm)

    monkeypatch.setattr(InkRenderer, "object_ink", object_ink)
    path = tmp_path / "uncertain.kicad_sch"
    path.write_text(
        """(kicad_sch (uuid "root") (paper "A4") (lib_symbols)
        (text "A" (at 0 0 0) (effects (font (size 1 1))) (uuid "curve"))
        (text "B" (at 0 0 0) (effects (font (size 1 1))) (uuid "probe")))""",
        encoding="utf-8",
    )
    coarse = ink_distance(
        *(svg_ink(drawing(body)) for body in drawings.values()), error_bound_mm=0.002
    )
    assert coarse.intersects
    assert 0 < coarse.overlap_depth_mm < coarse.error_bound_mm
    with DesignContext(schematic_path=path) as ctx:
        result = TextTextOverlapCheck(method="render", clearance=clearance).run(ctx)
        assert result.status.value == expected
        if expected == "error":
            assert result.details["reason_code"] == "ink_unavailable"
            assert "uncertainty" in result.message
        refined = TextTextOverlapCheck(
            method="render", clearance=clearance, resolution_mm=0.000001
        ).run(ctx)
        assert refined.status.value == ("fail" if clearance > 0.0005 else "pass")


def test_ink_distance_keeps_looking_for_a_definite_intersection():
    from kicad_evaltor.ink import InkShape

    # The first shape pair only barely touches; a later pair has a reliable
    # crossing. A shallow witness must not mask a stronger one.
    first = (
        InkShape(((0, 0), (1, 0)), 0.1),
        InkShape(((2, 0), (3, 0)), 0.1),
    )
    second = (
        InkShape(((0, 0.0995), (1, 0.0995)), 0.1),
        InkShape(((2.5, -1), (2.5, 1)), 0.1),
    )
    measured = ink_distance(first, second, error_bound_mm=0.002)
    assert measured.intersects
    assert measured.overlap_depth_mm > measured.error_bound_mm


def resistor_sheet(cli, value_at, label_at=None):
    definition = dumps(SymbolLibraries(cli_path=cli).embeddable("Device:R"))
    label = (
        ""
        if label_at is None
        else f'(label "SIG" (at {label_at[0]} {label_at[1]} 0) (effects (font (size 1.27 1.27)) (justify left bottom)) (uuid "aaaa0000-1111-2222-3333-000000000001"))'
    )
    return f"""(kicad_sch (version 20250114) (generator "eeschema")
      (uuid "11111111-2222-3333-4444-555555555555") (paper "A4")
      (lib_symbols {definition}) {label}
      (symbol (lib_id "Device:R") (at 100 100 0) (unit 1)
       (uuid "bbbb0000-1111-2222-3333-000000000001")
       (property "Reference" "R1" (at 110 90 0) (effects (font (size 1.27 1.27)) (justify left)))
       (property "Value" "10k" (at {value_at[0]} {value_at[1]} 0) (effects (font (size 1.27 1.27)) (justify left)))
       (instances (project "ink" (path "/11111111-2222-3333-4444-555555555555" (reference "R1") (unit 1)))))
      (sheet_instances (path "/" (page "1"))))"""


@pytest.mark.integration
@pytest.mark.parametrize(
    ("value_at", "label_at", "overlaps"),
    [
        ((103, 100), None, False),
        ((101.35, 100), None, False),
        ((98, 100.4), None, True),
        ((99.4, 103.4), None, True),
        ((103, 106), (103, 100.6), False),
        ((103, 106), (98, 100.6), True),
    ],
)
def test_six_lora_ink_regressions(kicad_cli_required, tmp_path, value_at, label_at, overlaps):
    path = tmp_path / "ink.kicad_sch"
    path.write_text(resistor_sheet(str(kicad_cli_required), value_at, label_at), encoding="utf-8")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    with DesignContext(schematic_path=path, kicad_cli_path=str(kicad_cli_required)) as ctx:
        renderer = InkRenderer(ctx.schematic)
        text = (
            renderer.object_ink(reference="R1", field="Value")
            if label_at is None
            else renderer.object_ink(uuid="aaaa0000-1111-2222-3333-000000000001")
        )
        body = renderer.object_ink(reference="R1", include_pins=True)
        result = ink_distance(text, body)
        assert result.intersects is overlaps
        check = TextSymbolOverlapCheck(method="render", clearance=0, include_own_pins=True).run(ctx)
        assert check.is_pass is not overlaps, check.message
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


@pytest.mark.integration
def test_caller_controls_whether_own_pins_are_compared(kicad_cli_required, tmp_path):
    path = tmp_path / "pin-policy.kicad_sch"
    path.write_text(resistor_sheet(str(kicad_cli_required), (99.4, 104.2)), encoding="utf-8")
    with DesignContext(schematic_path=path, kicad_cli_path=str(kicad_cli_required)) as ctx:
        renderer = InkRenderer(ctx.schematic)
        text = renderer.object_ink(reference="R1", field="Value")
        assert not ink_distance(
            text, renderer.object_ink(reference="R1", include_pins=False)
        ).intersects
        assert ink_distance(text, renderer.object_ink(reference="R1", include_pins=True)).intersects
        assert (
            TextSymbolOverlapCheck(method="render", clearance=0, include_own_pins=False)
            .run(ctx)
            .is_pass
        )
        assert (
            not TextSymbolOverlapCheck(method="render", clearance=0, include_own_pins=True)
            .run(ctx)
            .is_pass
        )


@pytest.mark.integration
def test_rendered_text_checks_include_visible_power_values(kicad_cli_required, tmp_path):
    from kicad_evaltor import TextTextOverlapCheck

    stock = dumps(SymbolLibraries(cli_path=str(kicad_cli_required)).embeddable("power:GND"))
    path = tmp_path / "power.kicad_sch"
    path.write_text(
        f"""(kicad_sch (version 20250114) (generator "eeschema")
      (uuid "11111111-2222-3333-4444-555555555555") (paper "A4") (lib_symbols {stock})
      (symbol (lib_id "power:GND") (at 100 100 0) (unit 1)
       (uuid "bbbb0000-1111-2222-3333-000000000001")
       (property "Reference" "#PWR01" (at 100 100 0) (hide yes) (effects (font (size 1.27 1.27))))
       (property "Value" "GND" (at 100 106 0) (effects (font (size 1.27 1.27)))))
      (text "GND" (at 100 106 0) (effects (font (size 1.27 1.27)))
       (uuid "aaaa0000-1111-2222-3333-000000000001"))
      (sheet_instances (path "/" (page "1"))))""",
        encoding="utf-8",
    )
    with DesignContext(schematic_path=path, kicad_cli_path=str(kicad_cli_required)) as ctx:
        result = TextTextOverlapCheck(method="render", clearance=0).run(ctx)
        assert not result.is_pass
        assert result.status.value == "fail", result.message
        assert result.details["count"] == 1
