"""Pairwise collision detection over axis-aligned boxes.

All the layout checks reduce to the same question -- "do any two of these boxes
collide, and by how much" -- so they share one engine rather than each rolling
its own pairwise loop. The engine is a sweep along x: items are sorted by their
left edge, and each item is compared only against those that start before it
ends. That is near-linear for a typical sheet and, unlike a grid, needs no cell
size to be tuned to the data.

A pair collides when the boxes overlap, or when they sit closer together than a
requested clearance.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from kicad_evaltor.geometry import BBox


@dataclass(frozen=True)
class CollisionItem:
    """Something that can collide: a labelled box plus what kind of thing it is.

    ``properties`` is what the item shows on the sheet, keyed by name. A wire
    shows nothing and has none.
    """

    kind: str
    label: str
    bbox: BBox
    owner: str | None = None
    properties: Mapping[str, str] = field(default_factory=dict)
    # Items sharing a group are pieces of one drawn thing, such as a label's text
    # and its flag. They are never paired with each other, and a pair of groups
    # is reported once however many of their pieces touch.
    group: object | None = field(default=None, compare=False, repr=False)

    def inflated(self, margin: float) -> BBox | None:
        """The box grown by ``margin``, or None if that would collapse it.

        A negative margin can shrink a thin box inside out. Such a box cannot
        overlap anything, so reporting it as absent lets the caller drop it
        rather than abort the whole check on one degenerate item.
        """
        if not margin:
            return self.bbox
        try:
            return self.bbox.inflate(margin)
        except ValueError:
            return None


@dataclass(frozen=True)
class Collision:
    """Two items whose boxes overlap or sit closer than the clearance, and where.

    ``gap`` is the distance between them when they do not actually overlap, which
    is the number a reader wants for a near miss. ``area`` is then the rectangle
    where they would touch, so it has no width or height in the separating
    direction.
    """

    first: CollisionItem
    second: CollisionItem
    area: BBox
    gap: float = 0.0

    @property
    def width(self) -> float:
        return self.area.width

    @property
    def height(self) -> float:
        return self.area.height

    def describe(self) -> str:
        """A one-line summary. Properties stay in :meth:`as_dict`."""
        return f"{self.first.label} overlaps {self.second.label}"

    def as_dict(self) -> dict[str, object]:
        return {
            "first": self.first.label,
            "first_kind": self.first.kind,
            "first_properties": dict(self.first.properties),
            "second": self.second.label,
            "second_kind": self.second.kind,
            "second_properties": dict(self.second.properties),
            "area": [self.area.min_x, self.area.min_y, self.area.max_x, self.area.max_y],
            "width": self.width,
            "height": self.height,
            "gap": self.gap,
        }


def _overlap(a: BBox, b: BBox) -> BBox | None:
    """The shared region of two boxes, or None when they merely touch.

    Touching is not overlapping: two items sharing an edge is normal on a
    schematic, where text routinely sits flush against a symbol outline.
    """
    min_x = max(a.min_x, b.min_x)
    min_y = max(a.min_y, b.min_y)
    max_x = min(a.max_x, b.max_x)
    max_y = min(a.max_y, b.max_y)
    if max_x <= min_x or max_y <= min_y:
        return None
    return BBox(min_x, min_y, max_x, max_y)


def _axis_span(low: float, high: float, near_a: float, near_b: float) -> tuple[float, float]:
    """The shared stretch of one axis, or the nearest points when there is none."""
    if low <= high:
        return (low, high)
    return (min(near_a, near_b), max(near_a, near_b))


def _touching(a: BBox, b: BBox) -> BBox:
    """The span between two separated boxes: how close they come.

    Where the boxes overlap on one axis the span is the whole of that shared
    edge, so a horizontal near miss reports the stretch of it. Where they only
    approach diagonally there is no shared edge, and the span collapses to the
    two corners that come nearest.
    """
    near_x = (min(max(a.min_x, b.min_x), a.max_x), min(max(b.min_x, a.min_x), b.max_x))
    near_y = (min(max(a.min_y, b.min_y), a.max_y), min(max(b.min_y, a.min_y), b.max_y))
    x_span = _axis_span(max(a.min_x, b.min_x), min(a.max_x, b.max_x), *near_x)
    y_span = _axis_span(max(a.min_y, b.min_y), min(a.max_y, b.max_y), *near_y)
    return BBox(x_span[0], y_span[0], x_span[1], y_span[1])


def _grown(box: BBox | None, by: float) -> BBox | None:
    """Grow a box, or report that the growth would collapse it."""
    if box is None or not by:
        return box
    try:
        return box.inflate(by)
    except ValueError:
        return None


def colliding_pairs(
    items: Sequence[CollisionItem],
    *,
    margin: float = 0.0,
    clearance: float = 0.0,
    ignore_same_owner: bool = False,
) -> list[Collision]:
    """Every pair that overlaps, or that sits closer than ``clearance``.

    ``margin`` grows each box before testing, so a positive margin reports items
    that are too close and a negative one ignores overlap smaller than that.
    ``clearance`` is the same idea stated as a gap between two untouched boxes:
    half of it goes on each side to decide which pairs are worth measuring, and
    the exact distance then decides. That way a 0.2mm clearance means 0.2mm,
    where a 0.2mm margin would mean 0.1mm of real gap.

    ``ignore_same_owner`` drops pairs belonging to one component, so a symbol's
    own reference and value text are not reported against each other.
    """
    ordered = sorted(items, key=lambda item: item.bbox.min_x)
    # Each box is measured once with the margin on it, and once more, grown by
    # half the clearance, purely so the sweep still reaches a pair that is close
    # but not touching. Boxes a negative growth would collapse are dropped, since
    # they can no longer collide with anything.
    reach: list[tuple[CollisionItem, BBox, BBox]] = []
    for item in ordered:
        box = item.inflated(margin)
        searched = _grown(box, clearance / 2.0)
        if box is not None and searched is not None:
            reach.append((item, box, searched))

    found: list[Collision] = []
    reported: set[tuple[int, int]] = set()
    for index, (item, box, searched) in enumerate(reach):
        for other, other_box, other_searched in reach[index + 1 :]:
            if other_searched.min_x >= searched.max_x:
                break
            if ignore_same_owner and item.owner is not None and item.owner == other.owner:
                continue
            if item.group is not None and item.group is other.group:
                continue
            pair = _pair_key(item, other)
            if pair in reported:
                continue
            area = _overlap(box, other_box)
            if area is not None:
                found.append(Collision(item, other, area))
                reported.add(pair)
                continue
            gap = box.clearance(other_box)
            if gap < clearance:
                found.append(Collision(item, other, _touching(box, other_box), gap))
                reported.add(pair)
    return found


def _pair_key(item: CollisionItem, other: CollisionItem) -> tuple[int, int]:
    first = id(item.group) if item.group is not None else id(item)
    second = id(other.group) if other.group is not None else id(other)
    return (min(first, second), max(first, second))


def cross_kind(collisions: Iterable[Collision], first: str, second: str) -> list[Collision]:
    """Keep only the collisions between two different kinds of item."""
    return [
        c for c in collisions if (c.first.kind, c.second.kind) in ((first, second), (second, first))
    ]
