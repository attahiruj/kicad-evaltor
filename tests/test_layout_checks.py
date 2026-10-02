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
