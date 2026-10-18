"""Read board data straight from a ``.kicad_pcb`` file.

This is the board's no-KiCad path, the counterpart of
:mod:`kicad_evaltor.schematic_file`. What KiCad stores as plain values is read
here: footprints with their fields, attributes and pads, net names, tracks,
vias, zones, the stackup, and net classes and design rules from the project
file. Those cover component, routing and width questions without KiCad
installed.

Anything that needs real geometry is left to KiCad. Courtyards, pad shapes,
the board outline, zone fill and connectivity come from ``pcbnew``
(:mod:`kicad_evaltor.pcbnew_bridge`), and rule checks come from
``kicad-cli pcb drc``.

Units are millimetres, as in the file. Board coordinates have Y pointing down.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from kicad_evaltor.sexpr import Quoted, SExpr, child, children, head, is_hidden, parse, value_of


@dataclass(frozen=True)
class BoardPad:
    """One pad of a footprint, as the file stores it.

    ``kind`` is KiCad's pad type: ``smd``, ``thru_hole``, ``np_thru_hole`` or
    ``connect``. A pad with no number, such as a separate paste aperture, has
    ``number == ""``. ``drill`` is None for a pad with no hole.
    """

    number: str
    kind: str
    shape: str
    size: tuple[float, float]
    layers: tuple[str, ...]
    net: str = ""
    pin_function: str = ""
    pin_type: str = ""
    drill: float | None = None
    die_length: float = 0.0
    """Length inside the package from pad to die, which KiCad's length tuning
    adds to the net. 0 when the footprint does not give one."""

    @property
    def is_copper(self) -> bool:
        return any(layer.endswith(".Cu") for layer in self.layers)


@dataclass(frozen=True)
class FootprintText:
    """A footprint's Reference or Value field: what it says and how it is drawn."""

    text: str
    layer: str
    hidden: bool
    height: float
    thickness: float


@dataclass(frozen=True)
class BoardFootprint:
    """One placed footprint.

    ``side`` is ``"front"`` or ``"back"``. ``rotation`` is in degrees,
    counter-clockwise as KiCad shows it. ``path`` is the schematic symbol's uuid
    path, which is what ties the footprint to its symbol.
    """

    reference: str
    value: str
    lib_id: str
    side: str
    x: float
    y: float
    rotation: float
    pads: tuple[BoardPad, ...] = ()
    properties: dict[str, str] = field(default_factory=dict)
    path: str = ""
    sheetfile: str = ""
    smd: bool = False
    through_hole: bool = False
    board_only: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos_files: bool = False
    dnp: bool = False
    reference_text: FootprintText | None = None

    @property
    def nets(self) -> set[str]:
        """Every net one of this footprint's pads is on."""
        return {pad.net for pad in self.pads if pad.net}

    def pad(self, number: str) -> BoardPad | None:
        return next((p for p in self.pads if p.number == number and p.is_copper), None)


@dataclass(frozen=True)
class BoardTrack:
    """A straight segment or an arc of copper.

    ``mid`` is the arc's midpoint, and None for a straight segment.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net: str
    mid: tuple[float, float] | None = None

    @property
    def is_arc(self) -> bool:
        return self.mid is not None

    @property
    def length(self) -> float:
        if self.mid is None:
            return math.dist(self.start, self.end)
        return arc_length(self.start, self.mid, self.end)


@dataclass(frozen=True)
class BoardVia:
    """A via. ``via_type`` is ``through``, ``blind_buried`` or ``micro``."""

    x: float
    y: float
    size: float
    drill: float
    layers: tuple[str, ...]
    net: str
    via_type: str = "through"

    @property
    def annular_ring(self) -> float:
        return (self.size - self.drill) / 2


@dataclass(frozen=True)
class BoardZone:
    """A copper zone or a rule area (keepout). How it fills is pcbnew's job."""

    net: str
    name: str
    layers: tuple[str, ...]
    priority: int = 0
    keepout: bool = False


@dataclass(frozen=True)
class BoardText:
    """A free text item drawn on the board, not owned by a footprint."""

    text: str
    layer: str
    x: float
    y: float
    hidden: bool = False


@dataclass(frozen=True)
class StackupLayer:
    name: str
    type: str
    thickness: float | None = None
    material: str = ""


@dataclass(frozen=True)
class NetClass:
    """One net class from the project file. A value left unset is None."""

    name: str
    priority: int
    track_width: float | None = None
    clearance: float | None = None
    via_diameter: float | None = None
    via_drill: float | None = None
    diff_pair_width: float | None = None
    diff_pair_gap: float | None = None


_NETCLASS_FIELDS = (
    "track_width",
    "clearance",
    "via_diameter",
    "via_drill",
    "diff_pair_width",
    "diff_pair_gap",
)


@dataclass(frozen=True)
class NetClasses:
    """The project's net classes and board design rules.

    KiCad 10 can put a net in several classes at once and names the result after
    all of them, like ``"VCC,Default"``. :meth:`effective` resolves such a name
    the way KiCad does: each value comes from the highest-priority constituent
    that sets it (lowest ``priority`` number first), and ``Default`` fills in the
    rest.
    """

    classes: dict[str, NetClass] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)

    def effective(self, name: str) -> NetClass:
        parts = [part.strip() for part in name.split(",") if part.strip()] or ["Default"]
        known = sorted(
            (self.classes[part] for part in parts if part in self.classes),
            key=lambda nc: nc.priority,
        )
        default = self.classes.get("Default")
        if default is not None and default not in known:
            known.append(default)
        values: dict[str, float | None] = {}
        for attr in _NETCLASS_FIELDS:
            values[attr] = next(
                (getattr(nc, attr) for nc in known if getattr(nc, attr) is not None), None
            )
        return NetClass(name=name, priority=known[0].priority if known else 0, **values)


class FileBoard:
    """Board access backed by the ``.kicad_pcb`` file itself."""

    def __init__(self, path: str | Path, project_path: str | Path | None = None) -> None:
        self._path = Path(path)
        document = parse(self._path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(document, list) or head(document) != "kicad_pcb":
            raise ValueError(f"Not a KiCad board: {self._path}")
        self._document = document
        self._project_path = (
            Path(project_path) if project_path else self._path.with_suffix(".kicad_pro")
        )
        # Files from KiCad 9 and earlier name a net once, in a top-level
        # (net 3 "GND") table, and refer to it by code alone on tracks and vias.
        codes = net_codes(document)
        self.footprints: list[BoardFootprint] = [
            _footprint_from(node, codes) for node in children(document, "footprint")
        ]
        self.tracks: list[BoardTrack] = [
            *(_track_from(node, codes) for node in children(document, "segment")),
            *(_track_from(node, codes) for node in children(document, "arc")),
        ]
        self.vias: list[BoardVia] = [_via_from(node, codes) for node in children(document, "via")]
        self.zones: list[BoardZone] = [
            _zone_from(node, codes) for node in children(document, "zone")
        ]
        self.texts: list[BoardText] = [_text_from(node) for node in children(document, "gr_text")]
        self._net_classes: NetClasses | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def version(self) -> int:
        try:
            return int(value_of(child(self._document, "version"), default="0"))
        except ValueError:
            return 0

    @property
    def thickness(self) -> float | None:
        return _float(value_of(child(child(self._document, "general"), "thickness"), default=""))

    @cached_property
    def copper_layers(self) -> list[str]:
        """Copper layer names, front to back."""
        layers = child(self._document, "layers")
        found = []
        for node in layers[1:] if layers else []:
            if isinstance(node, list) and len(node) >= 3 and str(node[1]).endswith(".Cu"):
                found.append(str(node[1]))
        order = {"F.Cu": -1, "B.Cu": 1000}
        return sorted(found, key=lambda name: order.get(name, _inner_index(name)))

    @cached_property
    def stackup(self) -> list[StackupLayer]:
        stackup = child(child(self._document, "setup"), "stackup")
        found = []
        for node in children(stackup, "layer"):
            found.append(
                StackupLayer(
                    name=value_of(node),
                    type=value_of(child(node, "type")),
                    thickness=_float(value_of(child(node, "thickness"), default="")),
                    material=value_of(child(node, "material")),
                )
            )
        return found

    def copper_thickness(self, layer: str) -> float | None:
        """A copper layer's thickness in mm, or None when the stackup omits it."""
        return next(
            (s.thickness for s in self.stackup if s.name == layer and s.type == "copper"), None
        )

    @cached_property
    def nets(self) -> list[str]:
        """Every named net on the board, sorted."""
        names: set[str] = set()
        for node in children(self._document, "net"):
            names.add(_net_name(node))
        for fp in self.footprints:
            names.update(fp.nets)
        names.update(t.net for t in self.tracks)
        names.update(v.net for v in self.vias)
        names.update(z.net for z in self.zones)
        names.discard("")
        return sorted(names)

    @property
    def net_classes(self) -> NetClasses:
        """Net classes and design rules from the project file, if there is one."""
        if self._net_classes is None:
            self._net_classes = load_net_classes(self._project_path)
        return self._net_classes

    def get_footprints(self) -> list[BoardFootprint]:
        return list(self.footprints)

    def footprint(self, reference: str) -> BoardFootprint | None:
        return next((f for f in self.footprints if f.reference == reference), None)

    def tracks_on(self, net: str) -> list[BoardTrack]:
        return [t for t in self.tracks if t.net == net]

    def vias_on(self, net: str) -> list[BoardVia]:
        return [v for v in self.vias if v.net == net]

    def routed_length(self, net: str) -> float:
        """Total track length on a net in mm, vias not counted."""
        return sum(t.length for t in self.tracks_on(net))

    @property
    def uses_via_height(self) -> bool:
        """Whether the project counts via height in lengths, as KiCad's
        ``use_height_for_length_calcs`` design setting says (on by default)."""
        return bool(self.net_classes.rules.get("use_height_for_length_calcs", True))

    def net_length(self, net: str, *, include_vias: bool | None = None) -> float:
        """A net's length as KiCad's DRC and length tuning count it, in mm.

        Track length, plus each via's height when ``include_vias`` (None follows
        the project's setting), plus every pad's pad-to-die length. A via counts
        only between the outermost copper layers its tracks use, and its full
        span when no track ends on it, which is how KiCad optimises via height.
        Verified against KiCad 10's DRC on every net of the example board.
        """
        total = self.routed_length(net)
        if self.uses_via_height if include_vias is None else include_vias:
            total += sum(self.via_length(v) for v in self.vias_on(net))
        total += sum(p.die_length for fp in self.footprints for p in fp.pads if p.net == net)
        return total

    def via_length(self, via: BoardVia) -> float:
        """The height of a via that a signal travels, in mm.

        Between the outermost copper layers of the tracks that end on it, or its
        whole span when none do; 0 when its tracks are all on one layer.
        """
        layers = {
            t.layer
            for t in self.tracks_on(via.net)
            if t.layer in self.copper_layers
            and any(math.dist(end, (via.x, via.y)) < 1e-4 for end in (t.start, t.end))
        }
        if not layers:
            layers = {layer for layer in via.layers if layer in self.copper_layers}
            if len(via.layers) >= 2 and not layers:
                layers = {self.copper_layers[0], self.copper_layers[-1]}
        if len(layers) < 2:
            return 0.0
        order = self.copper_layers
        ranked = sorted(layers, key=order.index)
        return self.layer_distance(ranked[0], ranked[-1])

    def layer_distance(self, first: str, last: str) -> float:
        """Distance through the board from one copper layer to another, in mm.

        Everything in the stackup from ``first`` to ``last``, both copper layers
        included, as KiCad measures via height. Without a stackup, the board's
        thickness stands in for the outer layers.
        """
        names = [s.name for s in self.stackup]
        if first in names and last in names:
            a, b = sorted((names.index(first), names.index(last)))
            span = self.stackup[a : b + 1]
            if all(
                s.thickness is not None for s in span if s.type in ("copper", "core", "prepreg")
            ):
                return sum(
                    s.thickness or 0.0 for s in span if s.type in ("copper", "core", "prepreg")
                )
        return self.thickness or 1.6


def load_board(path: str | Path, project_path: str | Path | None = None) -> FileBoard:
    return FileBoard(path, project_path)


def load_net_classes(project_path: Path) -> NetClasses:
    """Net classes and ``board.design_settings.rules`` from a ``.kicad_pro``."""
    if not project_path.is_file():
        return NetClasses()
    try:
        data = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return NetClasses()
    classes = {}
    for entry in data.get("net_settings", {}).get("classes", []):
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        classes[entry["name"]] = NetClass(
            name=entry["name"],
            priority=int(entry.get("priority", 0)),
            **{attr: _float(entry.get(attr)) for attr in _NETCLASS_FIELDS},
        )
    rules = data.get("board", {}).get("design_settings", {}).get("rules", {})
    return NetClasses(classes=classes, rules=rules if isinstance(rules, dict) else {})


def arc_length(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> float:
    """Length of the circular arc from ``start`` through ``mid`` to ``end``."""
    circle = arc_circle(start, mid, end)
    if circle is None:
        return math.dist(start, end)
    _, _, radius, _, sweep = circle
    return radius * abs(sweep)


def arc_circle(
    start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]
) -> tuple[float, float, float, float, float] | None:
    """The circle an arc lies on: (cx, cy, radius, start angle, signed sweep).

    The sweep runs from ``start`` to ``end`` the way round that passes through
    ``mid``, in radians. None when the three points are in a line.
    """
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = (
        (ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)
    ) / d
    uy = (
        (ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)
    ) / d
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    sweep = (a2 - a0) % (2 * math.pi)
    if (a1 - a0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    return ux, uy, math.dist((ux, uy), start), a0, sweep


def _footprint_from(node: list[SExpr], codes: dict[str, str]) -> BoardFootprint:
    properties = {value_of(p): value_of(p, 2) for p in children(node, "property")}
    at = _numbers(child(node, "at"))
    attr = {str(a) for a in (child(node, "attr") or [])[1:] if isinstance(a, str)}
    reference_node = next(
        (p for p in children(node, "property") if value_of(p) == "Reference"), None
    )
    return BoardFootprint(
        reference=properties.get("Reference", ""),
        value=properties.get("Value", ""),
        lib_id=value_of(node),
        side="back" if value_of(child(node, "layer")) == "B.Cu" else "front",
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        rotation=at[2] if len(at) > 2 else 0.0,
        pads=tuple(_pad_from(p, codes) for p in children(node, "pad")),
        properties=properties,
        path=value_of(child(node, "path")),
        sheetfile=value_of(child(node, "sheetfile")),
        smd="smd" in attr,
        through_hole="through_hole" in attr,
        board_only="board_only" in attr,
        exclude_from_bom="exclude_from_bom" in attr,
        exclude_from_pos_files="exclude_from_pos_files" in attr,
        dnp="dnp" in attr,
        reference_text=_field_text(reference_node) if reference_node else None,
    )


def _field_text(node: list[SExpr]) -> FootprintText:
    font = child(child(node, "effects"), "font")
    size = _numbers(child(font, "size"))
    return FootprintText(
        text=value_of(node, 2),
        layer=value_of(child(node, "layer")),
        hidden=is_hidden(node),
        height=size[1] if len(size) > 1 else (size[0] if size else 1.0),
        thickness=_float(value_of(child(font, "thickness"), default="")) or 0.0,
    )


def _pad_from(node: list[SExpr], codes: dict[str, str]) -> BoardPad:
    size = _numbers(child(node, "size"))
    drill = _numbers(child(node, "drill"))
    return BoardPad(
        number=value_of(node),
        kind=value_of(node, 2),
        shape=value_of(node, 3),
        size=(size[0], size[1]) if len(size) > 1 else (0.0, 0.0),
        layers=tuple(str(a) for a in (child(node, "layers") or [])[1:] if isinstance(a, str)),
        net=_net_name(child(node, "net"), codes),
        pin_function=value_of(child(node, "pinfunction")),
        pin_type=value_of(child(node, "pintype")),
        drill=drill[0] if drill else None,
        die_length=_float(value_of(child(node, "die_length"), default="")) or 0.0,
    )


def _track_from(node: list[SExpr], codes: dict[str, str]) -> BoardTrack:
    start = _numbers(child(node, "start"))
    end = _numbers(child(node, "end"))
    mid = _numbers(child(node, "mid"))
    return BoardTrack(
        start=(start[0], start[1]),
        end=(end[0], end[1]),
        width=_float(value_of(child(node, "width"), default="")) or 0.0,
        layer=value_of(child(node, "layer")),
        net=_net_name(child(node, "net"), codes),
        mid=(mid[0], mid[1]) if head(node) == "arc" and len(mid) > 1 else None,
    )


def _via_from(node: list[SExpr], codes: dict[str, str]) -> BoardVia:
    at = _numbers(child(node, "at"))
    kind = value_of(node, default="")
    return BoardVia(
        x=at[0],
        y=at[1],
        size=_float(value_of(child(node, "size"), default="")) or 0.0,
        drill=_float(value_of(child(node, "drill"), default="")) or 0.0,
        layers=tuple(str(a) for a in (child(node, "layers") or [])[1:] if isinstance(a, str)),
        net=_net_name(child(node, "net"), codes),
        via_type={"blind": "blind_buried", "micro": "micro"}.get(kind, "through"),
    )


def _zone_from(node: list[SExpr], codes: dict[str, str]) -> BoardZone:
    layers = child(node, "layers") or child(node, "layer") or []
    return BoardZone(
        net=_net_name(child(node, "net"), codes) or unescape(value_of(child(node, "net_name"))),
        name=value_of(child(node, "name")),
        layers=tuple(str(a) for a in layers[1:] if isinstance(a, str)),
        priority=int(_float(value_of(child(node, "priority"), default="")) or 0),
        keepout=child(node, "keepout") is not None,
    )


def _text_from(node: list[SExpr]) -> BoardText:
    at = _numbers(child(node, "at"))
    return BoardText(
        text=value_of(node),
        layer=value_of(child(node, "layer")),
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        hidden=is_hidden(node),
    )


def net_codes(document: list[SExpr]) -> dict[str, str]:
    """The top-level ``(net 3 "GND")`` table of an older file, code to name."""
    table = {}
    for node in children(document, "net"):
        atoms = [a for a in node[1:] if isinstance(a, str)]
        if len(atoms) >= 2 and atoms[0].isdigit():
            table[atoms[0]] = unescape(atoms[-1])
    return table


def _net_name(node: list[SExpr] | None, codes: dict[str, str] | None = None) -> str:
    """The net's name from ``(net "GND")`` (KiCad 10), ``(net 3 "GND")``, or the
    bare ``(net 3)`` older files put on tracks, vias and zones, looked up in
    ``codes``."""
    if node is None:
        return ""
    atoms = [a for a in node[1:] if isinstance(a, str)]
    if not atoms:
        return ""
    if len(atoms) == 1:
        atom = atoms[0]
        if atom.isdigit() and not isinstance(atom, Quoted):
            return (codes or {}).get(atom, "")
        return unescape(atom)
    return unescape(atoms[-1])


_ESCAPES = {
    "slash": "/",
    "backslash": "\\",
    "dblquote": '"',
    "quote": "'",
    "lt": "<",
    "gt": ">",
    "bar": "|",
    "comma": ",",
    "colon": ":",
    "space": " ",
    "dollar": "$",
    "tab": "\t",
    "return": "\n",
    "brace": "{",
}
_ESCAPE = re.compile(r"\{(" + "|".join(_ESCAPES) + r")\}")


def unescape(name: str) -> str:
    """A net name as KiCad shows it: ``Net-(U2-PS{slash}SYNC)`` is ``Net-(U2-PS/SYNC)``.

    KiCad stores characters that are special in its files, such as ``/``, as
    ``{slash}``-style sequences, and DRC reports and the editor show them
    unescaped.
    """
    return _ESCAPE.sub(lambda m: _ESCAPES[m.group(1)], name) if "{" in name else name


def _inner_index(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 500


def _numbers(node: list[SExpr] | None) -> list[float]:
    if node is None:
        return []
    found = []
    for atom in node[1:]:
        if isinstance(atom, str):
            number = _float(atom)
            if number is not None:
                found.append(number)
    return found


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
