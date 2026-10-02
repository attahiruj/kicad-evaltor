"""Text extents for KiCad's stroke font.

KiCad does not publish its stroke font metrics, so they are measured from the
installed KiCad by ``tools/calibrate_stroke_font.py`` and committed as JSON. That
keeps this module dependency-free at runtime: nothing here shells out, and the
numbers correspond exactly to a real KiCad build.

Every stored value is in *em units*, meaning multiples of the font size, except
``text_box_intercept_mm``, which is an absolute length because it comes from the
stroke thickness rather than from the glyph outlines.

The conventions, all confirmed against the calibration sheet, are:

* ``y`` grows downward, matching the schematic file, with the pen origin on the
  text baseline and ``y`` increasing upward from the anchor
* left/bottom justification puts the pen origin at the anchor
* right justification shifts by the text box width, which is the sum of the
  advances plus one stroke thickness
* top justification shifts by one line height
* an unjustified item is centred on its anchor in both axes
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_evaltor.geometry import BBox, Placement

_DATA_FILE = Path(__file__).with_name("_stroke_font_data.json")


@dataclass(frozen=True)
class Glyph:
    """One glyph's ink and advance, in em units relative to the pen origin."""

    advance: float
    x0: float
    y_top: float
    x1: float
    y_bottom: float

    @property
    def has_ink(self) -> bool:
        return self.x1 > self.x0 or self.y_top > self.y_bottom


@lru_cache(maxsize=1)
def _data() -> Any:
    """The calibration file.

    Typed as ``Any`` on purpose: the shape is validated once by the calibration
    tool, and annotating it here would mean restating the whole document for no
    safety that the loader does not already provide.
    """
    if not _DATA_FILE.exists():
        raise FileNotFoundError(
            f"stroke font metrics missing: {_DATA_FILE}. "
            "Regenerate with tools/calibrate_stroke_font.py"
        )
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def source_kicad_version() -> str:
    return str(_data().get("kicad_version", "unknown"))


@lru_cache(maxsize=1)
def _glyphs() -> dict[str, Glyph]:
    raw = _data()["glyphs"]
    return {
        char: Glyph(
            advance=float(entry["advance"]),
            x0=float(entry["x0"]),
            y_top=float(entry["y_top"]),
            x1=float(entry["x1"]),
            y_bottom=float(entry["y_bottom"]),
        )
        for char, entry in raw.items()
    }


@lru_cache(maxsize=256)
def glyph(char: str) -> Glyph:
    """Metrics for a single character, falling back to a wide box off the font."""
    known = _glyphs().get(char)
    if known is not None:
        return known
    return Glyph(advance=1.0, x0=0.1, y_top=1.0, x1=0.9, y_bottom=0.0)


def supported(char: str) -> bool:
    return char in _glyphs()


@lru_cache(maxsize=1)
def _text_box() -> tuple[float, float]:
    box = _data()["text_box"]
    return float(box["slope"]), float(box["intercept_mm"])


@lru_cache(maxsize=1)
def line_height(size: float) -> float:
    """Distance between baselines, in millimetres."""
    return float(_data()["line_height_em"]) * size


def advance_width(content: str, size: float) -> float:
    """Total pen advance across the string, in millimetres."""
    return sum(glyph(c).advance for c in content) * size


def text_box_width(content: str, size: float) -> float:
    """The width KiCad justifies against, in millimetres."""
    slope, intercept = _text_box()
    return slope * advance_width(content, size) + intercept


# KiCad's stroke font sets capital letters exactly one em tall. The calibration
# measures ink relative to the probe's anchor rather than to the baseline, so
# every glyph arrives displaced by the same amount; anchoring on "M" recovers
# the baseline without a hand-fitted constant.
CAP_HEIGHT_EM = 1.0


@lru_cache(maxsize=1)
def _baseline_shift_em() -> float:
    return glyph("M").y_top - CAP_HEIGHT_EM


@lru_cache(maxsize=1)
def baseline_offset_em() -> float:
    """Distance from a vertically centred anchor down to the text baseline, in em."""
    return float(_data().get("baseline_offset_em", 0.456299))


def baseline_y(anchor_y: float, size: float) -> float:
    """The y coordinate of the baseline for text centred on ``anchor_y``."""
    return anchor_y + baseline_offset_em() * size


def text_extents(content: str, size: float) -> tuple[float, float, float, float]:
    """Ink extents as ``(x0, y_top, x1, y_bottom)`` in millimetres.

    Vertical values are relative to the **baseline** with y increasing upward,
    and horizontal values to the pen origin. A string with no ink at all, such
    as spaces, still reports its box.
    """
    shift = _baseline_shift_em()
    pen = 0.0
    ink: list[tuple[float, float, float, float]] = []
    for char in content:
        g = glyph(char)
        if g.has_ink:
            ink.append((pen + g.x0, g.y_top - shift, pen + g.x1, g.y_bottom - shift))
        pen += g.advance

    if not ink:
        return (0.0, 0.0, advance_width(content, size), 0.0)

    return (
        min(i[0] for i in ink) * size,
        max(i[1] for i in ink) * size,
        max(i[2] for i in ink) * size,
        min(i[3] for i in ink) * size,
    )


def text_bbox(
    content: str,
    size: float,
    placement: Placement | None = None,
    justify_h: str | None = None,
    justify_v: str | None = None,
) -> BBox:
    """Ink bounding box of a text run in sheet coordinates.

    Vertical placement is anchored on the baseline. KiCad centres a field's *line
    cell* rather than its ink, so centring the ink instead would shift text with
    descenders by a third of an em and report overlaps that are not in the sheet.

    ``justify_h`` is ``left``, ``right``, ``center`` or None for KiCad's
    centred default; ``justify_v`` is ``top``, ``bottom``, ``middle`` or None.
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")

    x0, y_top, x1, y_bottom = text_extents(content, size)
    box_width = text_box_width(content, size)

    horizontal = (justify_h or "center").lower()
    # KiCad anchors both `left` and `right` field text at the left edge of the
    # text cell; only the default centres.
    if horizontal in ("left", "right"):
        dx = 0.0
    elif horizontal in ("center", "centre", "middle"):
        dx = -box_width / 2.0
    else:
        raise ValueError(f"unknown horizontal justification {justify_h!r}")

    # Everything is placed from the baseline, because that is the only point
    # KiCad actually pins. Sheet y grows downward, so a glyph extent measured
    # upward from the baseline (y_top, with y_bottom <= 0) is negated here.
    vertical = (justify_v or "middle").lower()
    if vertical in ("middle", "center", "centre"):
        baseline = baseline_y(0.0, size)
    elif vertical == "top":
        baseline = y_top
    elif vertical == "bottom":
        baseline = y_bottom
    else:
        raise ValueError(f"unknown vertical justification {justify_v!r}")

    local = BBox(x0 + dx, baseline - y_top, x1 + dx, baseline - y_bottom)
    return placement.apply_box(local) if placement is not None else local
