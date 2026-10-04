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
from kicad_evaltor.sexpr import SExpr, child, children, head, is_hidden, parse, value_of, words_of
from kicad_evaltor.sheets import (
    HierarchicalLabel,
    Sheet,
    label_from_node,
    sheet_from_node,
)

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
    # The font's stroke settings: 0 thickness means KiCad's default pen.
    thickness: float = 0.0
    bold: bool = False

    @property
    def visible(self) -> bool:
        return not self.hidden and self.value != ""


class FileSymbol:
    """One placed symbol, shaped like the kipy objects the checks consume.

    ``sheet`` names the sheet this symbol sits on, and is ``None`` for one drawn
    on the root sheet. ``sheet_path`` is KiCad's uuid instance path, which
    survives the case a name does not: one file instantiated under two sheet
    symbols. Both default to the root because a symbol read straight out of one
    file with no tree around it *is* a root-sheet symbol.
    """

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
        sheet: str | None = None,
        sheet_path: str = "/",
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
        self.sheet = sheet
        self.sheet_path = sheet_path
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
    """Schematic access backed by the file itself, plus kicad-cli for nets.

    A KiCad schematic is a tree of files, and this class is deliberately a
    faithful reader of exactly one of them. It reports the ``(sheet ...)`` blocks
    it can see and nothing more: walking into them is
    :mod:`kicad_evaltor.hierarchy`'s job, which is what keeps "one file, one
    coordinate space, one paper size" true here.

    ``project_dir`` is the project folder, which is where KiCad resolves a
    relative ``Sheetfile``; without one the file's own folder stands in.
    ``instance_path`` is the uuid path of the sheet this file draws, needed to
    give the file's own subsheets their paths. Both default to the root case.
    """

    def __init__(
        self,
        path: Path,
        cli_path: str | None = None,
        project_dir: Path | None = None,
        instance_path: str = "/",
    ) -> None:
        self._path = Path(path)
        self._cli_path = cli_path
        self._project_dir = Path(project_dir) if project_dir else None
        self._instance_path = instance_path
        self._document = self._load()

    def _load(self) -> list[SExpr]:
        document = parse(self._path.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(document, list) or head(document) != "kicad_sch":
            raise ValueError(f"Not a KiCad schematic: {self._path}")
        return document

    @property
    def path(self) -> Path:
        """The file this schematic was read from."""
        return self._path

    def project_dir(self) -> Path | None:
        """The project folder, when the caller knows one."""
        return self._project_dir

    def cli_path(self) -> str | None:
        """The kicad-cli path this schematic was configured with, if any."""
        return self._cli_path

    def instance_path(self) -> str:
        """The uuid instance path of the sheet this file draws."""
        return self._instance_path

    def base_dir(self) -> Path:
        """The folder a relative ``Sheetfile`` is resolved against first."""
        return self._project_dir if self._project_dir else self._path.parent

    def uuid(self) -> str:
        """The document's own ``(uuid ...)``, which is the root sheet's uuid."""
        return value_of(child(self._document, "uuid"))

    def sheets(self) -> list[Sheet]:
        """The ``(sheet ...)`` blocks on this sheet, and nothing below them.

        Direct children only. Each carries the paths its ``Sheetfile`` might
        resolve to, so a link that does not resolve is reported rather than
        skipped.
        """
        return [
            sheet_from_node(
                node,
                base_dir=self.base_dir(),
                parent_dir=self._path.parent,
                parent_path=self._instance_path,
            )
            for node in children(self._document, "sheet")
        ]

    def has_subsheets(self) -> bool:
        return bool(self.sheets())

    def hierarchical_labels(self) -> list[HierarchicalLabel]:
        """The ``(hierarchical_label ...)`` nodes, which a parent sheet's pins match."""
        return [label_from_node(node) for node in children(self._document, "hierarchical_label")]

    def page_instances(self) -> dict[str, str]:
        """The ``(sheet_instances ...)`` page numbers, keyed by instance path.

        KiCad writes at least the root path whenever it is tracking pages, so a
        non-empty mapping is how a file says "this design has pages" as opposed
        to a hand-written one that never had any.
        """
        pages: dict[str, str] = {}
        for path_node in children(child(self._document, "sheet_instances"), "path"):
            page = value_of(child(path_node, "page"))
            if page:
                pages[value_of(path_node)] = page
        return pages

    def get_symbols(self) -> list[FileSymbol]:
        """Return every placed symbol on this sheet, excluding lib_symbols.

        A placed instance is a direct child of the document whose first child is
        a ``lib_id`` list; the library definitions under ``lib_symbols`` start
        with a name string instead. This is one sheet's symbols and never
        another's: :meth:`kicad_evaltor.hierarchy.SchematicTree.iter_symbols`
        is the traversal, and it stamps each symbol with the sheet it came from.
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


@dataclass(frozen=True)
class FontStyle:
    """The parts of an ``(effects (font ...))`` block that decide how text is drawn."""

    size: float = 1.27
    thickness: float = 0.0
    bold: bool = False


def font_style(effects: SExpr | None) -> FontStyle:
    """Read the font of an ``effects`` block, defaulting what it leaves out.

    KiCad writes bold as ``(bold yes)``; files from before KiCad 7 carry a bare
    ``bold`` atom instead, so both are accepted.
    """
    font = child(effects, "font") if isinstance(effects, list) else None
    if font is None:
        return FontStyle()
    size = _atom_numbers(child(font, "size"))
    thickness = _atom_numbers(child(font, "thickness"))
    bold_node = child(font, "bold")
    bold = "bold" in font[1:] or (bold_node is not None and value_of(bold_node) != "no")
    return FontStyle(
        size=size[0] if size else 1.27,
        thickness=thickness[0] if thickness else 0.0,
        bold=bold,
    )


def _field_from(prop: list[SExpr], name: str, text: str) -> FileField:
    """Read one symbol property into a positioned field."""
    position = _atom_numbers(child(prop, "at"))
    effects = child(prop, "effects")
    font = font_style(effects)
    justify = words_of(child(effects, "justify")) if effects else ""
    return FileField(
        name=name,
        value=text,
        x=position[0] if position else 0.0,
        y=position[1] if len(position) > 1 else 0.0,
        rotation=position[2] if len(position) > 2 else 0.0,
        size=font.size,
        justify=justify or None,
        hidden=is_hidden(prop),
        thickness=font.thickness,
        bold=font.bold,
    )


def load_schematic(
    path: Path,
    cli_path: str | None = None,
    project_dir: Path | None = None,
    instance_path: str = "/",
) -> FileSchematic:
    """Convenience wrapper matching the other loader helpers."""
    return FileSchematic(
        path, cli_path=cli_path, project_dir=project_dir, instance_path=instance_path
    )


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
