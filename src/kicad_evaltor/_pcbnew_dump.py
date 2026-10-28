"""Write a board's geometry as JSON. Runs under KiCad's bundled Python.

Usage: python -I _pcbnew_dump.py BOARD OUT_JSON [--fill-zones]

This file is executed by KiCad's interpreter, not imported by the library, so
it uses nothing but the standard library and ``pcbnew``. Every number is
converted to millimetres here; the library reads the JSON through
:mod:`kicad_evaltor.pcbnew_bridge`.
"""

import json
import sys
from pathlib import Path

import pcbnew

NM = 1_000_000.0
INK_MAX_ERROR_NM = 1000


def mm(value):
    return round(value / NM, 6)


def point(vector):
    return [mm(vector.x), mm(vector.y)]


def bbox(box):
    return [mm(box.GetLeft()), mm(box.GetTop()), mm(box.GetRight()), mm(box.GetBottom())]


def polygons(poly_set, holes=False):
    """A SHAPE_POLY_SET as [{"outline": [[x, y], ...], "holes": [...]}, ...]."""
    found = []
    for i in range(poly_set.OutlineCount()):
        outline = poly_set.Outline(i)
        entry = {"outline": [point(outline.CPoint(j)) for j in range(outline.PointCount())]}
        if holes:
            entry["holes"] = []
            for h in range(poly_set.HoleCount(i)):
                hole = poly_set.Hole(i, h)
                entry["holes"].append([point(hole.CPoint(j)) for j in range(hole.PointCount())])
        found.append(entry)
    return found


def _hull(points):
    """Convex hull of [x, y] points, counter-clockwise (Andrew's monotone chain)."""
    pts = sorted({(p[0], p[1]) for p in points})
    if len(pts) < 3:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def ink(item):
    """The text's printed ink, as pieces ``{"points": [[x, y], ...], "radius": r}``.

    With a radius, a piece is a round-ended stroke along its points, half the
    stroke width wide each side; with radius 0 it is a filled convex outline.
    Either way the ink reaches farthest at a point plus the radius. This is
    where the ink ends, unlike the text's frame box (``bbox``), which leaves
    room for line spacing and descenders.

    Stroke-font text (KiCad's default) comes from ``GetEffectiveTextShape()``,
    whose segments carry the stroke width (a segment's box already includes
    half of it); segments that continue one another are chained into one
    stroke. Other text (TrueType fonts, knockout) comes from
    ``TransformTextToPolySet``, the outline KiCad plots, with its round parts
    approximated to within 1 µm outside the real ink, each piece reduced to its
    convex hull (the hull's corners are points of the outline, so the farthest
    reach in any direction is unchanged, and the recording stays small).
    """
    knockout = item.IsKnockout() if hasattr(item, "IsKnockout") else False
    shape = item.GetEffectiveTextShape()
    pieces = list(shape.GetSubshapes()) if hasattr(shape, "GetSubshapes") else [shape]
    if pieces and not knockout and all(hasattr(piece, "GetSeg") for piece in pieces):
        found = []
        for piece in pieces:
            seg = piece.GetSeg()
            a, b, radius = point(seg.A), point(seg.B), mm(piece.GetWidth()) / 2
            if found and found[-1]["radius"] == radius and found[-1]["points"][-1] == a:
                found[-1]["points"].append(b)
            else:
                found.append({"points": [a, b], "radius": radius})
        return found
    outline = pcbnew.SHAPE_POLY_SET()
    item.TransformTextToPolySet(outline, 0, INK_MAX_ERROR_NM, pcbnew.ERROR_OUTSIDE)
    return [{"points": _hull(p["outline"]), "radius": 0.0} for p in polygons(outline)]


def text_entry(board, item, owner, kind):
    return {
        "owner": owner,
        "kind": kind,
        "text": item.GetShownText(True) if hasattr(item, "GetShownText") else item.GetText(),
        "layer": board.GetLayerName(item.GetLayer()),
        "visible": bool(item.IsVisible()),
        "height": mm(item.GetTextHeight()),
        "thickness": mm(item.GetTextThickness()),
        "bbox": bbox(item.GetBoundingBox()),
        "ink": ink(item),
    }


def footprint_entry(board, fp):
    if hasattr(fp, "BuildCourtyardCaches"):
        fp.BuildCourtyardCaches()
    pads = []
    for pad in fp.Pads():
        copper = [board.GetLayerName(layer) for layer in pad.GetLayerSet().CuStack()]
        polygon = []
        if copper:
            layer = board.GetLayerID(copper[0])
            shape = pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE)
            polygon = polygons(shape)[0]["outline"] if shape.OutlineCount() else []
        drill = pad.GetDrillSize()
        pads.append(
            {
                "number": pad.GetNumber(),
                "net": pad.GetNetname(),
                "x": mm(pad.GetPosition().x),
                "y": mm(pad.GetPosition().y),
                "copper_layers": copper,
                "drill": mm(drill.x) if drill.x else None,
                "polygon": polygon,
            }
        )
    texts = [
        text_entry(board, fp.Reference(), fp.GetReference(), "reference"),
        text_entry(board, fp.Value(), fp.GetReference(), "value"),
    ]
    for item in fp.GraphicalItems():
        if isinstance(item, pcbnew.PCB_TEXT):
            texts.append(text_entry(board, item, fp.GetReference(), "user"))
    return {
        "reference": fp.GetReference(),
        "x": mm(fp.GetPosition().x),
        "y": mm(fp.GetPosition().y),
        "rotation": fp.GetOrientationDegrees(),
        "side": "back" if fp.IsFlipped() else "front",
        "bbox": bbox(fp.GetBoundingBox(False)),
        "courtyard": {
            "front": polygons(fp.GetCourtyard(pcbnew.F_CrtYd)),
            "back": polygons(fp.GetCourtyard(pcbnew.B_CrtYd)),
        },
        "pads": pads,
        "texts": texts,
    }


def zone_entry(board, zone, connectivity):
    layers = [board.GetLayerName(layer) for layer in zone.GetLayerSet().Seq()]
    filled = {}
    for layer in zone.GetLayerSet().Seq():
        if zone.IsOnCopperLayer() and zone.HasFilledPolysForLayer(layer):
            filled[board.GetLayerName(layer)] = round(
                zone.GetFilledPolysList(layer).Area() / NM**2, 4
            )
    pads = []
    if not zone.GetIsRuleArea():
        for pad in connectivity.GetConnectedPads(zone):
            pads.append(f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}")
    return {
        "net": zone.GetNetname(),
        "name": zone.GetZoneName(),
        "layers": layers,
        "rule_area": bool(zone.GetIsRuleArea()),
        "outline_area": round(zone.Outline().Area() / NM**2, 4),
        "filled_area": filled,
        "connected_pads": sorted(set(pads)),
    }


def main():
    board_path, out_path = sys.argv[1], sys.argv[2]
    board = pcbnew.LoadBoard(board_path)
    if "--fill-zones" in sys.argv[3:]:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()

    outline = pcbnew.SHAPE_POLY_SET()
    closed = bool(board.GetBoardPolygonOutlines(outline, False))
    nets = {}
    for code, net in board.GetNetInfo().NetsByNetcode().items():
        if net.GetNetname():
            nets[net.GetNetname()] = {"netclass": net.GetNetClassName()}

    texts = []
    for item in board.GetDrawings():
        if isinstance(item, pcbnew.PCB_TEXT):
            texts.append(text_entry(board, item, "", "board"))

    data = {
        "kicad_version": pcbnew.GetBuildVersion(),
        "zones_filled": "--fill-zones" in sys.argv[3:],
        "outline": {
            "closed": closed,
            "area": round(outline.Area() / NM**2, 4) if closed else 0.0,
            "bbox": bbox(outline.BBox()) if closed else None,
            "polygons": polygons(outline, holes=True) if closed else [],
        },
        "footprints": [footprint_entry(board, fp) for fp in board.GetFootprints()],
        "zones": [zone_entry(board, zone, connectivity) for zone in board.Zones()],
        "nets": nets,
        "texts": texts,
        "unconnected": connectivity.GetUnconnectedCount(True),
    }
    Path(out_path).write_text(json.dumps(data), encoding="utf-8")


main()
