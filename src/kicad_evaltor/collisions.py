"""Pairwise overlap detection over axis-aligned boxes.

All the layout checks reduce to the same question -- "do any two of these boxes
overlap, and by how much" -- so they share one engine rather than each rolling
its own pairwise loop. The engine is a sweep along x: items are sorted by their
left edge, and each item is compared only against those that start before it
ends. That is near-linear for a typical sheet and, unlike a grid, needs no cell
size to be tuned to the data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from kicad_evaltor.geometry import BBox


@dataclass(frozen=True)
class CollisionItem:
    """Something that can collide: a labelled box plus what kind of thing it is."""

    kind: str
    label: str
    bbox: BBox
    owner: str | None = None

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
    """Two items whose boxes overlap, and the region they share."""

    first: CollisionItem
    second: CollisionItem
    area: BBox

    @property
    def width(self) -> float:
        return self.area.width

    @property
    def height(self) -> float:
        return self.area.height

    def describe(self) -> str:
        return f"{self.first.label} overlaps {self.second.label}"

    def as_dict(self) -> dict[str, object]:
        return {
            "first": self.first.label,
            "first_kind": self.first.kind,
            "second": self.second.label,
            "second_kind": self.second.kind,
            "area": [self.area.min_x, self.area.min_y, self.area.max_x, self.area.max_y],
            "width": self.width,
            "height": self.height,
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


def colliding_pairs(
    items: Sequence[CollisionItem],
    *,
    margin: float = 0.0,
    ignore_same_owner: bool = False,
) -> list[Collision]:
    """Every overlapping pair among ``items``.

    ``margin`` grows each box before testing, which is how the layout checks
    express a required gap rather than strict intersection. ``ignore_same_owner``
    drops pairs belonging to one component, so a symbol's own reference and value
    text are not reported against each other.
    """
    ordered = sorted(items, key=lambda item: item.bbox.min_x)
    # Inflate once, up front: the sweep's early exit is only sound if it
    # compares grown boxes, because two grown boxes can meet across a gap that
    # the raw boxes left between them. Boxes that a negative margin collapses
    # are dropped, since they can no longer overlap anything.
    grown = [(item, box) for item in ordered if (box := item.inflated(margin)) is not None]

    found: list[Collision] = []
    for index, (item, left) in enumerate(grown):
        for other, right in grown[index + 1 :]:
            if right.min_x >= left.max_x:
                break
            if ignore_same_owner and item.owner is not None and item.owner == other.owner:
                continue
            area = _overlap(left, right)
            if area is not None:
                found.append(Collision(item, other, area))
    return found


def cross_kind(collisions: Iterable[Collision], first: str, second: str) -> list[Collision]:
    """Keep only the collisions between two different kinds of item."""
    return [
        c for c in collisions if (c.first.kind, c.second.kind) in ((first, second), (second, first))
    ]
