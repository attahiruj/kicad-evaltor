import pytest

from kicad_evaltor.checks.base import CheckCategory, TestStatus as Status
from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.checks.schematic.overlaps import OverlapParams
from kicad_evaltor.core.context import DesignContext
from conftest import demo_schematic_path


TEXT_TEXT = "sch.layout.text_text_overlap"
TEXT_SYMBOL = "sch.layout.text_symbol_overlap"
TEXT_WIRE = "sch.layout.text_wire_overlap"
SYMBOL_SYMBOL = "sch.layout.symbol_symbol_overlap"
TEXT_OFF_SHEET = "sch.layout.text_off_sheet"

ALL = [TEXT_TEXT, TEXT_SYMBOL, TEXT_WIRE, SYMBOL_SYMBOL, TEXT_OFF_SHEET]


def count(check_id, **params):
    """Findings, counting a pass as zero so numbers stay comparable."""
    return CheckRegistry.create(check_id, **params).run(_CTX()).details.get("count", 0)


def _CTX():
    global _ctx
    if _ctx is None:
        _ctx = DesignContext(schematic_path=demo_schematic_path())
    return _ctx


_ctx = None


def run(check_id, **params):
    """Run one check against the demo sheet.

    The context is cached across calls because it memoises the parsed
    schematic, and re-parsing per call would dominate the test time.
    """
    return CheckRegistry.create(check_id, **params).run(_CTX())


class TestRegistration:
    @pytest.mark.parametrize("check_id", ALL)
    def test_check_is_registered(self, check_id):
        assert CheckRegistry.get(check_id).id == check_id

    @pytest.mark.parametrize("check_id", ALL)
    def test_check_is_described(self, check_id):
        check = CheckRegistry.get(check_id)
        assert check.name
        assert check.description
        assert check.category is CheckCategory.SCHEMATIC


class TestDemoSheetResults:
    """The demo sheet's known layout defects, pinned so changes are deliberate."""

    def test_rotated_parts_do_not_fake_an_overlap(self):
        # SW1 is placed at 270 degrees and C4 at 90, with their fields stored at
        # a compensating angle. KiCad draws field text flat whatever the field or
        # symbol angle says, so neither part's reference collides with its value.
        result = run(TEXT_TEXT)
        assert result.status is Status.PASS
        assert result.details.get("overlaps", []) == []

    def test_hidden_power_symbol_values_are_not_checked(self):
        # GND and +3.3V sit on top of U2 in the file, but a power symbol's name
        # is drawn by its own artwork rather than as a text field, so it is not
        # text that can collide with the IC.
        result = run(TEXT_SYMBOL)
        assert result.status is Status.PASS
        assert result.details.get("overlaps", []) == []

    def test_opt_in_wire_check_only_finds_masked_labels(self):
        # Kept out of the demo suite because every finding is a label sitting on
        # the wire it labels, which KiCad masks. This pins that expectation so
        # nobody mistakes the output for a defect list.
        result = run(TEXT_WIRE)
        assert result.status is Status.FAIL
        assert result.details["count"] == 2
        labels = {o["first"] for o in result.details["overlaps"]}
        assert {"SDA", "SCL"} <= labels
        assert all(o["second_kind"] == "wire" for o in result.details["overlaps"])

    def test_no_two_symbol_bodies_share_space(self):
        result = run(SYMBOL_SYMBOL)
        assert result.status is Status.PASS
        assert result.details["checked"] == 34

    def test_everything_fits_on_the_sheet(self):
        result = run(TEXT_OFF_SHEET)
        assert result.status is Status.PASS
        assert result.details["sheet"] == [297.0, 210.0]
        # 34 fields plus no power symbol values: those are artwork, not text.
        assert result.details["checked"] == 34


# Two 5.08 x 5.08 bodies with a 1.27mm pin stub top and bottom, so body+pins spans
# y 96.19..103.81 while the body alone spans 97.46..102.54.
PART = """(symbol "Device:R"
      (symbol "R_0_1"
        (rectangle (start -2.54 -2.54) (end 2.54 2.54))
        (pin 1 (at 0 3.81 270) (length 1.27))
        (pin 2 (at 0 -3.81 90) (length 1.27))
      )
    )"""


def field(reference, value, x, y):
    return f"""(property "Reference" "{reference}" (at {x} {y} 0)
        (effects (font (size 1.27 1.27))))
      (property "Value" "{value}" (at {x} {y - 8} 0)
        (effects (font (size 1.27 1.27))))"""


@pytest.fixture
def own_symbol_ctx(tmp_path):
    """One placement per rule branch, around identical 5.08mm bodies.

    R1's reference sits over its own pin stub, R2's wholly inside its own body,
    R3's across its own body's edge, and a free-standing label over R2's pin.
    """
    path = tmp_path / "own_symbol.kicad_sch"
    path.write_text(
        f"""(kicad_sch (version 20250114) (generator "evaltor") (paper "A4")
  (lib_symbols
    {PART}
  )
  (symbol (lib_id "Device:R") (at 100 100 0)
    {field("R1", "10k", 100, 103.5)}
  )
  (symbol (lib_id "Device:R") (at 140 100 0)
    {field("R2", "10k", 140, 100)}
  )
  (symbol (lib_id "Device:R") (at 180 100 0)
    {field("R3", "10k", 180, 102.5)}
  )
  (text "NETTLE"
    (at 140 103.2 0)
    (effects (font (size 1.27 1.27)))
  )
)
""",
        encoding="utf-8",
    )
    return DesignContext(schematic_path=path)


class TestOwnSymbolText:
    """A part's own fields must clear its own artwork, but not its own pins.

    The demo sheet is clean by design, so these cases are built rather than
    observed. The exemptions are narrow on purpose: text that fits wholly inside
    a body is left alone, because a small symbol is mostly empty space, while text
    crossing the outline is reported.
    """

    def _reported(self, ctx):
        result = CheckRegistry.create(TEXT_SYMBOL).run(ctx)
        assert result.status is Status.FAIL
        return {o["first"]: o["second"] for o in result.details["overlaps"]}

    def test_text_across_its_own_body_edge_is_reported(self, own_symbol_ctx):
        assert self._reported(own_symbol_ctx)["R3.Reference"] == "R3"

    def test_text_wholly_inside_its_own_body_is_not(self, own_symbol_ctx):
        reported = self._reported(own_symbol_ctx)
        assert "R2.Reference" not in reported
        assert "R2.Value" not in reported

    def test_text_over_its_own_pin_stub_is_not(self, own_symbol_ctx):
        # A reference above a body is where the topmost pin also is, so this is
        # the case the pin exemption exists for.
        reported = self._reported(own_symbol_ctx)
        assert "R1.Reference" not in reported
        assert "R1.Value" not in reported

    def test_the_same_pin_stub_is_still_reported_for_text_that_owns_nothing(self, own_symbol_ctx):
        # Otherwise the rule would be a blanket exemption rather than a statement
        # about a part's own artwork.
        assert self._reported(own_symbol_ctx)["NETTLE"] == "R2"


class TestFindingsCarryProperties:
    def test_text_findings_report_the_property_they_were_drawn_from(self):
        result = run(TEXT_WIRE)
        for finding in result.details["overlaps"]:
            assert finding["first_kind"] == "text"
            assert finding["first_properties"] == {"label": finding["first"]}

    def test_a_wire_shows_nothing_so_it_reports_no_properties(self):
        result = run(TEXT_WIRE)
        for finding in result.details["overlaps"]:
            assert finding["second_kind"] == "wire"
            assert finding["second_properties"] == {}

    def test_symbol_findings_report_the_parts_properties(self):
        # A wide margin forces a finding on the tidy demo sheet, so the payload
        # can be inspected without editing the design.
        result = run(SYMBOL_SYMBOL, margin=40.0)
        assert result.status is Status.FAIL
        for finding in result.details["overlaps"]:
            assert finding["first_kind"] == finding["second_kind"] == "symbol"
            for side in ("first", "second"):
                properties = finding[f"{side}_properties"]
                assert properties
                # A power symbol's reference is hidden, so not every part draws one.
                assert properties.get("Reference") in (None, finding[side])

    def test_the_whole_failure_payload_is_json_serialisable(self):
        import json

        result = run(TEXT_WIRE)
        assert json.loads(json.dumps(result.details))["overlaps"] == result.details["overlaps"]


class TestParamsAffectResults:
    def test_margin_is_reported_back(self):
        assert run(TEXT_TEXT, margin=1.0).details["margin"] == 1.0

    def test_a_large_margin_catches_more(self):
        assert count(TEXT_TEXT, margin=1.0) > count(TEXT_TEXT, margin=0.0)

    def test_ignore_drops_a_named_item(self):
        everything = count(TEXT_WIRE)
        assert count(TEXT_WIRE, ignore=["SDA"]) == everything - 1

    def test_negative_margin_sheds_noise_rather_than_adding_it(self):
        assert count(TEXT_WIRE, margin=-0.2) < count(TEXT_WIRE)

    def test_positive_margin_is_the_other_direction(self):
        assert count(TEXT_WIRE, margin=0.5) > count(TEXT_WIRE)

    def test_a_margin_that_would_flatten_everything_does_not_crash(self):
        # Thin boxes invert under a large negative margin; the engine drops them
        # rather than aborting the check.
        assert count(TEXT_WIRE, margin=-5.0) == 0

    def test_margin_must_be_a_number(self):
        with pytest.raises(TypeError, match="margin"):
            OverlapParams(margin="loose")


class TestMissingInputs:
    def test_checks_skip_without_a_schematic(self):
        empty = DesignContext(schematic_path="does/not/exist.kicad_sch")
        for check_id in ALL:
            result = CheckRegistry.create(check_id).run(empty)
            assert result.status is Status.SKIP, check_id
