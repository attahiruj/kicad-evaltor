"""Read schematic data straight from a ``.kicad_sch`` file.

This is the no-KiCad path. Symbols, values, fields and footprints are plain
s-expressions and are parsed directly, so the schematic checks run anywhere.

Net connectivity is deliberately *not* reimplemented: resolving KiCad's
wire/label/junction graph is real geometry work, so the netlist is exported by
``kicad-cli`` when it is available and reported as unavailable when it is not.
That is the same division of labour used elsewhere in the library: ask KiCad for
anything geometric rather than guessing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_evaltor.geometry import Placement
from kicad_evaltor.sexpr import SExpr, child, children, head, parse, value_of

# KiCad's standard symbol properties, exposed as `fields` the way kipy does.
_STANDARD_PROPERTIES = frozenset(
    {
        "Reference",
        "Value",
        "Footprint",
        "Datasheet",
        "Description",
        "Keywords",
    }
)


# KiCad's named paper sizes in millimetres, as (width, height).
_PAPER_MM: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
    "A5": (210.0, 148.0),
    "B": (352.0, 250.0),
    "C": (432.0, 288.0),
    "D": (220.0, 170.0),
    "E": (879.0, 612.0),
    "USLetter": (279.4, 215.9),
    "USLegal": (355.6, 215.9),
    "USLedger": (431.8, 279.4),
}


@dataclass(frozen=True)
class FileField:
    """A symbol property, with the text placement KiCad stored for it.

    Field positions in a placed symbol are absolute sheet coordinates, not
    offsets from the symbol origin, so they can be used directly.
    """

    name: str
    value: str
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    size: float = 1.27
    justify: str | None = None
    hidden: bool = False

    @property
    def visible(self) -> bool:
        return not self.hidden and self.value != ""


class FileSymbol:
    """One placed symbol, shaped like the kipy objects the checks consume."""

    def __init__(
        self,
        reference: str,
        lib_id: str,
        value: str,
        footprint: str,
        properties: dict[str, str],
        *,
        x: float = 0.0,
        y: float = 0.0,
        rotation: float = 0.0,
        mirror: str | None = None,
        unit: int = 1,
        body_style: int = 1,
        fields: tuple[FileField, ...] = (),
    ) -> None:
        self.reference = reference
        self.lib_id = lib_id
        self.value = value
        self.footprint = FootprintRef(footprint)
        # One source for every property; fields and user_fields below are the
        # two views checks read, split to mirror kipy.
        self.properties = dict(properties)
        self.user_fields = {
            name: text for name, text in properties.items() if name not in _STANDARD_PROPERTIES
        }
        self.fields = {
            name: text for name, text in properties.items() if name in _STANDARD_PROPERTIES
        }
        self.sheet = None
        self.at = Placement(x, y, rotation=rotation, mirror=mirror)
        self.unit = unit
        self.body_style = body_style
        self.field_list = fields

    @property
    def texts(self) -> tuple[FileField, ...]:
        return self.field_list


class FootprintRef:
    def __init__(self, value: str) -> None:
        self.text = Text(value)


class Text:
    def __init__(self, value: str) -> None:
        self.value = value


class FileNetNode:
    def __init__(self, ref: str, pin: str, net: str) -> None:
        self.ref = ref
        self.pin = pin
        self.net = net


class FileNet:
    def __init__(self, name: str, nodes: list[FileNetNode]) -> None:
        self.name = name
        self.nodes = nodes


class FileNetlist:
    def __init__(self, nets: list[FileNet]) -> None:
        self.nets = nets


class NetlistUnavailable(RuntimeError):
    """Raised when connectivity was asked for but cannot be produced."""


class FileSchematic:
    """Schematic access backed by the file itself, plus kicad-cli for nets."""

    def __init__(self, path: Path, cli_path: str | None = None) -> None:
        self._path = Path(path)
        self._cli_path = cli_path
        self._document = self._load()

    def _load(self) -> list[SExpr]:
        document = parse(self._path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(document, list) or head(document) != "kicad_sch":
            raise ValueError(f"Not a KiCad schematic: {self._path}")
        return document

    def get_symbols(self) -> list[FileSymbol]:
        """Return every placed symbol, excluding lib_symbols definitions.

        A placed instance is a direct child of the document whose first child is
        a ``lib_id`` list; the library definitions under ``lib_symbols`` start
        with a name string instead.
        """
        symbols = []
        for node in self._document:
            if not isinstance(node, list) or head(node) != "symbol":
                continue
            if child(node, "lib_id") is None:
                continue
            symbols.append(self._symbol_from(node))
        return symbols

    def nodes(self) -> list[SExpr]:
        """The top-level s-expression nodes of the schematic.

        Wire, junction and text extraction needs the raw document, which the
        typed accessors deliberately do not expose.
        """
        return self._document

    def sheet_size(self) -> tuple[float, float]:
        """The drawing area of the sheet in millimetres, as (width, height).

        KiCad names its standard sizes; anything else, including the ``User``
        size, is read from the paper node's own dimensions.
        """
        paper = child(self._document, "paper")
        if paper is None:
            raise ValueError(f"Schematic has no paper size: {self._path}")
        name = value_of(paper)
        if name in _PAPER_MM:
            return _PAPER_MM[name]
        size = _atom_numbers(child(paper, "size"))
        if len(size) >= 2:
            return (size[0], size[1])
        raise ValueError(f"Unsupported paper size {name!r} in {self._path}")

    def lib_symbols(self) -> dict[str, list[SExpr]]:
        """The ``lib_symbols`` table, keyed by library id.

        The overlap checks need this to recover body outlines and pin geometry,
        which only exist in the library definitions rather than the placed
        instances.
        """
        table: dict[str, list[SExpr]] = {}
        for node in children(self._document, "lib_symbols"):
            for sym in children(node, "symbol"):
                lib_id = value_of(sym, 1)
                if lib_id:
                    table[lib_id] = sym
        return table

    def _symbol_from(self, node: list[SExpr]) -> FileSymbol:
        properties: dict[str, str] = {}
        fields: list[FileField] = []
        for prop in children(node, "property"):
            name = value_of(prop, 1)
            if not name:
                continue
            text = value_of(prop, 2)
            properties[name] = text
            fields.append(_field_from(prop, name, text))

        position = _atom_numbers(child(node, "at"))
        return FileSymbol(
            reference=properties.get("Reference", ""),
            lib_id=value_of(child(node, "lib_id")),
            value=properties.get("Value", ""),
            footprint=properties.get("Footprint", ""),
            properties=properties,
            x=position[0] if position else 0.0,
            y=position[1] if len(position) > 1 else 0.0,
            rotation=position[2] if len(position) > 2 else 0.0,
            mirror=value_of(child(node, "mirror")) or None,
            unit=int(float(value_of(child(node, "unit")) or 1)),
            body_style=int(float(value_of(child(node, "body_style")) or 1)),
            fields=tuple(fields),
        )

    def get_netlist(self) -> FileNetlist:
        """Export connectivity with kicad-cli rather than reimplementing it."""
        document = self._export_netlist()
        nets = []
        nets_node = child(document, "nets")
        if nets_node is not None:
            for net in children(nets_node, "net"):
                name = value_of(child(net, "name"))
                if not name:
                    continue
                nodes = []
                for node in children(net, "node"):
                    ref = value_of(child(node, "ref"))
                    if ref:
                        nodes.append(
                            FileNetNode(ref=ref, pin=value_of(child(node, "pin")), net=name)
                        )
                nets.append(FileNet(name, nodes))
        return FileNetlist(nets)

    def _export_netlist(self) -> list[SExpr]:
        cli = self._resolve_cli()
        if cli is None:
            raise NetlistUnavailable(
                "netlist export needs kicad-cli; provide kicad_cli_path or put it on PATH"
            )

        with tempfile.TemporaryDirectory(prefix="kicad-evaltor-") as workdir:
            target = Path(workdir) / "netlist.net"
            try:
                result = subprocess.run(
                    [
                        cli,
                        "sch",
                        "export",
                        "netlist",
                        "--format",
                        "kicadsexpr",
                        "-o",
                        str(target),
                        str(self._path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise NetlistUnavailable(f"netlist export failed: {exc}") from exc

            if result.returncode != 0 or not target.exists():
                detail = (result.stderr or result.stdout).strip()
                raise NetlistUnavailable(f"netlist export failed: {detail}")

            document = parse(target.read_text(encoding="utf-8", errors="replace"))

        if not isinstance(document, list):
            raise NetlistUnavailable("netlist export produced unreadable output")
        return document

    def _resolve_cli(self) -> str | None:
        if self._cli_path:
            return self._cli_path
        return shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")


def _atom_numbers(node: list[SExpr] | None) -> list[float]:
    """Numbers from a positional node such as ``(at 1.27 2.54 90)``."""
    if node is None:
        return []
    numbers: list[float] = []
    for atom in node[1:]:
        if isinstance(atom, list):
            continue
        try:
            numbers.append(float(atom))
        except ValueError:
            continue
    return numbers


def _field_from(prop: list[SExpr], name: str, text: str) -> FileField:
    """Read one symbol property into a positioned field."""
    position = _atom_numbers(child(prop, "at"))
    effects = child(prop, "effects")
    size = value_of(child(child(effects, "font"), "size")) if effects else ""
    justify = value_of(child(effects, "justify")) if effects else ""
    return FileField(
        name=name,
        value=text,
        x=position[0] if position else 0.0,
        y=position[1] if len(position) > 1 else 0.0,
        rotation=position[2] if len(position) > 2 else 0.0,
        size=float(size) if size else 1.27,
        justify=justify or None,
        hidden=child(prop, "hide") is not None,
    )


def load_schematic(path: Path, cli_path: str | None = None) -> FileSchematic:
    """Convenience wrapper matching the other loader helpers."""
    return FileSchematic(path, cli_path=cli_path)


def to_dict(schematic: FileSchematic) -> dict[str, Any]:
    """Summarise a schematic for debugging and for the example output."""
    symbols = schematic.get_symbols()
    return {
        "path": str(schematic._path),
        "symbol_count": len(symbols),
        "symbols": sorted(
            ({"reference": s.reference, "lib_id": s.lib_id, "value": s.value} for s in symbols),
            key=lambda s: s["reference"],
        ),
    }
