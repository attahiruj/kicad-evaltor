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
* right justification shifts by the text box width, which is the sum of the
  advances plus one stroke thickness
* top justification shifts by one line height
* an unjustified item is centred on its anchor in both axes

Those three shifts are all explained by one model: KiCad justifies the *line
cell*, a box as wide as the advances plus one stroke thickness and
``line_height`` tall, and the ink sits inside that cell wherever its glyph
bearings put it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_evaltor.geometry import BBox, Point

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


# KiCad stores characters that would break a net name or a quoted string as
# ``{name}`` escapes and draws the character itself. Read back from kicad-cli: a
# name not in this table is drawn literally, braces and all.
_ESCAPES = {
    "dblquote": '"',
    "quote": "'",
    "lt": "<",
    "gt": ">",
    "backslash": "\\",
    "slash": "/",
    "bar": "|",
    "colon": ":",
    "comma": ",",
    "space": " ",
    "dollar": "$",
    "tab": "\t",
    "return": "\n",
    "brace": "{",
}
_ESCAPE = re.compile(r"\{(" + "|".join(_ESCAPES) + r")\}")


def unescape(text: str) -> str:
    """The text KiCad draws for a string as the file stores it."""
    return _ESCAPE.sub(lambda m: _ESCAPES[m.group(1)], text)


# Formatting markup, measured from kicad-cli in em at two sizes: ``_{...}`` and
# ``^{...}`` draw at four fifths of the size, dropped or raised from the
# baseline, and ``~{...}`` draws a bar a fixed height above the capitals that
# runs from just after where the run starts to just before where it ends.
_SCRIPT_SCALE = 0.8
_SUBSCRIPT_DROP_EM = 0.1105
_SUPERSCRIPT_RISE_EM = 0.2895
_OVERBAR_EM = 1.2775
_OVERBAR_START_EM = 0.1395
_OVERBAR_END_EM = 0.0609
_MARKUP = {"_": "sub", "^": "super", "~": "overbar"}


def _runs(content: str) -> list[tuple[str, str]]:
    """Split one line into ``(text, style)`` runs; style is ``""`` for plain."""
    runs: list[tuple[str, str]] = []
    plain: list[str] = []
    i = 0
    while i < len(content):
        style = _MARKUP.get(content[i])
        close = content.find("}", i + 2) if style and content[i + 1 : i + 2] == "{" else -1
        if style and close != -1:
            if plain:
                runs.append(("".join(plain), ""))
                plain = []
            runs.append((content[i + 2 : close], style))
            i = close + 1
            continue
        plain.append(content[i])
        i += 1
    if plain:
        runs.append(("".join(plain), ""))
    return runs


def _scale(style: str) -> float:
    return _SCRIPT_SCALE if style in ("sub", "super") else 1.0


def advance_width(content: str, size: float) -> float:
    """Total pen advance across one line of drawn text, in millimetres."""
    return (
        sum(_scale(style) * glyph(c).advance for text, style in _runs(content) for c in text) * size
    )


def text_box_width(content: str, size: float) -> float:
    """The width KiCad justifies against, in millimetres."""
    slope, intercept = _text_box()
    return slope * advance_width(content, size) + intercept


# KiCad's stroke font sets capital letters exactly one em tall. The calibration
# measures ink relative to the probe's anchor rather than to the baseline, so
# every glyph arrives displaced by the same amount; anchoring on "M" recovers
# the baseline without a hand-fitted constant. The same displacement is the room
# KiCad leaves below the baseline inside the line cell, which is how far the cell
# has to reach up from its bottom edge to reach the baseline.
_CAP_HEIGHT_EM = 1.0


@lru_cache(maxsize=1)
def descent_em() -> float:
    """Distance from the bottom of the line cell up to the baseline, in em."""
    return glyph("M").y_top - _CAP_HEIGHT_EM


def text_extents(content: str, size: float) -> tuple[float, float, float, float]:
    """Ink extents as ``(x0, y_top, x1, y_bottom)`` in millimetres.

    Vertical values are relative to the **baseline** with y increasing upward,
    and horizontal values to the pen origin. A string with no ink at all, such
    as spaces, still reports its box. ``content`` is one line of drawn text,
    formatting markup included; see ``unescape`` for the file's escapes.
    """
    shift = descent_em()
    pen = 0.0
    ink: list[tuple[float, float, float, float]] = []
    for text, style in _runs(content):
        scale = _scale(style)
        raise_by = {"sub": -_SUBSCRIPT_DROP_EM, "super": _SUPERSCRIPT_RISE_EM}.get(style, 0.0)
        start = pen
        for char in text:
            g = glyph(char)
            if g.has_ink:
                ink.append(
                    (
                        pen + scale * g.x0,
                        scale * (g.y_top - shift) + raise_by,
                        pen + scale * g.x1,
                        scale * (g.y_bottom - shift) + raise_by,
                    )
                )
            pen += scale * g.advance
        if style == "overbar" and text:
            ink.append((start + _OVERBAR_START_EM, _OVERBAR_EM, pen - _OVERBAR_END_EM, _OVERBAR_EM))

    if not ink:
        return (0.0, 0.0, advance_width(content, size), 0.0)

    return (
        min(i[0] for i in ink) * size,
        max(i[1] for i in ink) * size,
        max(i[2] for i in ink) * size,
        min(i[3] for i in ink) * size,
    )


def _local_cell(
    content: str,
    size: float,
    justify_h: str | None,
    justify_v: str | None,
) -> BBox:
    """The line cell for a run of text, anchored on the origin."""
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")

    width = text_box_width(content, size)
    height = line_height(size)

    horizontal = (justify_h or "center").lower()
    if horizontal == "left":
        left = 0.0
    elif horizontal == "right":
        left = -width
    elif horizontal in ("center", "centre", "middle"):
        left = -width / 2.0
    else:
        raise ValueError(f"unknown horizontal justification {justify_h!r}")

    # Sheet y grows downward, so the cell's top edge is the smaller y. Every
    # other value is measured from the same anchor KiCad records for the item.
    vertical = (justify_v or "middle").lower()
    if vertical == "top":
        top = 0.0
    elif vertical == "bottom":
        top = -height
    elif vertical in ("middle", "center", "centre"):
        top = -height / 2.0
    else:
        raise ValueError(f"unknown vertical justification {justify_v!r}")

    return BBox(left, top, left + width, top + height)


def _translated(box: BBox, at: Point | None) -> BBox:
    if at is None:
        return box
    return BBox(
        box.min_x + at[0],
        box.min_y + at[1],
        box.max_x + at[0],
        box.max_y + at[1],
    )


def text_cell_bbox(
    content: str,
    size: float,
    at: Point | None = None,
    justify_h: str | None = None,
    justify_v: str | None = None,
) -> BBox:
    """The box KiCad reserves for a run of text, in sheet coordinates.

    This is the line cell: as wide as the advances plus one stroke thickness, and
    ``line_height`` tall. Justification moves the cell, not the ink, which is why
    an unjustified item's ink sits wherever its glyph bearings put it inside a
    centred cell.

    The cell is for text read left to right. ``at`` is the sheet anchor; turning
    the cell for vertical text, or for a field on a rotated symbol, is the
    caller's job, because the rules differ between fields and labels.

    ``justify_h`` is ``left``, ``right``, ``center`` or None for KiCad's
    centred default; ``justify_v`` is ``top``, ``bottom``, ``middle`` or None.
    """
    return _translated(_local_cell(content, size, justify_h, justify_v), at)


def text_bbox(
    content: str,
    size: float,
    at: Point | None = None,
    justify_h: str | None = None,
    justify_v: str | None = None,
) -> BBox:
    """Ink bounding box of a text run in sheet coordinates.

    The ink is placed inside the line cell ``text_cell_bbox`` returns, which is
    what KiCad justifies. That matters most for vertical placement: a
    top-justified run starts one line height lower than its ink top suggests,
    because the cell's top is what the anchor pins.

    ``at``, ``justify_h`` and ``justify_v`` mean what they mean in
    ``text_cell_bbox``.
    """
    cell = _local_cell(content, size, justify_h, justify_v)
    x0, y_top, x1, y_bottom = text_extents(content, size)
    baseline = cell.max_y - descent_em() * size
    return _translated(
        BBox(cell.min_x + x0, baseline - y_top, cell.min_x + x1, baseline - y_bottom), at
    )
