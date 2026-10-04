"""Subsheet links: what a ``(sheet ...)`` block in a ``.kicad_sch`` says.

A KiCad schematic is a tree of files. Each file draws one sheet, and a
``(sheet ...)`` node on it is a box the reader clicks to open another file.
This module reads those boxes: their name, the file they point at, the pins
they expose to the parent, and the page number the project gives them.

Everything here is pure data plus node-to-object parsing. Nothing reads a file
from disk except :func:`resolve_sheet_path`, which only asks whether a candidate
exists; nothing imports :mod:`kicad_evaltor.schematic_file`, so the file reader
can depend on this module rather than the other way round.

``Sheetfile`` is kept verbatim. Resolving a relative path needs to know which
directory KiCad would have resolved it against, and a normalised absolute path
cannot answer that -- so the raw string is the identity of the link and the
candidates are kept beside it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from kicad_evaltor.sexpr import SExpr, child, children, value_of

# The instance path of the root sheet. KiCad writes it that way in
# `(sheet_instances (path "/" ...))`, and every other path is this plus the
# uuids of the sheets above it.
ROOT_PATH = "/"


@dataclass(frozen=True)
class SheetPin:
    """One pin on a sheet symbol: a named connection point into the child file.

    ``shape`` is kept verbatim from ``(pin "NAME" <shape> ...)`` because KiCad
    spells the five electrical types as ``input``, ``output``,
    ``bidirectional``, ``tri_state`` and ``passive``, and this reader does not
    need to interpret them: ``kicad-cli sch erc`` owns the electrical question.
    """

    name: str
    shape: str
    x: float
    y: float
    rotation: float
    size: float = 1.27


@dataclass(frozen=True)
class HierarchicalLabel:
    """A label inside a child sheet that a parent sheet's pin connects to.

    The net names crossing a sheet boundary come from these, which is why the
    pin_mismatch check pairs them by name.
    """

    name: str
    shape: str
    x: float
    y: float
    rotation: float


@dataclass(frozen=True)
class Sheet:
    """One ``(sheet ...)`` block: a link from this file to a child file.

    ``resolved`` is the first candidate that exists on disk, or ``None``.
    ``candidates`` is every path tried, in order, which is what tells a reader
    *where* the loader looked rather than only that it failed.

    ``path`` is KiCad's uuid instance path: ``/`` plus the uuid of every sheet
    above this one, including itself. It is what the file format uses to say
    which instance of a file a symbol belongs to, and it is the only handle
    that stays unique when one file is instantiated more than once.
    """

    name: str
    filename: str
    resolved: Path | None
    candidates: tuple[Path, ...]
    uuid: str
    x: float
    y: float
    width: float
    height: float
    pages: Mapping[str, str]
    path: str
    pins: tuple[SheetPin, ...]
    properties: Mapping[str, str]

    def pin(self, name: str) -> SheetPin | None:
        """The pin of that name, or None when this sheet exposes no such pin."""
        for pin in self.pins:
            if pin.name == name:
                return pin
        return None

    @property
    def stem(self) -> str:
        """The linked file's stem, which is the other thing a reader may name."""
        return Path(self.filename).stem


def resolve_sheet_path(
    raw: str, *, base_dir: Path, parent_dir: Path
) -> tuple[Path | None, tuple[Path, ...]]:
    """Where a ``Sheetfile`` string points, and every path that was tried.

    KiCad resolves a relative ``Sheetfile`` against the project directory, so
    that is tried first; the referring sheet's own directory is the fallback,
    which is what makes a file that travels outside the project folder work.
    Candidates are deduped and the first one that exists wins.

    Backslashes are normalised before joining. A design authored on Windows
    writes ``..\\shared\\x.kicad_sch``, and on POSIX a literal backslash is an
    ordinary filename character rather than a separator, so without this the
    same link resolves on one platform and silently does not on the other.

    An empty ``Sheetfile`` yields no candidates at all rather than raising:
    an unset filename is a finding for ``sch.sheet.file_missing`` to report,
    not something that should abort a whole run.
    """
    text = raw.replace("\\", "/").strip()
    if not text:
        return None, ()

    if _is_absolute(text):
        candidates: tuple[Path, ...] = (Path(text),)
    else:
        unique: list[Path] = []
        for directory in (base_dir, parent_dir):
            candidate = _normalise(Path(directory) / text)
            if candidate not in unique:
                unique.append(candidate)
        candidates = tuple(unique)

    resolved = next((candidate for candidate in candidates if candidate.exists()), None)
    return resolved, candidates


def _normalise(candidate: Path) -> Path:
    """Collapse ``..`` and ``.`` without touching the filesystem.

    ``project/../shared/shared.kicad_sch`` and ``shared/shared.kicad_sch`` are the
    same file, and a link written the first way has to be recognisable as the same
    link as one written the second -- otherwise dedup misses and two sheets of one
    file look like two different files. ``resolve()`` would also follow symlinks,
    which would then disagree with the ``Sheetfile`` text a user is reading.
    """
    return Path(os.path.normpath(candidate))


def _is_absolute(text: str) -> bool:
    """True for a POSIX ``/x`` or a Windows ``C:\\x``, on any host platform.

    Both are tested on every platform so that a design authored on one system
    and checked on another is read the way its author meant, rather than being
    joined onto the current directory.
    """
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


def join_sheet_path(parent_path: str, uuid: str) -> str:
    """The instance path of a sheet whose parent sits at ``parent_path``."""
    if not uuid:
        return parent_path or ROOT_PATH
    base = (parent_path or ROOT_PATH).rstrip("/")
    return f"{base}/{uuid}"


def sheet_from_node(
    node: list[SExpr], *, base_dir: Path, parent_dir: Path, parent_path: str
) -> Sheet:
    """Read one ``(sheet ...)`` node.

    ``parent_path`` is the instance path of the *document this node lives in*,
    which is what the new sheet's path is derived from. Module-level and
    keyword-only so it can be tested against a literal s-expression without a
    file on disk.
    """
    properties: dict[str, str] = {}
    for prop in children(node, "property"):
        name = value_of(prop, 1)
        if name:
            properties[name] = value_of(prop, 2)

    at = _numbers(child(node, "at"))
    size = _numbers(child(node, "size"))
    uuid = value_of(child(node, "uuid"))
    filename = properties.get("Sheetfile", "")
    resolved, candidates = resolve_sheet_path(filename, base_dir=base_dir, parent_dir=parent_dir)

    return Sheet(
        name=properties.get("Sheetname", ""),
        filename=filename,
        resolved=resolved,
        candidates=candidates,
        uuid=uuid,
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        width=size[0] if size else 0.0,
        height=size[1] if len(size) > 1 else 0.0,
        pages=_pages(node),
        path=join_sheet_path(parent_path, uuid),
        pins=tuple(pin_from_node(pin) for pin in children(node, "pin")),
        properties=properties,
    )


def pin_from_node(node: list[SExpr]) -> SheetPin:
    """Read one ``(pin "NAME" <shape> (at x y angle) ...)`` node."""
    at = _numbers(child(node, "at"))
    effects = child(node, "effects")
    size = value_of(child(child(effects, "font"), "size"))
    return SheetPin(
        name=value_of(node),
        shape=value_of(node, 2),
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        rotation=at[2] if len(at) > 2 else 0.0,
        size=float(size) if size else 1.27,
    )


def label_from_node(node: list[SExpr]) -> HierarchicalLabel:
    """Read one ``(hierarchical_label "NAME" (shape ...) (at x y angle))`` node."""
    at = _numbers(child(node, "at"))
    return HierarchicalLabel(
        name=value_of(node),
        shape=value_of(child(node, "shape")),
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        rotation=at[2] if len(at) > 2 else 0.0,
    )


def _pages(node: list[SExpr]) -> dict[str, str]:
    """The ``(page "N")`` a project gives this sheet instance, per project.

    Only the page number is read. The rest of the ``(instances ...)`` subtree is
    where KiCad records the symbol annotations of an instance, which this reader
    has no use for. The page hangs off ``(path ...)``, not off ``(project ...)``
    directly, because the same project can instantiate a file more than once and
    each instance is numbered on its own.
    """
    pages: dict[str, str] = {}
    for project in children(child(node, "instances"), "project"):
        name = value_of(project)
        if name:
            pages[name] = value_of(child(child(project, "path"), "page"))
    return pages


def _numbers(node: SExpr | None) -> list[float]:
    """The numeric arguments of ``(at x y angle)`` or ``(size w h)``.

    A malformed position yields nothing at all rather than a partial one: keeping
    the numbers that happened to parse would shift a part sideways by one field
    and report a confidently wrong location, which is worse than the zeros the
    caller falls back to.
    """
    if node is None:
        return []
    out: list[float] = []
    for atom in node[1:]:
        if isinstance(atom, list):
            return []
        try:
            out.append(float(atom))
        except ValueError:
            return []
    return out
