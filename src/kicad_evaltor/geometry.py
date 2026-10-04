"""Axis-aligned boxes and the placement transform KiCad schematics use.

Everything here works in the coordinate space of a ``.kicad_sch`` file: x to
the right, y downward, millimetres.

The conventions, all measured from the installed KiCad by rendering the demo
sheets and reading back the drawn pin lines, so they are observed rather than
assumed:

* symbol geometry is authored with library-local ``y`` growing **upwards**,
  while the sheet's grows downwards, so every local ``y`` is negated on the way
  out: a pin at local ``y = -2.54`` lands ``2.54 mm`` *below* its anchor
* the ``(at x y angle)`` angle is counter-clockwise **on screen**, which is
  clockwise in a y-down frame, so the applied rotation is ``-angle``
* a pin's ``at`` is its connection point and the pin runs from there toward the
  body along its own angle
* ``(mirror x)`` and ``(mirror y)`` are applied to the library-local point, before
  the frame conversion and the rotation
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

Point = tuple[float, float]

_ORTHOGONAL = {0.0: (1.0, 0.0), 90.0: (0.0, 1.0), 180.0: (-1.0, 0.0), 270.0: (0.0, -1.0)}


def _finite(*values: float) -> None:
    for value in values:
        if not math.isfinite(value):
            raise ValueError(f"coordinate must be finite, got {value!r}")


def normalize_angle(angle: float) -> float:
    """Fold an angle into [0, 360)."""
    return angle % 360.0


def rotate_point(x: float, y: float, angle: float) -> Point:
    """Rotate a point about the origin by ``angle`` degrees.

    Multiples of 90 degrees take an exact path so repeated placement of
    grid-aligned geometry does not accumulate floating point drift.
    """
    _finite(x, y, angle)
    exact = _ORTHOGONAL.get(normalize_angle(angle))
    if exact is not None:
        cos_a, sin_a = exact
        return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)

    radians = math.radians(angle)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)


@dataclass(frozen=True)
class Placement:
    """Where a symbol sits, and how its library-local axes reach the sheet.

    ``mirror`` follows the schematic file's spelling: ``"x"`` reflects the
    library-local geometry about its x axis, ``"y"`` about its y axis, both
    before the frame conversion and the rotation.
    """

    x: float
    y: float
    rotation: float = 0.0
    mirror: str | None = None

    def __post_init__(self) -> None:
        _finite(self.x, self.y, self.rotation)
        if self.mirror not in (None, "x", "y"):
            raise ValueError(f"mirror must be 'x', 'y' or None, got {self.mirror!r}")

    def apply(self, x: float, y: float) -> Point:
        """Map a library-local point into sheet coordinates."""
        rx, ry = self.apply_direction(x, y)
        return (self.x + rx, self.y + ry)

    def apply_direction(self, dx: float, dy: float) -> Point:
        """Map a library-local direction into sheet coordinates.

        The same mirror, frame conversion and rotation ``apply`` does, without the
        translation, for the cases where only the heading matters.
        """
        if self.mirror == "x":
            dy = -dy
        elif self.mirror == "y":
            dx = -dx
        # Library y grows upwards and the file's angle is counter-clockwise on
        # screen, so the heading is flipped before a negated rotation is applied.
        return rotate_point(dx, -dy, -self.rotation)

    def apply_box(self, box: BBox) -> BBox:
        """Map a library-local box, which must be axis-aligned, into sheet coordinates.

        A box rotated by anything other than a multiple of 90 degrees is no
        longer axis-aligned, so this returns the bounding box of the four
        transformed corners and is therefore conservative.
        """
        corners = [
            self.apply(box.min_x, box.min_y),
            self.apply(box.min_x, box.max_y),
            self.apply(box.max_x, box.min_y),
            self.apply(box.max_x, box.max_y),
        ]
        return BBox.from_points(corners)


@dataclass(frozen=True)
class BBox:
    """An axis-aligned rectangle in millimetres."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        _finite(self.min_x, self.min_y, self.max_x, self.max_y)
        if self.min_x > self.max_x:
            raise ValueError(f"min_x {self.min_x} exceeds max_x {self.max_x}")
        if self.min_y > self.max_y:
            raise ValueError(f"min_y {self.min_y} exceeds max_y {self.max_y}")

    @classmethod
    def from_points(cls, points: Iterable[Point]) -> BBox:
        materialised = list(points)
        if not materialised:
            raise ValueError("cannot build a BBox from no points")
        xs = [p[0] for p in materialised]
        ys = [p[1] for p in materialised]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @classmethod
    def union_all(cls, boxes: Iterable[BBox]) -> BBox | None:
        corners: list[Point] = []
        for box in boxes:
            corners.extend(((box.min_x, box.min_y), (box.max_x, box.max_y)))
        return cls.from_points(corners) if corners else None

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return ((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    @property
    def area(self) -> float:
        return self.width * self.height

    def union(self, other: BBox) -> BBox:
        return BBox(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def intersection(self, other: BBox) -> BBox | None:
        """The shared area, or None when the boxes do not overlap."""
        min_x = max(self.min_x, other.min_x)
        min_y = max(self.min_y, other.min_y)
        max_x = min(self.max_x, other.max_x)
        max_y = min(self.max_y, other.max_y)
        if min_x >= max_x or min_y >= max_y:
            return None
        return BBox(min_x, min_y, max_x, max_y)

    def intersects(self, other: BBox) -> bool:
        return self.intersection(other) is not None

    def clearance(self, other: BBox) -> float:
        """Shortest distance between the boxes; 0.0 when they touch or overlap.

        Touching counts as a clearance of zero, so a caller asking for a
        positive minimum clearance rejects contact as well as overlap. Two boxes
        separated on both axes are their diagonal apart, not the smaller of the
        two gaps, so the distance is the hypotenuse.
        """
        gap_x = max(0.0, max(self.min_x, other.min_x) - min(self.max_x, other.max_x))
        gap_y = max(0.0, max(self.min_y, other.min_y) - min(self.max_y, other.max_y))
        return math.hypot(gap_x, gap_y)

    def inflate(self, margin: float) -> BBox:
        """Grow (or, with a negative margin, shrink) the box on all four sides.

        A negative margin is the escape hatch for approximations: body outlines
        sampled from a quadratic bulge and pins modelled as bare lines both run
        slightly large, so callers that want to ignore sub-millimeter overlap
        shrink the boxes rather than treating every measured overlap as real.
        Shrinking past the point of collapse raises, via the bounds check.
        """
        return BBox(
            self.min_x - margin,
            self.min_y - margin,
            self.max_x + margin,
            self.max_y + margin,
        )

    def contains_point(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def contains(self, other: BBox) -> bool:
        """True when ``other`` lies entirely within this box."""
        return (
            self.min_x <= other.min_x
            and self.min_y <= other.min_y
            and self.max_x >= other.max_x
            and self.max_y >= other.max_y
        )
