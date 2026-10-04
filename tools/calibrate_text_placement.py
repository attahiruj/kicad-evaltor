"""Measure where KiCad draws each kind of schematic text, by rendering probe sheets.

``font_metrics`` predicts a run of text's ink from the stroke font alone. KiCad
then moves that ink by an amount that depends on what the text belongs to: a net
label sits clear of its wire, a global label's text starts past its flag, a
symbol field is centred in a box KiCad justifies itself. This tool measures that
correction, and the outline drawn around global and hierarchical labels, against
the installed ``kicad-cli``.

Every probe is a single text item on an otherwise empty sheet. The sheet is
exported to SVG, where kicad-cli wraps each run of text in its own
``<g class="stroked-text">`` group and draws label outlines as plain paths, so
each probe's text ink and outline are read back separately, stroke width
included.

Each quantity was found to be linear in the text size, the two pens KiCad uses
and the text box width, so it is fitted exactly from five probe sets that pull
those apart, and then checked against held-out sets that vary them together.
The check is printed, and the run fails if any held-out edge misses by more than
``--tolerance``.

Regenerate the committed table with::

    python tools/calibrate_text_placement.py --out src/kicad_evaltor/_text_placement_data.json

Run it after ``calibrate_stroke_font.py``: the corrections are measured against
the font model, so a new font table needs new corrections.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from calibrate_stroke_font import find_cli, kicad_version

from kicad_evaltor.font_metrics import text_bbox, text_box_width
from kicad_evaltor.text_placement import (
    FIELD,
    FIELD_VARIANTS,
    LABEL_SHAPES,
    Fit,
    Pen,
    key,
    outline_key,
)

JUSTIFY_H = ("left", "center", "right")
JUSTIFY_V = ("top", "center", "bottom")
DRAWS = (0, 90)
PITCH_X = 50.0
PITCH_Y = 40.0
COLUMNS = 22
# A probe owns the ink whose centre falls this close to its anchor.
WINDOW_X = 22.0
WINDOW_Y = 18.0


@dataclass(frozen=True)
class FontSet:
    """One probe set: every probe on the sheet drawn with these font settings."""

    size: float
    content: str
    thickness: float = 0.0
    bold: bool = False

    @property
    def pen(self) -> Pen:
        return Pen.for_font(self.size, self.thickness, self.bold)

    @property
    def width(self) -> float:
        return text_box_width(self.content, self.size)

    def font(self) -> str:
        extra = ""
        if self.thickness:
            extra += f" (thickness {self.thickness:g})"
        if self.bold:
            extra += " (bold yes)"
        return f"(font (size {self.size:g} {self.size:g}){extra})"


# Five sets for five unknowns. With no thickness set, the stroke is fixed and the
# layout pen follows the size, so those two sets alone cannot tell the pens from
# the size; two explicit thicknesses at different sizes, where both pens are
# equal, separate them. The last changes only the text.
FIT_SETS = (
    FontSet(1.27, "Mgy"),
    FontSet(2.54, "Mgy"),
    FontSet(1.27, "Mgy", thickness=0.3),
    FontSet(2.54, "Mgy", thickness=0.2),
    FontSet(1.27, "LONGER_NAME"),
)
# Sets the fit never sees, varying several inputs at once.
CHECK_SETS = (
    FontSet(1.0, "AB-c"),
    FontSet(1.524, "U2"),
    FontSet(1.27, "Mgy", bold=True),
    FontSet(2.54, "Net_42", thickness=0.25),
    FontSet(2.0, "x", thickness=0.25),
    FontSet(1.524, "LONG_NET_NAME", thickness=0.254),
    FontSet(2.54, "Mgy", bold=True),
    FontSet(1.27, "Mgy", thickness=0.5),
)


@dataclass(frozen=True)
class Probe:
    kind: str
    shape: str
    draw: int
    h: str
    v: str

    @property
    def key(self) -> str:
        return key(self.kind, self.shape, self.draw, self.h, self.v)


def probes() -> list[Probe]:
    out = []
    for kind in (FIELD, "text", "label", "global_label", "hierarchical_label"):
        if kind in ("global_label", "hierarchical_label"):
            shapes: tuple[str, ...] = LABEL_SHAPES
        elif kind == FIELD:
            shapes = FIELD_VARIANTS
        else:
            shapes = ("",)
        for shape, draw, h, v in itertools.product(shapes, DRAWS, JUSTIFY_H, JUSTIFY_V):
            out.append(Probe(kind, shape, draw, h, v))
    return out


def _justify(h: str, v: str) -> str:
    words = [w for w in (h, v) if w != "center"]
    return f"(justify {' '.join(words)})" if words else ""


def _node(probe: Probe, fonts: FontSet, x: float, y: float, sheet: str) -> str:
    effects = f"(effects {fonts.font()} {_justify(probe.h, probe.v)})"
    text = fonts.content
    if probe.kind == FIELD:
        # The probe symbol draws nothing, so the field is the only ink it owns. A
        # mirrored one moves the field's box but not its letters, which is what
        # tells the two apart.
        mirror = f"(mirror {probe.shape[-1]})" if probe.shape else ""
        return (
            f'(symbol (lib_id "probe:P") (at {x:g} {y:g} 0) {mirror} (unit 1) (exclude_from_sim no)'
            f' (in_bom yes) (on_board yes) (dnp no) (uuid "{uuid.uuid4()}")'
            f' (property "Reference" "P1" (at {x:g} {y:g} 0)'
            f" (effects (font (size 1.27 1.27)) (hide yes)))"
            f' (property "Value" "{text}" (at {x:g} {y:g} {probe.draw}) {effects})'
            f' (instances (project "probe" (path "/{sheet}" (reference "P1") (unit 1)))))'
        )
    if probe.kind == "text":
        return (
            f'(text "{text}" (exclude_from_sim no) (at {x:g} {y:g} {probe.draw}) {effects}'
            f' (uuid "{uuid.uuid4()}"))'
        )
    shape = f"(shape {probe.shape})" if probe.shape else ""
    return (
        f'({probe.kind} "{text}" {shape} (at {x:g} {y:g} {probe.draw}) {effects}'
        f' (uuid "{uuid.uuid4()}"))'
    )


def _sheet(nodes: list[str], sheet: str) -> str:
    lib = (
        '(lib_symbols (symbol "probe:P" (exclude_from_sim no) (in_bom yes) (on_board yes)'
        ' (property "Reference" "P" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        ' (property "Value" "P" (at 0 0 0) (effects (font (size 1.27 1.27))))'
        ' (symbol "P_0_1")))'
    )
    return (
        f'(kicad_sch (version 20250114) (generator "evaltor-calibration")'
        f' (generator_version "10.0") (uuid "{sheet}") (paper "A0") {lib}\n'
        + "\n".join(nodes)
        + '\n(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n'
    )


Box = tuple[float, float, float, float]
_NUM = r"-?\d+(?:\.\d+)?"


def _stroke_width(element: ET.Element, inherited: float) -> float:
    match = re.search(r"stroke-width:\s*(" + _NUM + ")", element.get("style") or "")
    return float(match.group(1)) if match else inherited


def _points(element: ET.Element) -> list[tuple[float, float]]:
    tag = element.tag.rsplit("}", 1)[-1]
    if tag == "path":
        pairs = re.findall("(" + _NUM + r")[ ,](" + _NUM + ")", element.get("d", ""))
        return [(float(a), float(b)) for a, b in pairs]
    if tag == "rect":
        x, y, w, h = (float(element.get(k, "0")) for k in ("x", "y", "width", "height"))
        return [(x, y), (x + w, y + h)]
    if tag == "circle":
        cx, cy, r = (float(element.get(k, "0")) for k in ("cx", "cy", "r"))
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return []


def _extent(points: list[tuple[float, float]], pen: float) -> Box:
    half = pen / 2.0
    return (
        min(p[0] for p in points) - half,
        min(p[1] for p in points) - half,
        max(p[0] for p in points) + half,
        max(p[1] for p in points) + half,
    )


def drawn(svg: str) -> tuple[list[tuple[str, Box]], list[Box]]:
    """Every run of text, as ``(content, box)``, and every other stroke's box."""
    texts: list[tuple[str, Box]] = []
    shapes: list[Box] = []

    def walk(element: ET.Element, pen: float) -> None:
        pen = _stroke_width(element, pen)
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "g" and element.get("class") == "stroked-text":
            content = next((c.text or "" for c in element if c.tag.endswith("desc")), "")
            points = [p for child in element.iter() for p in _points(child)]
            if points:
                texts.append((content, _extent(points, pen)))
            return
        points = _points(element)
        if points:
            shapes.append(_extent(points, pen))
        for child in element:
            walk(child, pen)

    walk(ET.fromstring(svg), 0.0)
    return texts, shapes


def _local(box: Box, x: float, y: float, draw: int) -> Box:
    """A sheet box around anchor ``(x, y)`` in the text's own frame."""
    x0, y0, x1, y1 = box[0] - x, box[1] - y, box[2] - x, box[3] - y
    if draw == 0:
        return (x0, y0, x1, y1)
    # Text at 90 degrees reads up the sheet: its own x is the sheet's -y, and its
    # own y, pointing down the letters, is the sheet's x.
    return (-y1, x0, -y0, x1)


@dataclass(frozen=True)
class Measured:
    ink: Box
    outline: Box | None


def measure(cli: str, fonts: FontSet, all_probes: list[Probe]) -> dict[str, Measured]:
    sheet = str(uuid.uuid4())
    anchors = []
    nodes = []
    for n, probe in enumerate(all_probes):
        x = PITCH_X * (1 + n % COLUMNS)
        y = PITCH_Y * (1 + n // COLUMNS)
        anchors.append((probe, x, y))
        nodes.append(_node(probe, fonts, x, y, sheet))
    with tempfile.TemporaryDirectory(prefix="kicad-evaltor-placement-") as tmp:
        sch = Path(tmp) / "placement.kicad_sch"
        sch.write_text(_sheet(nodes, sheet), encoding="utf-8")
        out = Path(tmp) / "svg"
        result = subprocess.run(
            [cli, "sch", "export", "svg", "-o", str(out), str(sch)],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"kicad-cli sch export svg failed:\n{result.stderr}")
        svg = next(out.glob("*.svg")).read_text(encoding="utf-8")
    texts, shapes = drawn(svg)

    def near(box: Box, x: float, y: float) -> bool:
        return (
            abs((box[0] + box[2]) / 2 - x) < WINDOW_X and abs((box[1] + box[3]) / 2 - y) < WINDOW_Y
        )

    measured = {}
    for probe, x, y in anchors:
        mine = [box for content, box in texts if content == fonts.content and near(box, x, y)]
        if len(mine) != 1:
            raise SystemExit(f"{probe.key}: expected one run of text, found {len(mine)}")
        flags = [box for box in shapes if near(box, x, y)]
        flag = (
            (
                min(b[0] for b in flags),
                min(b[1] for b in flags),
                max(b[2] for b in flags),
                max(b[3] for b in flags),
            )
            if flags
            else None
        )
        measured[probe.key] = Measured(
            _local(mine[0], x, y, probe.draw),
            _local(flag, x, y, probe.draw) if flag else None,
        )
    return measured


def _residual(probe: Probe, fonts: FontSet, ink: Box) -> tuple[float, float]:
    model = text_bbox(fonts.content, fonts.size, None, probe.h, probe.v)
    return (
        (ink[0] + ink[2] - model.min_x - model.max_x) / 2.0,
        (ink[1] + ink[3] - model.min_y - model.max_y) / 2.0,
    )


def _solve(rows: list[list[float]], values: list[float]) -> list[float]:
    """Solve a small square linear system by Gaussian elimination."""
    n = len(values)
    m = [[*row, value] for row, value in zip(rows, values, strict=True)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise SystemExit("calibration sets do not determine the fit")
        for r in range(n):
            if r != col:
                factor = m[r][col] / m[col][col]
                m[r] = [a - factor * b for a, b in zip(m[r], m[col], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def _fit(values: list[float]) -> list[float]:
    rows = [[1.0, f.size, f.pen.stroke, f.pen.layout, f.width] for f in FIT_SETS]
    return [round(c, 6) for c in _solve(rows, values)]


def calibrate(cli: str, tolerance: float) -> dict:
    all_probes = probes()
    fitted = [measure(cli, fonts, all_probes) for fonts in FIT_SETS]

    text = {}
    for probe in all_probes:
        residuals = [
            _residual(probe, f, m[probe.key].ink) for f, m in zip(FIT_SETS, fitted, strict=True)
        ]
        text[probe.key] = {
            "dx": _fit([r[0] for r in residuals]),
            "dy": _fit([r[1] for r in residuals]),
        }

    # A label's outline turns rigidly with it, so only the flat ones are fitted.
    outline = {}
    for probe in all_probes:
        if probe.kind not in ("global_label", "hierarchical_label"):
            continue
        if probe.draw != 0 or probe.v != "center":
            continue
        boxes = [m[probe.key].outline for m in fitted]
        if any(box is None for box in boxes):
            raise SystemExit(f"{probe.key}: no outline drawn")
        outline[outline_key(probe.kind, probe.shape, probe.h)] = {
            edge: _fit([box[i] for box in boxes if box is not None])
            for i, edge in enumerate(("x0", "y0", "x1", "y1"))
        }

    worst = _check(cli, all_probes, text, outline)
    print(f"held-out worst edge error: {worst:.4f} mm")
    if worst > tolerance:
        raise SystemExit(f"held-out error {worst:.4f} mm exceeds tolerance {tolerance} mm")

    return {
        "kicad_version": kicad_version(cli),
        "fit_sets": [f.__dict__ for f in FIT_SETS],
        "held_out_worst_mm": round(worst, 4),
        "text": text,
        "outline": outline,
    }


def _predict(probe: Probe, fonts: FontSet, text: dict, outline: dict) -> tuple[Box, Box | None]:
    entry = text[probe.key]
    pen = fonts.pen
    dx = Fit.from_list(entry["dx"])(fonts.size, pen, fonts.width)
    dy = Fit.from_list(entry["dy"])(fonts.size, pen, fonts.width)
    model = text_bbox(fonts.content, fonts.size, None, probe.h, probe.v)
    half = pen.stroke / 2.0
    ink = (
        model.min_x + dx - half,
        model.min_y + dy - half,
        model.max_x + dx + half,
        model.max_y + dy + half,
    )
    flag = outline.get(outline_key(probe.kind, probe.shape, probe.h))
    if flag is None or probe.draw != 0:
        return ink, None
    edges = tuple(
        Fit.from_list(flag[e])(fonts.size, pen, fonts.width) for e in ("x0", "y0", "x1", "y1")
    )
    return ink, edges  # type: ignore[return-value]


def _check(cli: str, all_probes: list[Probe], text: dict, outline: dict) -> float:
    worst = 0.0
    for fonts in CHECK_SETS:
        measured = measure(cli, fonts, all_probes)
        by_kind: dict[str, float] = {}
        for probe in all_probes:
            ink, flag = _predict(probe, fonts, text, outline)
            got = measured[probe.key]
            error = max(abs(a - b) for a, b in zip(ink, got.ink, strict=True))
            if flag is not None and got.outline is not None:
                error = max(error, *(abs(a - b) for a, b in zip(flag, got.outline, strict=True)))
            by_kind[probe.kind] = max(by_kind.get(probe.kind, 0.0), error)
            worst = max(worst, error)
        summary = "  ".join(f"{k} {v:.3f}" for k, v in by_kind.items())
        print(f"  {fonts}: {summary}")
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate where KiCad draws schematic text.")
    parser.add_argument("--cli", help="path to kicad-cli, if not on PATH")
    parser.add_argument("--out", type=Path, required=True, help="JSON file to write")
    parser.add_argument(
        "--tolerance", type=float, default=0.02, help="largest held-out edge error accepted, in mm"
    )
    args = parser.parse_args(argv)

    data = calibrate(find_cli(args.cli), args.tolerance)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"  kicad: {data['kicad_version']}")
    print(f"  text entries: {len(data['text'])}  outlines: {len(data['outline'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
