import pytest

from kicad_evaltor.collisions import CollisionItem, colliding_pairs, cross_kind
from kicad_evaltor.geometry import BBox


def item(label, x0, y0, x1, y1, kind="box", owner=None):
    return CollisionItem(kind, label, BBox(x0, y0, x1, y1), owner)


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

    def test_cross_kind_keeps_only_mixed_pairs(self):
        texts = [item("t", 0, 0, 5, 5, kind="text"), item("t2", 0, 0, 5, 5, kind="text")]
        symbols = [item("s", 0, 0, 5, 5, kind="symbol")]
        wires = [item("w", 0, 0, 5, 5, kind="wire")]
        found = colliding_pairs(texts + symbols + wires)
        assert len(cross_kind(found, "text", "symbol")) == 2
        assert len(cross_kind(found, "text", "wire")) == 2
        assert len(cross_kind(found, "text", "text")) == 1
