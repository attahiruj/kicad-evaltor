import pytest

from kicad_evaltor.checks.base import CheckCategory, TestStatus as Status
from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.checks.schematic.overlaps import OverlapParams
from kicad_evaltor.core.context import DesignContext
from conftest import demo_schematic_path, hierarchy_schematic_path, sheet_symbol


TEXT_TEXT = "sch.layout.text_text_overlap"
TEXT_SYMBOL = "sch.layout.text_symbol_overlap"
TEXT_WIRE = "sch.layout.text_wire_overlap"
SYMBOL_SYMBOL = "sch.layout.symbol_symbol_overlap"
SYMBOL_WIRE = "sch.layout.symbol_wire_overlap"
NO_CONNECT = "sch.noconnect.floating"
TEXT_OFF_SHEET = "sch.layout.text_off_sheet"

ALL = [TEXT_TEXT, TEXT_SYMBOL, TEXT_WIRE, SYMBOL_SYMBOL, SYMBOL_WIRE, NO_CONNECT, TEXT_OFF_SHEET]


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


_hierarchy_ctx = None


def run_tree(check_id, **params):
    """Run one check against the hierarchical demo, cached like ``run``."""
    global _hierarchy_ctx
    if _hierarchy_ctx is None:
        _hierarchy_ctx = DesignContext(schematic_path=hierarchy_schematic_path())
    return CheckRegistry.create(check_id, **params).run(_hierarchy_ctx)


def count_tree(check_id, **params):
    """Findings on the hierarchical demo, counting a pass as zero."""
    return run_tree(check_id, **params).details.get("count", 0)


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
    """The flat demo's layout, pinned so changes are deliberate.

    It is drawn tidily, and with every box measured where KiCad draws it, no
    layout check finds anything on it.
    """

    def test_rotated_parts_do_not_fake_an_overlap(self):
        # SW1 is placed at 270 degrees and C4 at 90, with their fields stored at
        # 90. The symbol's turn cancels the field's, so KiCad draws both flat and
        # neither part's reference collides with its value.
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

    def test_net_labels_sit_clear_of_the_wires_they_name(self):
        # KiCad lifts a net label's text off its wire, so SDA and SCL, which the
        # line cell around their anchors used to drag onto the wire, touch none.
        result = run(TEXT_WIRE)
        assert result.status is Status.PASS

    def test_no_two_symbol_bodies_share_space(self):
        result = run(SYMBOL_SYMBOL)
        assert result.status is Status.PASS
        assert result.details["checked"] == 34

    def test_everything_fits_on_the_sheet(self):
        result = run(TEXT_OFF_SHEET)
        assert result.status is Status.PASS
        assert result.details["sheet"] == [297.0, 210.0]
        # 34 fields and labels, and no power symbol values: those are artwork.
        assert result.details["checked"] == 34


class TestHierarchyDemoResults:
    """The hierarchical demo: findings from both sheets, each named for its sheet.

    Items on the root are named as they are; items on ``shared`` carry the
    ``shared.`` prefix, so one report can hold both without ambiguity.
    """

    def test_the_default_looks_at_every_sheet(self):
        # 29 symbols on the root and 48 with a body on shared: a root-only
        # default would check the first number alone.
        assert run_tree(SYMBOL_SYMBOL).details["checked"] == 77

    def test_no_text_crosses_a_symbol(self):
        # The LEDs' references sit beside their light arrows and CC2 sits between
        # two of J2's pins. Each is clear of every drawn piece; only the box
        # around a whole symbol, with its empty corners, would reach them.
        result = run_tree(TEXT_SYMBOL)
        assert result.status is Status.PASS
        # 112 runs of text and 78 symbols.
        assert result.details["checked"] == 190

    def test_a_finding_names_the_sheet_it_came_from(self):
        result = run_tree(TEXT_WIRE)
        sheets = {o["first"].split(".", 1)[0] for o in result.details["overlaps"]}
        assert sheets == {"shared"}

    def test_the_sheet_qualifier_stays_off_the_items_own_properties(self):
        # The prefix says which sheet a finding came from. The properties are the
        # item's own values, and prefixing them would report a name that does not
        # exist on the sheet.
        result = run_tree(TEXT_WIRE)
        for finding in result.details["overlaps"]:
            _, _, name = finding["first"].partition(".")
            assert finding["first_properties"] == {"hierarchical_label": name}

    def test_opt_in_wire_check_only_finds_labels_on_their_own_wires(self):
        # Kept out of the demo suite because every finding is a hierarchical
        # label's flag on the wire it connects, which is what the flag is for.
        result = run_tree(TEXT_WIRE)
        assert result.status is Status.FAIL
        assert result.details["count"] == 17
        assert all(o["first"].startswith("shared.") for o in result.details["overlaps"])
        assert all(o["second_kind"] == "wire" for o in result.details["overlaps"])

    def test_text_never_meets_text(self):
        result = run_tree(TEXT_TEXT)
        assert result.status is Status.PASS
        assert result.details["checked"] == 112

    def test_each_sheet_reads_its_own_paper(self):
        result = run_tree(TEXT_OFF_SHEET)
        assert result.status is Status.PASS
        assert result.details["sheets"] == [
            {"sheet": "hierarchy", "size": [297.0, 210.0]},
            {"sheet": "shared", "size": [297.0, 210.0]},
        ]

    def test_flags_on_sheet_pins_and_pins_on_pins_are_kept_promises(self):
        # Six flags close unused pins of the shared sheet block, and four power
        # symbols sit straight on a part's pin with no wire between. KiCad's ERC
        # is clean, and so is this.
        result = run_tree(NO_CONNECT)
        assert result.status is Status.PASS
        assert result.details["flags"] == 14


class TestSheetSelection:
    """The ``sheet`` parameter: what a check looks at, and how it says so."""

    def test_a_single_sheet_can_be_named(self):
        assert run_tree(SYMBOL_SYMBOL, sheet="shared").details["checked"] == 48

    def test_the_root_can_be_named(self):
        assert run_tree(SYMBOL_SYMBOL, sheet="hierarchy").details["checked"] == 29

    def test_a_sheet_can_be_named_by_its_linked_file_stem(self):
        assert run_tree(SYMBOL_SYMBOL, sheet="circuit").details["checked"] == 48

    def test_the_name_is_matched_without_regard_to_case(self):
        assert run_tree(SYMBOL_SYMBOL, sheet="ALL").details["checked"] == 77

    def test_an_unknown_sheet_skips_and_names_the_ones_that_would_work(self):
        result = run_tree(SYMBOL_SYMBOL, sheet="Nonesuch")

        assert result.status is Status.SKIP
        assert "Nonesuch" in result.message
        assert result.details["sheets"] == ["hierarchy", "shared"]

    def test_ignore_matches_the_unprefixed_label_on_every_sheet(self):
        # Sheet-qualified ignore entries are out of scope, so "VDD_IN" has to keep
        # working when the finding it drops is labelled "shared.VDD_IN".
        everything = run_tree(TEXT_WIRE)
        dropped = [o for o in everything.details["overlaps"] if o["first"] == "shared.VDD_IN"]

        assert dropped
        kept = run_tree(TEXT_WIRE, ignore=["VDD_IN"])
        assert kept.details["count"] == everything.details["count"] - len(dropped)


# Two 5.08 x 5.08 bodies with a 1.27mm pin stub top and bottom. The outline is
# stroked half a line width outside its path, so body+pins spans y 96.31..103.97
# while the body alone spans 97.38..102.62.
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

    R1's reference sits over its own pin stub and clears its own body by
    0.24mm, R2's wholly inside its own body, R3's across its own body's edge,
    and a free-standing label over R2's pin.
    """
    path = tmp_path / "own_symbol.kicad_sch"
    path.write_text(
        f"""(kicad_sch (version 20250114) (generator "evaltor") (paper "A4")
  (lib_symbols
    {PART}
  )
  (symbol (lib_id "Device:R") (at 100 100 0)
    {field("R1", "10k", 100, 103.6)}
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


class TestNoConnectFlags:
    """A flag that marks nothing, and a pin nothing reaches, are both promises
    the sheet does not keep.

    The demo sheet is clean by design, so these cases are built rather than
    observed.
    """

    SYMBOL = """(symbol "Test:Tagged"
      (property "Reference" "T?")
      (symbol "Tagged_0_1"
        (rectangle (start -2.54 0) (end 2.54 -2.54))
        (pin passive line (at -2.54 -1.27 0) (length 0) (name P1) (number 1))
        (pin passive line (at 2.54 -1.27 180) (length 0) (name P2) (number 2))
      )
    )"""

    @pytest.fixture
    def ctx(self, tmp_path):
        def schematic(extra=""):
            path = tmp_path / f"flags_{abs(hash(extra))}.kicad_sch"
            path.write_text(
                f"""(kicad_sch (version 20250114) (generator "evaltor") (paper "A4")
  (lib_symbols
    {self.SYMBOL}
  )
  (symbol (lib_id "Test:Tagged") (at 100 100 0)
    (property "Reference" "T1" (at 100 90))
  )
  {extra}
)
""",
                encoding="utf-8",
            )
            return DesignContext(schematic_path=path)

        return schematic

    def _run(self, ctx):
        result = CheckRegistry.create(NO_CONNECT).run(ctx)
        return result

    def test_the_demo_sheet_flags_every_pin_it_leaves_open(self):
        result = run(NO_CONNECT, sheet="all")
        assert result.status is Status.PASS
        assert result.details["flags"] == 37

    def test_no_sheet_leaves_a_pin_open(self):
        assert run(NO_CONNECT).details["flags"] == 37

    def test_a_flag_on_a_pin_is_a_kept_promise(self, ctx):
        both = "(no_connect (at 97.46 101.27)) (no_connect (at 102.54 101.27))"
        result = self._run(ctx(both))
        assert result.status is Status.PASS
        assert result.details.get("unmarked_pins", []) == []

    def test_a_flag_off_every_pin_marks_nothing(self, ctx):
        result = self._run(ctx("(no_connect (at 100 99))"))
        assert result.status is Status.FAIL
        assert result.details["floating_flags"] == [[100.0, 99.0]]

    def test_a_wire_reaching_a_pin_counts(self, ctx):
        wire = "(wire (pts (xy 102.54 101.27) (xy 110 101.27)) (stroke (width 0) (type default)))"
        result = self._run(ctx(wire))
        assert result.details["unmarked_pins"] == ["T1.1"]

    def test_a_label_on_a_pin_counts(self, ctx):
        label = """(label "N1" (at 97.46 101.27 0) (effects (font (size 1.27 1.27))))"""
        result = self._run(ctx(label))
        assert result.details["unmarked_pins"] == ["T1.2"]

    def test_a_pin_ending_on_another_pin_is_connected(self, ctx):
        # A second part placed so its pin lands on T1's: KiCad joins the two
        # with no wire, the way a power symbol sits on a capacitor.
        other = """(symbol (lib_id "Test:Tagged") (at 105.08 100 0)
    (property "Reference" "T2" (at 105.08 90))
  )"""
        result = self._run(ctx(other))
        assert "T1.2" not in result.details["unmarked_pins"]
        assert "T2.1" not in result.details["unmarked_pins"]

    def test_a_flag_on_a_sheet_pin_marks_it(self, ctx):
        # An unused pin on a sheet block is closed with a flag like any other.
        # ``sheet_symbol`` puts the first pin at (58.8, 50.8).
        block = sheet_symbol("Child", "child.kicad_sch", uuid="sheet-1", pins=["SPARE"])
        both = "(no_connect (at 97.46 101.27)) (no_connect (at 102.54 101.27))"
        result = self._run(ctx(block + both + "(no_connect (at 58.8 50.8))"))
        assert result.details.get("floating_flags", []) == []

    def test_a_label_counts_by_its_anchor_not_its_letters(self, ctx):
        # KiCad draws a net label's text clear of the point it connects at, so a
        # label whose letters happen to cover a pin does not connect to it.
        label = """(label "N1" (at 96.5 102.5 0) (effects (font (size 1.27 1.27))))"""
        result = self._run(ctx(label))
        assert result.details["unmarked_pins"] == ["T1.1", "T1.2"]

    def test_text_on_a_pin_does_not_connect_it(self, ctx):
        note = """(text "N1" (at 97.46 101.27 0) (effects (font (size 1.27 1.27))))"""
        result = self._run(ctx(note))
        assert result.details["unmarked_pins"] == ["T1.1", "T1.2"]

    def test_the_details_name_both_kinds_of_finding(self, ctx):
        result = self._run(ctx("(no_connect (at 100 99))"))
        assert result.message == "1 flag marking nothing, 2 pins reached by nothing"
        assert result.details["floating_flags"] == [[100.0, 99.0]]
        assert result.details["unmarked_pins"] == ["T1.1", "T1.2"]

    def test_a_pin_the_symbol_hides_is_not_reported(self, tmp_path):
        """A hidden pin is not drawn, so nothing on the sheet can be wrong about it."""
        path = tmp_path / "hidden.kicad_sch"
        path.write_text(
            """(kicad_sch (version 20250114) (generator "evaltor") (paper "A4")
  (lib_symbols
    (symbol "Test:Hidden"
      (symbol "Hidden_0_1"
        (rectangle (start -2.54 0) (end 2.54 -2.54))
        (pin passive line (at -2.54 -1.27 0) (length 0) (name P1) (number 1))
        (pin no_connect line (at 2.54 -1.27 180) (length 0) (hide yes) (name NC) (number 2))
      )
    )
  )
  (symbol (lib_id "Test:Hidden") (at 100 100 0)
    (property "Reference" "T1" (at 100 90))
  )
  (no_connect (at 97.46 101.27))
)
""",
            encoding="utf-8",
        )
        result = self._run(DesignContext(schematic_path=path))
        assert result.status is Status.PASS
        assert result.details["pins"] == 1

    def test_ignore_accepts_a_pin_left_open_on_purpose(self, ctx):
        result = CheckRegistry.create(NO_CONNECT, ignore=["T1.1", "T1.2"]).run(ctx())
        assert result.status is Status.PASS


class TestWiresUnderSymbols:
    """A wire ends at a pin; it does not run back under the symbol.

    A body that hangs below its pin at (100, 100) spans y 100..102.62. Four
    wires cross that body, and only the two that are actually wrong may be
    reported, or every power symbol on a real sheet would be.
    """

    SYMBOL = """(symbol "Test:Post"
      (property "Reference" "P?")
      (symbol "Post_0_1"
        (rectangle (start -2.54 0) (end 2.54 -2.54))
        (pin passive line (at 0 0 270) (length 0) (name P) (number 1))
      )
    )"""

    WIRES = {
        "into": ((100, 100), (100, 105)),
        "away": ((100, 100), (100, 95)),
        "square": ((100, 100), (105, 100)),
        "crossing": ((100, 101.5), (105, 101.5)),
    }

    @pytest.fixture
    def ctx(self, tmp_path):
        wires = "".join(
            f"""(wire (pts (xy {x0} {y0}) (xy {x1} {y1}))
        (stroke (width 0) (type default)))"""
            for (x0, y0), (x1, y1) in self.WIRES.values()
        )
        path = tmp_path / "buried_wire.kicad_sch"
        path.write_text(
            f"""(kicad_sch (version 20250114) (generator "evaltor") (paper "A4")
  (lib_symbols
    {self.SYMBOL}
  )
  (symbol (lib_id "Test:Post") (at 100 100 0)
    (property "Reference" "P1" (at 100 90))
  )
  {wires}
)
""",
            encoding="utf-8",
        )
        return DesignContext(schematic_path=path)

    @pytest.fixture
    def label(self):
        def label(name):
            # WireSegment renders its endpoints as floats, so the expected label
            # is built the same way rather than from the integers in the file.
            (x0, y0), (x1, y1) = ((float(a), float(b)) for a, b in self.WIRES[name])
            return f"wire {(x0, y0)}->{(x1, y1)}"

        return label

    def _reported(self, ctx):
        result = CheckRegistry.create(SYMBOL_WIRE).run(ctx)
        assert result.status is Status.FAIL
        return {o["second"] for o in result.details["overlaps"]}

    def test_a_wire_running_into_the_body_is_reported(self, ctx, label):
        assert label("into") in self._reported(ctx)

    def test_a_wire_leaving_away_from_the_body_is_not(self, ctx, label):
        assert label("away") not in self._reported(ctx)

    def test_a_wire_arriving_square_to_the_pin_is_not(self, ctx, label):
        assert label("square") not in self._reported(ctx)

    def test_a_wire_crossing_a_body_with_no_pin_under_it_is_reported(self, ctx, label):
        assert label("crossing") in self._reported(ctx)

    def test_the_demo_sheet_has_no_buried_wires(self):
        result = run(SYMBOL_WIRE)
        assert result.status is Status.PASS
        assert result.details.get("count", 0) == 0

    def test_a_finding_names_the_symbol_and_the_wire(self, ctx):
        result = CheckRegistry.create(SYMBOL_WIRE).run(ctx)
        finding = result.details["overlaps"][0]
        assert finding["first_kind"] == "symbol"
        assert finding["first"] == "P1"
        assert finding["second_kind"] == "wire"
        assert finding["second_properties"] == {}


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
        result = run_tree(TEXT_WIRE)
        for finding in result.details["overlaps"]:
            assert finding["first_kind"] == "text"
            assert len(finding["first_properties"]) == 1

    def test_a_wire_shows_nothing_so_it_reports_no_properties(self):
        result = run_tree(TEXT_WIRE)
        for finding in result.details["overlaps"]:
            assert finding["second_kind"] == "wire"
            assert finding["second_properties"] == {}

    def test_symbol_findings_report_the_parts_properties(self):
        # A wide margin forces a finding between symbols, so the payload can be
        # inspected without editing the design.
        result = run_tree(SYMBOL_SYMBOL, sheet="shared", margin=40.0)
        assert result.status is Status.FAIL
        for finding in result.details["overlaps"]:
            assert finding["first_kind"] == finding["second_kind"] == "symbol"
            for side in ("first", "second"):
                properties = finding[f"{side}_properties"]
                reference = finding[side].partition(".")[2]
                # A power symbol hides its reference, and often its value too, so
                # it can draw no property at all. A part always draws some.
                if not reference.startswith("#"):
                    assert properties
                assert properties.get("Reference") in (None, reference)

    def test_the_whole_failure_payload_is_json_serialisable(self):
        import json

        result = run_tree(TEXT_WIRE)
        assert json.loads(json.dumps(result.details))["overlaps"] == result.details["overlaps"]


class TestParamsAffectResults:
    def test_margin_is_reported_back(self):
        assert run(TEXT_TEXT, margin=1.0).details["margin"] == 1.0

    def test_a_large_margin_catches_more(self):
        assert count_tree(TEXT_TEXT, margin=1.0) > count_tree(TEXT_TEXT, margin=0.0)

    def test_ignore_drops_a_named_item(self):
        everything = run_tree(TEXT_WIRE)
        dropped = [o for o in everything.details["overlaps"] if o["first"] == "shared.+3.7V_IN"]
        assert dropped, "the demo should report +3.7V_IN against a wire"
        assert count_tree(TEXT_WIRE, ignore=["+3.7V_IN"]) == (
            everything.details["count"] - len(dropped)
        )

    def test_negative_margin_sheds_noise_rather_than_adding_it(self):
        assert count_tree(TEXT_WIRE, margin=-0.2) < count_tree(TEXT_WIRE)

    def test_positive_margin_is_the_other_direction(self):
        assert count_tree(TEXT_WIRE, margin=0.5) > count_tree(TEXT_WIRE)

    def test_a_margin_that_would_flatten_everything_does_not_crash(self):
        # Thin boxes invert under a large negative margin; the engine drops them
        # rather than aborting the check.
        assert count_tree(TEXT_WIRE, margin=-5.0) == 0

    def test_margin_must_be_a_number(self):
        with pytest.raises(TypeError, match="margin"):
            OverlapParams(margin="loose")


class TestMissingInputs:
    def test_checks_skip_without_a_schematic(self):
        empty = DesignContext(schematic_path="does/not/exist.kicad_sch")
        for check_id in ALL:
            result = CheckRegistry.create(check_id).run(empty)
            assert result.status is Status.SKIP, check_id


# Two notes drawn on the same spot, one of them hidden, and a hidden label doing
# the same. A symbol field carries `(hide yes)` beside itself; these carry it
# inside the effects block, which is a different place to look.
TWO_DRAWN_NOTES = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols)
  (text "FIRST" (at 50 50 0) (effects (font (size 1.27 1.27))))
  (text "SECOND" (at 50 50 0) (effects (font (size 1.27 1.27))))
)
"""

TWO_DRAWN_NOTES_PLUS_HIDDEN = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols)
  (text "FIRST" (at 50 50 0) (effects (font (size 1.27 1.27))))
  (text "SECOND" (at 50 50 0) (effects (font (size 1.27 1.27))))
  (text "HIDDEN NOTE" (at 50 50 0) (effects (font (size 1.27 1.27)) (hide yes)))
  (label "HIDDEN LABEL" (at 50 50 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
  (global_label "HIDDEN GLOBAL" (shape input) (at 50 50 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
  (hierarchical_label "HIDDEN HIER" (shape input) (at 50 50 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
)
"""

# A note parked well outside the A4 page, hidden.
HIDDEN_OFF_SHEET = """(kicad_sch (version 20250114) (generator "evaltor")
  (paper "A4")
  (lib_symbols)
  (text "HIDDEN OFF SHEET" (at 900 900 0)
    (effects (font (size 1.27 1.27)) (hide yes)))
)
"""


class TestHiddenTextIsNotInk:
    """Hidden text draws nothing, so it cannot overlap anything.

    These are the reported symptom: a note the author hid still landing in the
    scene, and the layout checks reporting overlaps between text no reader can
    see and text they can.
    """

    @staticmethod
    def _count(tmp_path, check_id: str, source: str) -> int:
        path = tmp_path / f"{check_id}.kicad_sch"
        path.write_text(source, encoding="utf-8")
        with DesignContext(schematic_path=path) as ctx:
            result = CheckRegistry.create(check_id).run(ctx)
        return result.details.get("count", 0)

    def test_the_two_drawn_notes_collide(self, tmp_path):
        assert self._count(tmp_path, TEXT_TEXT, TWO_DRAWN_NOTES) == 1

    def test_adding_hidden_text_changes_nothing(self, tmp_path):
        # Same single collision between the two visible notes. The three hidden
        # items sit on the same spot, so a scene that kept them would report
        # more than one finding here.
        assert self._count(tmp_path, TEXT_TEXT, TWO_DRAWN_NOTES_PLUS_HIDDEN) == 1

    def test_hidden_text_off_the_page_is_not_an_off_sheet_finding(self, tmp_path):
        assert self._count(tmp_path, TEXT_OFF_SHEET, HIDDEN_OFF_SHEET) == 0
