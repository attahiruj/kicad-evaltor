"""Where KiCad puts the ink of a text item, and the outline it draws around a label.

``font_metrics`` knows the stroke font: how wide each glyph is and where its ink
sits relative to the pen. It does not know where each kind of schematic item puts
that pen. KiCad lifts a net label off its wire, pushes a global label's text past
its flag, centres a field inside a box it then turns with the symbol, and draws
every stroke with a pen whose width depends on the font settings. None of that
is published, so it is measured from the installed ``kicad-cli`` by
``tools/calibrate_text_placement.py`` and committed as JSON, like the font.

The measurement is a correction on top of the font model. For every kind of text,
label shape, orientation and justification it records how far KiCad's drawn ink
sits from where ``font_metrics.text_bbox`` puts it. Each correction, and each edge
of a label outline, was found to be linear in the text size, the two pen widths
(see ``Pen``) and the text box width, so it is stored as the coefficients of

    value = a + b * size + c * stroke + d * layout + e * width

with every length in millimetres. The calibration checks the fit against sizes,
pens and strings it did not fit on.

Everything here works in the text's own frame: the anchor at the origin, the
text reading left to right, ``y`` down. Turning the result onto the sheet is
``schematic_items``' job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from kicad_evaltor.font_metrics import text_bbox, text_box_width, unescape
from kicad_evaltor.geometry import BBox, Point

_DATA_FILE = Path(__file__).with_name("_text_placement_data.json")

# KiCad draws text whose font sets no thickness with the schematic's default line
# width, whatever the size, yet parts of its layout use a pen an eighth of the
# size instead. Bold text uses a fifth of the size for both, and an explicit
# thickness is capped at a quarter of it. The drawn widths were read back from
# the strokes kicad-cli writes, the layout width from how far text moves as the
# pen changes.
DEFAULT_PEN = 0.1524
DEFAULT_LAYOUT_PEN_EM = 0.125
BOLD_PEN_EM = 0.2
MAX_PEN_EM = 0.25

# The kinds of text the calibration covers. A field belongs to a symbol; the rest
# stand on the sheet.
FIELD = "field"
# Baseline to baseline between the lines of multi-line text, measured from
# kicad-cli at two sizes.
LINE_PITCH_EM = 1.61
# A field is also measured on a symbol mirrored each way; see ``field_parts``.
MIRROR_X = "mirror_x"
MIRROR_Y = "mirror_y"
FIELD_VARIANTS = ("", MIRROR_X, MIRROR_Y)
KINDS = (FIELD, "text", "label", "global_label", "hierarchical_label")
LABEL_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")


@dataclass(frozen=True)
class Pen:
    """The two pen widths KiCad uses for a run of text, in millimetres.

    ``stroke`` is what it draws with. ``layout`` is the width some of its
    placement rules use instead; which rules is left to the calibration, which
    fits each correction against both. They differ only for text that sets no
    thickness of its own.
    """

    stroke: float
    layout: float

    @classmethod
    def for_font(cls, size: float, thickness: float = 0.0, bold: bool = False) -> Pen:
        if thickness > 0:
            pen = min(thickness, MAX_PEN_EM * size)
            return cls(pen, pen)
        if bold:
            return cls(BOLD_PEN_EM * size, BOLD_PEN_EM * size)
        return cls(DEFAULT_PEN, DEFAULT_LAYOUT_PEN_EM * size)


@dataclass(frozen=True)
class Fit:
    """``a + b * size + c * stroke + d * layout + e * width``, in millimetres."""

    a: float
    b: float
    c: float
    d: float
    e: float

    def __call__(self, size: float, pen: Pen, width: float) -> float:
        return self.a + self.b * size + self.c * pen.stroke + self.d * pen.layout + self.e * width

    @classmethod
    def from_list(cls, values: list[float]) -> Fit:
        a, b, c, d, e = (float(v) for v in values)
        return cls(a, b, c, d, e)


_ZERO = Fit(0.0, 0.0, 0.0, 0.0, 0.0)


def key(kind: str, shape: str, draw: int, h: str, v: str) -> str:
    """The table key for one kind, label shape, orientation and justification."""
    return f"{kind}/{shape}/{draw}/{h}/{v}"


def outline_key(kind: str, shape: str, h: str) -> str:
    return f"{kind}/{shape}/{h}"


@lru_cache(maxsize=1)
def _data() -> Any:
    if not _DATA_FILE.exists():
        raise FileNotFoundError(
            f"text placement data missing: {_DATA_FILE}. "
            "Regenerate with tools/calibrate_text_placement.py"
        )
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _text_fits() -> dict[str, tuple[Fit, Fit]]:
    return {
        k: (Fit.from_list(entry["dx"]), Fit.from_list(entry["dy"]))
        for k, entry in _data()["text"].items()
    }


@lru_cache(maxsize=1)
def _outline_fits() -> dict[str, tuple[Fit, Fit, Fit, Fit]]:
    return {
        k: tuple(Fit.from_list(entry[edge]) for edge in ("x0", "y0", "x1", "y1"))  # type: ignore[misc]
        for k, entry in _data()["outline"].items()
    }


def _shape(kind: str, shape: str | None) -> str:
    if kind in ("global_label", "hierarchical_label"):
        return shape if shape in LABEL_SHAPES else "input"
    if kind == FIELD and shape in FIELD_VARIANTS:
        return shape
    return ""


def _draw(draw: float) -> int:
    return 90 if round(draw) % 180 == 90 else 0


def _line_ink(
    line: str,
    size: float,
    pen: Pen,
    kind: str,
    shape: str | None,
    draw: float,
    justify_h: str,
    justify_v: str,
) -> BBox:
    model = text_bbox(line, size, None, justify_h, justify_v)
    width = text_box_width(line, size)
    dx, dy = _text_fits().get(
        key(kind, _shape(kind, shape), _draw(draw), justify_h, justify_v), (_ZERO, _ZERO)
    )
    shift_x, shift_y = dx(size, pen, width), dy(size, pen, width)
    half = pen.stroke / 2.0
    return BBox(
        model.min_x + shift_x - half,
        model.min_y + shift_y - half,
        model.max_x + shift_x + half,
        model.max_y + shift_y + half,
    )


def ink(
    content: str,
    size: float,
    pen: Pen,
    kind: str,
    shape: str | None = None,
    draw: float = 0.0,
    justify_h: str = "center",
    justify_v: str = "center",
) -> BBox:
    """The drawn extent of a run of text in its own frame, stroke included.

    ``content`` is the text as the file stores it: its ``{slash}``-style escapes
    are undone here, and a newline starts another line. Each line is justified
    on its own across the page; down the page the lines move as a block, with
    ``top`` pinning the first line where a single line would sit, ``bottom`` the
    last, and the default centring them.

    ``draw`` is the orientation it is drawn at, 0 or 90, which picks the
    correction: KiCad does not place vertical free text quite where it places
    horizontal text. The box itself is still returned reading left to right.
    """
    lines = unescape(content).split("\n")
    # One trailing newline ends the last line rather than opening another; a
    # second one, and any blank line before it, does take up a line.
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    anchor_line = {"top": 0.0, "bottom": len(lines) - 1.0}.get(justify_v, (len(lines) - 1) / 2.0)
    pitch = LINE_PITCH_EM * size
    boxes = []
    for index, line in enumerate(lines):
        if not line.strip() and len(lines) > 1:
            continue
        box = _line_ink(line, size, pen, kind, shape, draw, justify_h, justify_v)
        drop = (index - anchor_line) * pitch
        boxes.append(BBox(box.min_x, box.min_y + drop, box.max_x, box.max_y + drop))
    whole = BBox.union_all(boxes)
    if whole is None:
        return _line_ink("", size, pen, kind, shape, draw, justify_h, justify_v)
    return whole


def _centre(box: BBox) -> Point:
    return ((box.min_x + box.max_x) / 2.0, (box.min_y + box.max_y) / 2.0)


@dataclass(frozen=True)
class FieldParts:
    """A field split the way KiCad places it.

    KiCad justifies a field in the field's own frame and carries that box
    through the field's angle and the symbol's rotation and mirror, then draws
    the letters the right way up, centred on where the box landed but for a
    small offset of its own. So the parts move differently:

    * ``centre``, in the field's own frame, turns with the field and the symbol
    * ``offset`` and ``extent``, in the drawn frame, turn only with the angle the
      letters are drawn at, which a mirror does not change

    ``extent`` is the ink box around its own centre.
    """

    centre: Point
    offset: Point
    extent: BBox


def field_parts(
    content: str, size: float, pen: Pen, justify_h: str, justify_v: str, draw: float
) -> FieldParts:
    """Separate a field's box centre from its ink, at orientation ``draw``.

    One rendering shows only their sum. A mirror flips the box centre and leaves
    the ink offset alone, so across the mirrored axis half the difference of the
    plain and mirrored measurements is the centre and half the sum the offset.
    Mirroring about the sheet's x axis flips the field's own y when it is drawn
    flat and its own x when it is drawn vertical; ``(mirror y)`` the other one.
    """
    plain = ink(content, size, pen, FIELD, None, draw, justify_h, justify_v)
    flip_x = ink(content, size, pen, FIELD, MIRROR_X, draw, justify_h, justify_v)
    flip_y = ink(content, size, pen, FIELD, MIRROR_Y, draw, justify_h, justify_v)
    (px, py) = _centre(plain)
    vertical = _draw(draw) == 90
    across_x = _centre(flip_x if vertical else flip_y)[0]
    across_y = _centre(flip_y if vertical else flip_x)[1]
    centre = ((px - across_x) / 2.0, (py - across_y) / 2.0)
    offset = ((px + across_x) / 2.0, (py + across_y) / 2.0)
    extent = BBox(plain.min_x - px, plain.min_y - py, plain.max_x - px, plain.max_y - py)
    return FieldParts(centre, offset, extent)


def outline(
    kind: str,
    shape: str | None,
    content: str,
    size: float,
    pen: Pen,
    justify_h: str = "left",
) -> BBox | None:
    """The flag KiCad draws for a global or hierarchical label, in its own frame.

    A global label's outline grows with its text; a hierarchical label's is a
    small fixed arrow at the anchor. Other kinds draw none.
    """
    if kind not in ("global_label", "hierarchical_label"):
        return None
    fits = _outline_fits().get(outline_key(kind, _shape(kind, shape), justify_h))
    if fits is None:
        return None
    width = text_box_width(content, size)
    x0, y0, x1, y1 = (fit(size, pen, width) for fit in fits)
    return BBox(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


@lru_cache(maxsize=1)
def source_kicad_version() -> str:
    return str(_data().get("kicad_version", "unknown"))
