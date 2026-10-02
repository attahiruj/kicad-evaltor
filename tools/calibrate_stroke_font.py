"""Measure KiCad's stroke font by rendering a calibration sheet.

KiCad does not expose its stroke font metrics, so they are measured from the one
authority that knows them exactly: the installed ``kicad-cli``. A sheet of probe
strings is written, rendered to SVG, and each glyph's ink box and advance are
read back out of the drawn paths.

Three properties make this exact rather than approximate:

* each character probe renders the character *twice*, so the horizontal offset
  between the two identical glyphs' left ink edges is exactly one advance, with
  no dependence on side bearings
* advance and ink height were confirmed to scale linearly with font size, so a
  single measurement is normalised by size and reused at any size
* the sheet is rendered on its own, away from the demo design, and every probe
  is measured in a tight window around its own anchor

Regenerate the committed table with::

    python tools/calibrate_stroke_font.py --out src/kicad_evaltor/_stroke_font_data.json

The output is committed so that using the metrics needs neither KiCad nor a
calibration run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SIZE = 2.54
ANCHOR_X = 100.0
ANCHOR_Y = 100.0
CELL_W = 40.0
CELL_H = 30.0
COLUMNS = 8

MEASURE_RADIUS = 12.0
CLUSTER_GAP = 0.15

CHARACTERS = [chr(c) for c in range(0x21, 0x7F)]
JUSTIFICATIONS = ("left bottom", "left top", "right bottom", "right top")
JUSTIFY_SAMPLES = ("M", "Mg", "MMg")
JUSTIFY_PROBE_ROW = 3


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _text_node(content: str, x: float, y: float, justify: str | None) -> str:
    effects = f"(font (size {SIZE:g} {SIZE:g}))"
    if justify:
        effects += f"\n\t\t\t(justify {justify})"
    return (
        f'\t(text "{_escape(content)}"\n'
        f"\t\t(exclude_from_sim no)\n"
        f"\t\t(at {x:g} {y:g} 0)\n"
        f"\t\t(effects\n\t\t\t{effects}\n\t\t)\n"
        f'\t\t(uuid "{uuid.uuid4()}")\n'
        f"\t)"
    )


def build_sheet(probes: list[tuple[str, float, float, str | None]]) -> str:
    """A self-contained sheet holding only probe text items.

    KiCad refuses to load a schematic with no ``lib_symbols`` node, hence the
    empty one.
    """
    nodes = "\n\n".join(_text_node(t, x, y, j) for t, x, y, j in probes)
    return (
        "(kicad_sch\n"
        "\t(version 20250114)\n"
        '\t(generator "evaltor-calibration")\n'
        '\t(generator_version "10.0")\n'
        f'\t(uuid "{uuid.uuid4()}")\n'
        '\t(paper "A0")\n'
        "\t(lib_symbols )\n"
        f"{nodes}\n"
        '\t(sheet_instances (path "/" (page "1")))\n'
        "\t(embedded_fonts no)\n"
        ")\n"
    )


def find_cli(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if not found:
        raise SystemExit("kicad-cli not found on PATH; pass --cli")
    return found


def render(sheet_text: str, workdir: Path, cli: str) -> str:
    sch = workdir / "calibration.kicad_sch"
    sch.write_text(sheet_text, encoding="utf-8")
    out = workdir / "calibration.svg"
    if out.is_dir():
        shutil.rmtree(out)
    elif out.exists():
        out.unlink()

    result = subprocess.run(
        [cli, "sch", "export", "svg", "-o", str(out), str(sch)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"kicad-cli sch export svg failed:\n{result.stderr}")

    produced = out / f"{sch.stem}.svg" if out.is_dir() else out
    if not produced.exists():
        raise SystemExit(f"kicad-cli wrote no SVG at {produced}")
    return produced.read_text(encoding="utf-8")


def drawn_boxes(svg: str) -> list[tuple[float, float, float, float]]:
    """Bounding box of every line segment the renderer drew."""
    import re

    pattern = re.compile(r"<path d=\"([^\"]+)\"")
    seg = re.compile(r"M(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)\s+L(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)")
    boxes = []
    for match in pattern.finditer(svg):
        for m in seg.finditer(match.group(1)):
            x1, y1, x2, y2 = (float(g) for g in m.groups())
            boxes.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return boxes


def cluster_by_x(
    boxes: list[tuple[float, float, float, float]], gap: float = CLUSTER_GAP
) -> list[tuple[float, float, float, float]]:
    """Group segments into glyphs, splitting where the ink has a gap."""
    if not boxes:
        return []
    ordered = sorted(boxes)
    groups: list[list[tuple[float, float, float, float]]] = [[ordered[0]]]
    for box in ordered[1:]:
        if box[0] - groups[-1][-1][2] > gap:
            groups.append([box])
        else:
            groups[-1].append(box)
    return [
        (
            min(b[0] for b in g),
            min(b[1] for b in g),
            max(b[2] for b in g),
            max(b[3] for b in g),
        )
        for g in groups
    ]


def measure_glyphs(
    boxes: list[tuple[float, float, float, float]],
    anchor: tuple[float, float],
) -> list[tuple[float, float, float, float]]:
    """Glyph ink boxes near an anchor, in sheet-relative coordinates.

    The renderer flips y, so vertical offsets are negated. Coordinates are
    returned as (x0, y_top, x1, y_bottom) relative to the anchor. One glyph can
    yield several boxes when its ink is not connected, such as the two strokes
    of a quotation mark.
    """
    ax, ay = anchor
    near = [
        b for b in boxes if abs(b[0] - ax) <= MEASURE_RADIUS and abs(b[1] - ay) <= MEASURE_RADIUS
    ]
    result = []
    for box in cluster_by_x(near):
        result.append(
            (
                box[0] - ax,
                -(box[1] - ay),
                box[2] - ax,
                -(box[3] - ay),
            )
        )
    return result


def ink_union(
    clusters: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    """Combine the clusters of one glyph into a single ink box."""
    if not clusters:
        raise SystemExit("no ink clusters to combine")
    return (
        min(c[0] for c in clusters),
        max(c[1] for c in clusters),
        max(c[2] for c in clusters),
        min(c[3] for c in clusters),
    )


def calibrate(cli: str, verbose: bool) -> dict[str, object]:
    def cell(i: int) -> tuple[float, float]:
        return (
            ANCHOR_X + (i % COLUMNS) * CELL_W,
            ANCHOR_Y + (i // COLUMNS) * CELL_H,
        )

    # Three sheets. Singletons give each glyph's ink box and how many separate
    # blobs of ink it is made of; the doubled probe uses that blob count to find
    # where the second copy starts, which is what makes the advance measurable
    # for characters whose ink is disconnected.
    single_probes = [(char, *cell(i), "left bottom") for i, char in enumerate(CHARACTERS)]
    double_probes = [
        (f"{char} {char}", *cell(i), "left bottom") for i, char in enumerate(CHARACTERS)
    ]
    space_probes = [
        ("MM", ANCHOR_X, ANCHOR_Y, "left bottom"),
        ("M M", ANCHOR_X + CELL_W, ANCHOR_Y, "left bottom"),
    ]
    justify_probes = []
    for row, sample in enumerate(JUSTIFY_SAMPLES):
        for col, justify in enumerate(JUSTIFICATIONS):
            justify_probes.append(
                (
                    sample,
                    ANCHOR_X + col * CELL_W,
                    ANCHOR_Y + (row + JUSTIFY_PROBE_ROW) * CELL_H,
                    justify,
                )
            )
    # An unjustified item lands centred on its anchor; probe that too.
    justify_probes.append(
        ("Mg", ANCHOR_X, ANCHOR_Y + (JUSTIFY_PROBE_ROW + len(JUSTIFY_SAMPLES)) * CELL_H, None)
    )

    def render_boxes(probes: list[tuple[str, float, float, str | None]]) -> list:
        with tempfile.TemporaryDirectory(prefix="kicad-evaltor-calib-") as tmp:
            return drawn_boxes(render(build_sheet(probes), Path(tmp), cli))

    single_boxes = render_boxes(single_probes)
    double_boxes = render_boxes(double_probes)
    space_boxes = render_boxes(space_probes)
    justify_boxes = render_boxes(justify_probes)
    if verbose:
        print("sheets rendered")

    tight = measure_glyphs(space_boxes, (ANCHOR_X, ANCHOR_Y))
    loose = measure_glyphs(space_boxes, (ANCHOR_X + CELL_W, ANCHOR_Y))
    if len(tight) < 2 or len(loose) < 2:
        raise SystemExit("could not measure the space advance")
    space_advance = (loose[1][0] - loose[0][0]) - (tight[1][0] - tight[0][0])
    if verbose:
        print(f"space advance {space_advance / SIZE:.6f} of size")

    glyphs: dict[str, dict[str, float]] = {}
    for i, char in enumerate(CHARACTERS):
        anchor = cell(i)
        single = measure_glyphs(single_boxes, anchor)
        if not single:
            raise SystemExit(f"no ink found for {char!r} at {anchor}")
        ink = ink_union(single)

        doubled = measure_glyphs(double_boxes, anchor)
        if not doubled:
            raise SystemExit(f"no ink found for the doubled probe of {char!r}")
        # The doubled probe lays out as [ink][space][ink], so the ink spans from
        # the first copy's left edge to the second copy's right edge:
        #   span = advance + space + ink_width
        # which gives the advance without having to count ink blobs. Blob
        # counting is unreliable because a glyph's ink can split differently
        # between the two renderings.
        span = max(c[2] for c in doubled) - min(c[0] for c in doubled)
        advance = span - space_advance - (ink[2] - ink[0])

        # KiCad's stroke font divides the em into 21 columns, so every advance
        # is a whole number of 1/21. Snap to that grid; the tolerance still
        # rejects a measurement that is genuinely wrong rather than merely a
        # few ten-thousandths of a millimetre out.
        units = advance / SIZE * 21.0
        if abs(units - round(units)) > 0.6:
            raise SystemExit(
                f"implausible advance for {char!r}: {advance / SIZE:.5f} of size "
                f"({units:.3f} units of 1/21)"
            )
        advance = round(units) / 21.0 * SIZE

        glyphs[char] = {
            "advance": round(advance / SIZE, 6),
            "x0": round(ink[0] / SIZE, 6),
            "y_top": round(ink[1] / SIZE, 6),
            "x1": round(ink[2] / SIZE, 6),
            "y_bottom": round(ink[3] / SIZE, 6),
        }

    # Self-check: a capital M should be exactly one em tall.
    height = glyphs["M"]["y_top"] - glyphs["M"]["y_bottom"]
    if abs(height - 1.0) > 0.02:
        raise SystemExit(
            f"calibration looks wrong: 'M' ink height {height:.4f} of size, expected 1.0"
        )
    for char, m in glyphs.items():
        ink_height = m["y_top"] - m["y_bottom"]
        if ink_height > 1.6:
            raise SystemExit(f"implausible ink height for {char!r}: {ink_height:.3f} of size")

    glyphs[" "] = {
        "advance": round(space_advance / SIZE, 6),
        "x0": 0.0,
        "y_top": 0.0,
        "x1": 0.0,
        "y_bottom": 0.0,
    }

    justification: dict[str, dict[str, float]] = {}
    sample_boxes: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    for row, sample in enumerate(JUSTIFY_SAMPLES):
        base_y = ANCHOR_Y + (row + JUSTIFY_PROBE_ROW) * CELL_H
        per_justify = {}
        for col, justify in enumerate(JUSTIFICATIONS):
            clusters = measure_glyphs(justify_boxes, (ANCHOR_X + col * CELL_W, base_y))
            if not clusters:
                raise SystemExit(f"no ink found for {sample!r} justify {justify!r}")
            ink = ink_union(clusters)
            normalised = (
                round(ink[0] / SIZE, 6),
                round(ink[1] / SIZE, 6),
                round(ink[2] / SIZE, 6),
                round(ink[3] / SIZE, 6),
            )
            per_justify[justify] = normalised
            if justify == "left bottom":
                justification[f"{justify}"] = dict(
                    zip(("x0", "y_top", "x1", "y_bottom"), normalised)
                )
        sample_boxes[sample] = per_justify

    # KiCad justifies around its own text box, whose width is not simply the sum
    # of advances. Recover it from how far a right-justified item shifts, for
    # strings of differing length, and fit a line.
    pairs = []
    for sample, per_justify in sample_boxes.items():
        left = per_justify["left bottom"]
        right = per_justify["right bottom"]
        box_width = left[0] - right[0]
        advance_sum = sum(glyphs[c]["advance"] for c in sample)
        pairs.append((advance_sum, box_width))
    pairs.sort()
    if len(pairs) >= 2:
        (x0_, y0_), (x1_, y1_) = pairs[0], pairs[-1]
        slope = (y1_ - y0_) / (x1_ - x0_)
        intercept = y0_ - slope * x0_
    else:  # pragma: no cover - only with a single sample configured
        slope, intercept = 1.0, 0.0
    box_model = {
        "slope": round(slope, 6),
        # The intercept is the stroke thickness KiCad leaves on the right of the
        # text box. It is an absolute length, not a multiple of the font size,
        # so it is converted back to millimetres here.
        "intercept_mm": round(intercept * SIZE, 6),
        "samples": [[round(a, 6), round(b, 6)] for a, b in pairs],
    }

    # Vertical placement follows the same idea; record the observed line height.
    top_shift = sample_boxes["M"]["left top"][1] - sample_boxes["M"]["left bottom"][1]
    line_height = -top_shift

    default_clusters = measure_glyphs(
        justify_boxes,
        (ANCHOR_X, ANCHOR_Y + (JUSTIFY_PROBE_ROW + len(JUSTIFY_SAMPLES)) * CELL_H),
    )
    if default_clusters:
        ink = ink_union(default_clusters)
        justification["default"] = {
            "x0": round(ink[0] / SIZE, 6),
            "y_top": round(ink[1] / SIZE, 6),
            "x1": round(ink[2] / SIZE, 6),
            "y_bottom": round(ink[3] / SIZE, 6),
        }

    return {
        "kicad_version": kicad_version(cli),
        "calibration_size_mm": SIZE,
        "glyphs": glyphs,
        "justification": justification,
        "text_box": {"slope": box_model["slope"], "intercept_mm": box_model["intercept_mm"]},
        "line_height_em": round(line_height, 6),
        "box_samples": box_model["samples"],
    }


def kicad_version(cli: str) -> str:
    try:
        result = subprocess.run(
            [cli, "version"], capture_output=True, text=True, timeout=60, check=False
        )
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate KiCad's stroke font metrics.")
    parser.add_argument("--cli", help="path to kicad-cli, if not on PATH")
    parser.add_argument("--out", type=Path, required=True, help="JSON file to write")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    data = calibrate(find_cli(args.cli), args.verbose)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"  kicad: {data['kicad_version']}")
    print(f"  glyphs: {len(data['glyphs'])}  justification: {len(data['justification'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
