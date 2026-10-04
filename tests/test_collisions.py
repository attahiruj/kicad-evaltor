import pytest

from kicad_evaltor.collisions import CollisionItem, colliding_pairs, cross_kind
from kicad_evaltor.geometry import BBox


def item(label, x0, y0, x1, y1, kind="box", owner=None, properties=None):
    return CollisionItem(kind, label, BBox(x0, y0, x1, y1), owner, properties or {})


class TestOverlapSemantics:
    def test_identical_boxes_collide(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 0, 0, 2, 2)
        assert len(colliding_pairs([a, b])) == 1

    def test_touching_edges_do_not_collide(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2, 0, 4, 2)
        assert colliding_pairs([a, b]) == []

    def test_separated_boxes_do_not_collide(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 3, 0, 5, 2)
        assert colliding_pairs([a, b]) == []

    def test_corner_contact_does_not_collide(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2, 2, 4, 4)
        assert colliding_pairs([a, b]) == []

    def test_overlap_area_is_the_shared_region(self):
        a, b = item("a", 0, 0, 4, 4), item("b", 2, 1, 6, 3)
        found = colliding_pairs([a, b])
        assert len(found) == 1
        area = found[0].area
        assert (area.min_x, area.min_y, area.max_x, area.max_y) == (2, 1, 4, 3)
        assert found[0].width == pytest.approx(2.0)
        assert found[0].height == pytest.approx(2.0)


class TestMargin:
    def test_zero_margin_ignores_a_small_gap(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.5, 0, 4, 2)
        assert colliding_pairs([a, b]) == []

    def test_margin_turns_a_gap_into_a_collision(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.5, 0, 4, 2)
        assert len(colliding_pairs([a, b], margin=0.5)) == 1

    def test_margin_grows_both_sides(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 3.0, 0, 5, 2)
        # Each box grows by the full margin, so a 1.0 gap closes at margin 0.5.
        # Closing exactly means touching, which is not a collision.
        assert colliding_pairs([a, b], margin=0.5) == []
        assert len(colliding_pairs([a, b], margin=0.6)) == 1

    def test_large_margin_catches_a_near_miss(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.2, 0, 4, 2)
        assert colliding_pairs([a, b]) == []
        assert len(colliding_pairs([a, b], margin=0.3)) == 1


class TestClearance:
    """A clearance is a gap between two boxes, not a growth of either one."""

    def test_a_gap_wider_than_the_clearance_is_left_alone(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.21, 0, 4, 2)
        assert colliding_pairs([a, b], clearance=0.2) == []

    def test_a_gap_just_under_the_clearance_is_caught(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.19, 0, 4, 2)
        assert len(colliding_pairs([a, b], clearance=0.2)) == 1

    def test_a_gap_exactly_at_the_clearance_is_left_alone(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.2, 0, 4, 2)
        assert colliding_pairs([a, b], clearance=0.2) == []

    def test_the_gap_is_reported_with_the_finding(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.19, 0, 4, 2)
        found = colliding_pairs([a, b], clearance=0.2)
        assert found[0].gap == pytest.approx(0.19)
        assert found[0].as_dict()["gap"] == pytest.approx(0.19)

    def test_a_near_miss_points_at_where_they_would_touch(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.19, 0, 4, 2)
        area = colliding_pairs([a, b], clearance=0.2)[0].area
        assert (area.min_x, area.max_x) == (2.0, 2.19)
        assert (area.min_y, area.max_y) == (0.0, 2.0)

    def test_diagonal_near_misses_are_measured_on_the_diagonal(self):
        # 0.1mm apart on each axis is 0.141mm away, which is inside the
        # clearance. Reporting the smaller axis gap would call it 0.1mm.
        a, b = item("a", 0, 0, 2, 2), item("b", 2.1, 2.1, 4, 4)
        found = colliding_pairs([a, b], clearance=0.2)
        assert len(found) == 1
        assert found[0].gap == pytest.approx(0.1 * 2**0.5)

    def test_a_diagonal_gap_wider_than_the_clearance_is_left_alone(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.15, 2.15, 4, 4)
        assert colliding_pairs([a, b], clearance=0.2) == []

    def test_contact_is_caught_because_it_leaves_no_gap(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.0, 0, 4, 2)
        found = colliding_pairs([a, b], clearance=0.2)
        assert len(found) == 1
        assert found[0].gap == 0.0

    def test_zero_clearance_is_overlap_only(self):
        a, b = item("a", 0, 0, 2, 2), item("b", 2.19, 0, 4, 2)
        assert colliding_pairs([a, b], clearance=0.0) == []

    def test_a_clearance_and_a_margin_both_apply(self):
        # A negative margin shrinks the boxes first, so the pair that a 0.2
        # clearance catches needs a wider margin to survive it.
        a, b = item("a", 0, 0, 2, 2), item("b", 2.19, 0, 4, 2)
        assert colliding_pairs([a, b], clearance=0.2)
        assert colliding_pairs([a, b], clearance=0.2, margin=-0.1) == []


class TestOwnerFiltering:
    def test_same_owner_pairs_are_dropped(self):
        a = item("a", 0, 0, 2, 2, owner="R1")
        b = item("b", 1, 0, 3, 2, owner="R1")
        assert len(colliding_pairs([a, b], ignore_same_owner=True)) == 0

    def test_different_owner_pairs_survive(self):
        a = item("a", 0, 0, 2, 2, owner="R1")
        b = item("b", 1, 0, 3, 2, owner="R2")
        assert len(colliding_pairs([a, b], ignore_same_owner=True)) == 1

    def test_ownerless_items_are_never_filtered(self):
        a = item("a", 0, 0, 2, 2)
        b = item("b", 1, 0, 3, 2)
        assert len(colliding_pairs([a, b], ignore_same_owner=True)) == 1


class TestGroups:
    """Pieces of one drawn thing, such as a label's text and its flag."""

    def test_pieces_of_one_thing_never_collide_with_each_other(self):
        group = object()
        text = CollisionItem("text", "GND", BBox(0, 0, 4, 1), group=group)
        flag = CollisionItem("text", "GND", BBox(1, 0, 2, 1), group=group)
        assert colliding_pairs([text, flag]) == []

    def test_a_thing_is_reported_once_however_many_pieces_touch(self):
        group = object()
        text = CollisionItem("text", "GND", BBox(0, 0, 4, 1), group=group)
        flag = CollisionItem("text", "GND", BBox(3, 0, 5, 1), group=group)
        wall = item("wall", 3.5, 0, 6, 1)
        found = colliding_pairs([text, flag, wall])
        assert [(c.first.label, c.second.label) for c in found] == [("GND", "wall")]

    def test_the_empty_corner_between_pieces_collides_with_nothing(self):
        # Text centred over a small flag leaves the box around both mostly empty
        # where the flag is not; something there is clear of both pieces.
        group = object()
        text = CollisionItem("text", "+3.3V", BBox(0, 1, 6, 2), group=group)
        flag = CollisionItem("text", "+3.3V", BBox(1, 0, 2, 1.2), group=group)
        corner = item("C4.Value", 5, 0, 7, 0.9)
        assert colliding_pairs([text, flag, corner]) == []


class TestPairEnumeration:
    def test_every_unordered_pair_is_reported_once(self):
        items = [item("a", 0, 0, 10, 10), item("b", 1, 1, 11, 11), item("c", 2, 2, 12, 12)]
        pairs = colliding_pairs(items)
        assert len(pairs) == 3

    def test_input_order_does_not_change_the_result(self):
        items = [item("a", 5, 5, 15, 15), item("b", 0, 0, 10, 10), item("c", 7, 7, 17, 17)]
        forward = {frozenset((c.first.label, c.second.label)) for c in colliding_pairs(items)}
        backward = {
            frozenset((c.first.label, c.second.label))
            for c in colliding_pairs(list(reversed(items)))
        }
        assert forward == backward

    def test_empty_and_singleton_input_is_quiet(self):
        assert colliding_pairs([]) == []
        assert colliding_pairs([item("a", 0, 0, 1, 1)]) == []


class TestReporting:
    def test_describe_names_both_items(self):
        a, b = item("R1.Value", 0, 0, 2, 2), item("U2", 1, 1, 3, 3)
        collision = colliding_pairs([a, b])[0]
        assert collision.describe() == "R1.Value overlaps U2"

    def test_as_dict_carries_the_area_and_kinds(self):
        a = item("t", 0, 0, 2, 2, kind="text")
        b = item("s", 1, 1, 3, 3, kind="symbol")
        payload = colliding_pairs([a, b])[0].as_dict()
        assert payload["first"] == "t"
        assert payload["second"] == "s"
        assert payload["first_kind"] == "text"
        assert payload["second_kind"] == "symbol"
        assert payload["area"] == [1, 1, 2, 2]

    def test_as_dict_carries_the_properties_behind_the_labels(self):
        # "R1.Value" says which field collided; {"Value": "10k", "LCSC": ...}
        # says what is on the sheet, and a user-defined property is no different
        # from a standard one.
        a = CollisionItem("text", "R1.Value", BBox(0, 0, 2, 2), "R1", {"Value": "10k"})
        b = CollisionItem(
            "symbol", "U2", BBox(1, 1, 3, 3), "U2", {"Value": "MPU-6050", "LCSC": "C221676"}
        )
        payload = colliding_pairs([a, b])[0].as_dict()
        assert payload["first_properties"] == {"Value": "10k"}
        assert payload["second_properties"] == {"Value": "MPU-6050", "LCSC": "C221676"}

    def test_an_item_showing_nothing_reports_empty_properties(self):
        a = item("wire", 0, 0, 2, 2, kind="wire")
        b = item("t", 1, 1, 3, 3, kind="text")
        payload = colliding_pairs([a, b])[0].as_dict()
        assert payload["first_properties"] == {}

    def test_the_payload_survives_json_serialisation(self):
        import json

        a = item("t", 0, 0, 2, 2, kind="text", properties={"Value": "10k"})
        b = item("s", 1, 1, 3, 3, kind="symbol", properties={"LCSC": "C221676"})
        assert json.loads(json.dumps(colliding_pairs([a, b])[0].as_dict()))[
            "second_properties"
        ] == {"LCSC": "C221676"}

    def test_cross_kind_keeps_only_mixed_pairs(self):
        texts = [item("t", 0, 0, 5, 5, kind="text"), item("t2", 0, 0, 5, 5, kind="text")]
        symbols = [item("s", 0, 0, 5, 5, kind="symbol")]
        wires = [item("w", 0, 0, 5, 5, kind="wire")]
        found = colliding_pairs(texts + symbols + wires)
        assert len(cross_kind(found, "text", "symbol")) == 2
        assert len(cross_kind(found, "text", "wire")) == 2
        assert len(cross_kind(found, "text", "text")) == 1
