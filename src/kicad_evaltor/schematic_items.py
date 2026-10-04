"""Geometric extraction from a ``.kicad_sch`` document.

The overlap checks need more than the netlist-level view of a schematic: they
need every piece of ink on the sheet with real coordinates. Placed symbol
instances only carry their position and their field text, so body outlines and
pins are recovered by looking each ``lib_id`` up in the ``lib_symbols`` table and
transforming the library geometry onto the sheet.

Coordinate system: KiCad schematic millimetres, x right, y down. Library symbols
are authored with their own y growing upwards, and ``Placement`` handles the
conversion, mirroring and rotation.

Known limitations, all deliberate rather than accidental:

* Pin bodies are modelled as the pin line from its connection point to its
  attachment point, widened by half a line width. Decorative shapes (clock
  inversions, input arrows) add a few hundred microns and are ignored.
* Arc and bezier graphics are sampled from their quadratic bulge approximation
  rather than solved exactly, so a body bbox can be a fraction of a millimeter
  conservative.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from kicad_evaltor.font_metrics import text_cell_bbox
from kicad_evaltor.geometry import BBox, Placement
from kicad_evaltor.schematic_file import FileField, FileSchematic
from kicad_evaltor.sexpr import SExpr, child, children, head, is_hidden, value_of

Point = tuple[float, float]

# KiCad stores a width of 0 to mean "whatever the sheet currently draws with",
# which is 0.1524 mm for wires, pins and any graphic that leaves it unset. That
# was measured by exporting a probe sheet to PostScript and reading the widths
# back: a zero-width wire plots at 0.1524 mm, and so do the twelve connector
# pins on the demo sheet. The overlap checks need a real thickness, so the zero
# becomes that number here.
DEFAULT_LINE_WIDTH = 0.1524


def _numbers(node: SExpr | None) -> list[float]:
    if node is None:
        return []
    out: list[float] = []
    for atom in node[1:]:
        if isinstance(atom, list):
            continue
        try:
            out.append(float(atom))
        except ValueError:
            continue
    return out


def _point(node: SExpr | None) -> Point:
    values = _numbers(node)
    if len(values) >= 2:
        return (values[0], values[1])
    return (0.0, 0.0)


def _quadratic_extrema(start: Point, control: Point, end: Point) -> list[Point]:
    """Sample a quadratic bulge, which stands in for a circular arc."""
    points: list[Point] = []
    for step in range(17):
        t = step / 16.0
        inv = 1.0 - t
        points.append(
            (
                inv * inv * start[0] + 2 * inv * t * control[0] + t * t * end[0],
                inv * inv * start[1] + 2 * inv * t * control[1] + t * t * end[1],
            )
        )
    return points


@dataclass(frozen=True)
class SymbolPin:
    """One pin of a library symbol, in symbol-local coordinates.

    ``offset`` is the pin's own ``(offset ...)`` shift, applied in library
    coordinates before the symbol is placed. ``hidden`` marks a pin KiCad does not
    draw, which is how a symbol carries its unconnected and reserved pins.
    """

    name: str
    number: str
    at: Point
    angle: float
    length: float
    offset: Point = (0.0, 0.0)
    hidden: bool = False

    @property
    def connection(self) -> Point:
        """Where a wire, a label or a no-connect flag has to land."""
        return (self.at[0] + self.offset[0], self.at[1] + self.offset[1])

    def segment(self) -> tuple[Point, Point]:
        """The pin line, from its connection point to its attachment point."""
        start = self.connection
        radians = math.radians(self.angle)
        dx = math.cos(radians) * self.length
        dy = math.sin(radians) * self.length
        return (start, (start[0] + dx, start[1] + dy))


@dataclass(frozen=True)
class SymbolGeometry:
    """Body graphics and pins for one symbol body style."""

    bboxes: tuple[BBox, ...]
    pins: tuple[SymbolPin, ...]


@dataclass(frozen=True)
class TextItem:
    """A run of text on the sheet, with the box KiCad reserves for it.

    The box is the line cell, not the ink: it is what KiCad justifies, and text
    that is merely close enough to touch is still unreadable.

    ``field`` names the property it was drawn from: a symbol field name such as
    ``Value``, or a free-standing kind such as ``label``.
    """

    content: str
    bbox: BBox
    rotation: float
    size: float
    owner: str | None = None
    field: str = ""

    @property
    def label(self) -> str:
        if self.owner and self.field:
            return f"{self.owner}.{self.field}"
        if self.owner:
            return self.owner
        return self.content


@dataclass(frozen=True)
class WireSegment:
    """A wire, kept as a thickened line."""

    start: Point
    end: Point
    width: float = DEFAULT_LINE_WIDTH

    @property
    def bbox(self) -> BBox:
        half = self.width / 2.0
        return BBox(
            min(self.start[0], self.end[0]) - half,
            min(self.start[1], self.end[1]) - half,
            max(self.start[0], self.end[0]) + half,
            max(self.start[1], self.end[1]) + half,
        )

    @property
    def label(self) -> str:
        return f"wire {self.start}->{self.end}"


@dataclass(frozen=True)
class Junction:
    at: Point
    diameter: float = 0.0

    @property
    def bbox(self) -> BBox:
        half = self.diameter / 2.0
        return BBox(self.at[0] - half, self.at[1] - half, self.at[0] + half, self.at[1] + half)

    @property
    def label(self) -> str:
        return f"junction {self.at}"


@dataclass(frozen=True)
class PinConnection:
    """Where one pin expects a wire, a label or a no-connect flag to land.

    ``into_body`` is the unit heading from that point towards the symbol's
    artwork, which is where a wire must *not* go: a wire leaves the pin, it does
    not run back across the drawing.

    ``hidden`` carries the library symbol's ``(hide yes)``. The point is still the
    pin's electrical end, so a wire or a flag that lands there connects; what it
    cannot be is seen.
    """

    component: str
    number: str
    name: str
    at: Point
    into_body: Point = (0.0, 0.0)
    hidden: bool = False

    @property
    def label(self) -> str:
        """``U2.4`` for pin 4.

        The number identifies a pin; the name does not, since a symbol can have
        eight pins all called ``NC``.
        """
        return f"{self.component}.{self.number or self.name}"


@dataclass(frozen=True)
class Component:
    """A placed symbol with its outline, pins and visible text.

    ``properties`` holds its drawn properties, user-defined ones included. Hidden
    ones are left out: they are not drawn.
    """

    reference: str
    lib_id: str
    at: Placement
    body_bbox: BBox | None
    pin_bboxes: tuple[BBox, ...]
    texts: tuple[TextItem, ...]
    is_power: bool = False
    properties: Mapping[str, str] = field(default_factory=dict)
    pin_connections: tuple[PinConnection, ...] = ()

    @property
    def value(self) -> str:
        """The Value property, which is the one a schematic is read for."""
        return self.properties.get("Value", "")

    @property
    def bbox(self) -> BBox | None:
        """The symbol's ink extent: body plus every pin."""
        boxes = [b for b in (self.body_bbox, *self.pin_bboxes) if b is not None]
        if not boxes:
            return None
        return BBox.union_all(boxes)

    @property
    def label(self) -> str:
        return self.reference


@dataclass
class SchematicScene:
    """Everything on the sheet, with coordinates."""

    components: list[Component] = field(default_factory=list)
    wires: list[WireSegment] = field(default_factory=list)
    junctions: list[Junction] = field(default_factory=list)
    texts: list[TextItem] = field(default_factory=list)
    no_connects: list[Point] = field(default_factory=list)

    def component(self, reference: str) -> Component | None:
        for comp in self.components:
            if comp.reference == reference:
                return comp
        return None

    @property
    def component_texts(self) -> list[TextItem]:
        return [t for t in self.texts if t.owner is not None]

    @property
    def standalone_texts(self) -> list[TextItem]:
        return [t for t in self.texts if t.owner is None]

    @property
    def power_components(self) -> list[Component]:
        return [c for c in self.components if c.is_power]


def _justify(field_node: FileField) -> tuple[str, str]:
    """Map a KiCad ``justify`` string onto horizontal and vertical alignment."""
    words = (field_node.justify or "").split()
    halign = "center"
    valign = "center"
    for word in words:
        if word == "left":
            halign = "left"
        elif word == "right":
            halign = "right"
        elif word == "top":
            valign = "top"
        elif word == "bottom":
            valign = "bottom"
    return halign, valign


def _symbol_pins(sub: SExpr) -> tuple[SymbolPin, ...]:
    pins: list[SymbolPin] = []
    for pin in children(sub, "pin"):
        at = _point(child(pin, "at"))
        angles = _numbers(child(pin, "at"))
        angle = angles[2] if len(angles) > 2 else 0.0
        lengths = _numbers(child(pin, "length"))
        pins.append(
            SymbolPin(
                name=value_of(child(pin, "name")),
                number=value_of(child(pin, "number")),
                at=at,
                angle=angle,
                length=lengths[0] if lengths else 2.54,
                offset=_point(child(pin, "offset")),
                hidden=is_hidden(pin),
            )
        )
    return tuple(pins)


def _stroke_width(node: SExpr) -> float:
    """The drawn width of a graphic or wire, resolving KiCad's zero to the default."""
    stroke = child(node, "stroke")
    width = _numbers(child(stroke, "width")) if stroke else []
    if width and width[0] > 0:
        return width[0]
    return DEFAULT_LINE_WIDTH


def _symbol_bboxes(sub: SExpr) -> tuple[BBox, ...]:
    """Bounding boxes of a symbol body's graphics, in local coordinates.

    KiCad strokes a path down its centre line, so each box is grown by half the
    graphic's stroke width. A circle and a polyline are grown the same way even
    though they are not rectangles, which leaves their boxes slightly generous at
    the corners and never short of the ink anywhere else.
    """
    boxes: list[BBox] = []

    def add(points: list[Point], width: float) -> None:
        if not points:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        half = width / 2.0
        boxes.append(BBox(min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half))

    for graphic in sub[1:]:
        if not isinstance(graphic, list):
            continue
        kind = head(graphic)
        width = _stroke_width(graphic)
        if kind == "rectangle":
            start = _point(child(graphic, "start"))
            end = _point(child(graphic, "end"))
            add([start, end], width)
        elif kind == "circle":
            center = _point(child(graphic, "center"))
            radius = _numbers(child(graphic, "radius"))
            r = radius[0] if radius else 0.0
            add(
                [
                    (center[0] - r, center[1] - r),
                    (center[0] + r, center[1] + r),
                ],
                width,
            )
        elif kind == "polyline":
            pts = child(graphic, "pts")
            add([_point(p) for p in children(pts, "xy")] if pts else [], width)
        elif kind in ("arc", "bezier"):
            pts = child(graphic, "pts")
            raw = [_point(p) for p in children(pts, "xy")] if pts else []
            if len(raw) >= 3:
                mid = raw[1]
                # KiCad stores the bulge as a bezier control offset; pull it
                # back out by the distance from the chord midpoint.
                chord_x = (raw[0][0] + raw[2][0]) / 2.0
                chord_y = (raw[0][1] + raw[2][1]) / 2.0
                control = (2.0 * mid[0] - chord_x, 2.0 * mid[1] - chord_y)
                add(_quadratic_extrema(raw[0], control, raw[2]), width)
            else:
                add(raw, width)
    return tuple(boxes)


def _shifted(box: BBox, offset: Point) -> BBox:
    return BBox(
        box.min_x + offset[0],
        box.min_y + offset[1],
        box.max_x + offset[0],
        box.max_y + offset[1],
    )


def _shifted_pin(pin: SymbolPin, offset: Point) -> SymbolPin:
    if offset == (0.0, 0.0):
        return pin
    return SymbolPin(
        name=pin.name,
        number=pin.number,
        at=(pin.at[0] + offset[0], pin.at[1] + offset[1]),
        angle=pin.angle,
        length=pin.length,
        offset=pin.offset,
    )


def _lookup_geometry(
    lib_symbols: Mapping[str, list[SExpr]], lib_id: str, unit: int, body_style: int
) -> SymbolGeometry | None:
    """Find the sub-symbols that make up one placed instance.

    Unit 0 graphics are shared by every instance, so they are always included
    alongside the graphics for the requested unit and body style.
    """
    definition = lib_symbols.get(lib_id)
    if definition is None:
        return None
    boxes: list[BBox] = []
    pins: list[SymbolPin] = []
    for sub in children(definition, "symbol"):
        name = value_of(sub, 1)
        parts = name.rsplit("_", 2)
        if len(parts) != 3:
            continue
        sub_unit, sub_style = parts[1], parts[2]
        try:
            sub_unit_i, sub_style_i = int(sub_unit), int(sub_style)
        except ValueError:
            continue
        if sub_unit_i != 0 and (sub_unit_i, sub_style_i) != (unit, body_style):
            continue
        # A sub-symbol's own offset shifts everything inside it, which is how a
        # library symbol stacks two bodies or moves a graphic off its origin.
        offset = _point(child(sub, "offset"))
        boxes.extend(_shifted(box, offset) for box in _symbol_bboxes(sub))
        pins.extend(_shifted_pin(pin, offset) for pin in _symbol_pins(sub))
    if not boxes and not pins:
        return None
    return SymbolGeometry(bboxes=tuple(boxes), pins=tuple(pins))


def _text_item(
    content: str,
    size: float,
    at: Point,
    halign: str,
    valign: str,
    owner: str | None,
    name: str,
) -> TextItem | None:
    if not content:
        return None
    return TextItem(
        content=content,
        bbox=text_cell_bbox(content, size, at, halign, valign),
        rotation=0.0,
        size=size,
        owner=owner,
        field=name,
    )


def _component_texts(symbol, owner: str) -> tuple[TextItem, ...]:
    texts: list[TextItem] = []
    is_power = symbol.lib_id.startswith("power:")
    for field_node in symbol.texts:
        if not field_node.visible:
            continue
        # A power symbol's name is drawn by the symbol's own artwork, not as a
        # text field, so its Value property is not rendered even when the file
        # leaves `(hide yes)` off it.
        if is_power and field_node.name == "Value":
            continue
        halign, valign = _justify(field_node)
        # KiCad renders symbol field text horizontally, at the absolute sheet
        # position the field stores, whatever angle the field or the symbol says.
        item = _text_item(
            field_node.value,
            field_node.size,
            (field_node.x, field_node.y),
            halign,
            valign,
            owner,
            field_node.name,
        )
        if item is not None:
            texts.append(item)
    return tuple(texts)


def _visible_properties(symbol) -> dict[str, str]:
    """The symbol's drawn properties, keyed by name.

    Built from the fields rather than from ``symbol.properties`` so that hidden
    and empty values drop out here too.
    """
    return {field_node.name: field_node.value for field_node in symbol.texts if field_node.visible}


def extract(schematic: FileSchematic) -> SchematicScene:
    """Build the sheet's geometry from a parsed schematic."""
    lib_symbols = schematic.lib_symbols()
    scene = SchematicScene()

    for symbol in schematic.get_symbols():
        geometry = _lookup_geometry(lib_symbols, symbol.lib_id, symbol.unit, symbol.body_style)
        body: BBox | None = None
        pin_boxes: list[BBox] = []
        connections: list[PinConnection] = []
        if geometry is not None:
            local = BBox.union_all(geometry.bboxes) if geometry.bboxes else None
            if local is not None:
                body = symbol.at.apply_box(local)
            for pin in geometry.pins:
                local_start, local_end = pin.segment()
                start = symbol.at.apply(*local_start)
                end = symbol.at.apply(*local_end)
                # A pin is a drawn line, so its box carries the drawn thickness.
                # Without it the box is degenerate and can never overlap anything.
                half = DEFAULT_LINE_WIDTH / 2.0
                pin_boxes.append(
                    BBox(
                        min(start[0], end[0]) - half,
                        min(start[1], end[1]) - half,
                        max(start[0], end[0]) + half,
                        max(start[1], end[1]) + half,
                    )
                )
                connections.append(
                    PinConnection(
                        component=symbol.reference,
                        number=pin.number,
                        name=pin.name,
                        at=symbol.at.apply(*pin.connection),
                        into_body=symbol.at.apply_direction(
                            math.cos(math.radians(pin.angle)), math.sin(math.radians(pin.angle))
                        ),
                        hidden=pin.hidden,
                    )
                )
        component = Component(
            reference=symbol.reference,
            lib_id=symbol.lib_id,
            at=symbol.at,
            body_bbox=body,
            pin_bboxes=tuple(pin_boxes),
            pin_connections=tuple(connections),
            texts=_component_texts(symbol, symbol.reference),
            is_power=symbol.lib_id.startswith("power:"),
            properties=_visible_properties(symbol),
        )
        scene.components.append(component)
        scene.texts.extend(component.texts)

    for wire in children(schematic.nodes(), "wire"):
        pts = child(wire, "pts")
        points = [_point(p) for p in children(pts, "xy")] if pts else []
        stroke = child(wire, "stroke")
        width = _numbers(child(stroke, "width")) if stroke else []
        thickness = width[0] if width and width[0] > 0 else DEFAULT_LINE_WIDTH
        for start, end in itertools.pairwise(points):
            scene.wires.append(WireSegment(start, end, thickness))

    for junction in children(schematic.nodes(), "junction"):
        scene.junctions.append(Junction(_point(child(junction, "at"))))

    # A no-connect flag marks a pin as deliberately unconnected. It carries no
    # size of its own: it is an X centred on the pin it belongs to.
    for flag in children(schematic.nodes(), "no_connect"):
        scene.no_connects.append(_point(child(flag, "at")))

    for kind in ("text", "label", "global_label", "hierarchical_label"):
        for node in children(schematic.nodes(), kind):
            # Hidden text puts no ink on the sheet, so it cannot overlap anything
            # and no reader can be confused by it. These nodes keep `(hide yes)`
            # inside the effects block rather than beside it.
            if is_hidden(node):
                continue
            effects = child(node, "effects")
            font = child(effects, "font") if effects else None
            size_values = _numbers(child(font, "size")) if font else []
            size = size_values[0] if size_values else 1.27
            justify = value_of(child(effects, "justify")) if effects else ""
            angles = _numbers(child(node, "at"))
            at = (angles[0] if angles else 0.0, angles[1] if len(angles) > 1 else 0.0)
            words = justify.split()
            halign = "left" if "left" in words else "right" if "right" in words else "center"
            valign = "top" if "top" in words else "bottom" if "bottom" in words else "center"
            content = value_of(node, 1) if kind == "text" else value_of(node)
            item = _text_item(content, size, at, halign, valign, None, kind)
            if item is not None:
                scene.texts.append(item)

    return scene
