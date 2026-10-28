"""KiCad footprint libraries: finding a footprint and comparing copper with it.

KiCad's own comparison (DRC ``lib_footprint_mismatch``, behind
:class:`~kicad_evaltor.checks.pcb.drc_rules.FootprintMatchesLibraryCheck`)
needs the footprint's library nickname. A board written by a script often
stores a bare name (``R_0603_1608Metric``), and then KiCad cannot say which
library it came from. This module resolves such a name across the stock
``.pretty`` libraries (and any ``fp-lib-table`` given), and compares what
matters for assembly: the copper pads, by number, position relative to the
footprint origin, size and shape.

Libraries are found the way :mod:`kicad_evaltor.symbol_library` finds symbols:
a ``KICADn_FOOTPRINT_DIR`` environment variable, the install next to
``kicad-cli``, or the standard install locations.

Pad positions in a board file are in the footprint's own frame, as in the
library, except that a footprint flipped to the back has them mirrored top to
bottom; :func:`copper_pads` undoes that. Pad angles in a board file include
the footprint's rotation, which is taken off here.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from kicad_evaltor.board_file import FileBoard
from kicad_evaltor.sexpr import SExpr, child, children, parse, value_of

FOOTPRINT_DIR_ENV_VARS = (
    "KICAD10_FOOTPRINT_DIR",
    "KICAD9_FOOTPRINT_DIR",
    "KICAD8_FOOTPRINT_DIR",
    "KICAD_FOOTPRINT_DIR",
)


class FootprintNotFound(LookupError):
    """The library, or the footprint inside it, does not exist."""


# --------------------------------------------------------------- discovery


def find_footprint_dirs(cli_path: str | Path | None = None) -> list[Path]:
    """Folders holding KiCad's stock ``.pretty`` libraries, best first.

    Checked in order: the ``KICADn_FOOTPRINT_DIR`` variables, the install that
    ``cli_path`` (or ``kicad-cli`` on ``PATH``) belongs to, then the standard
    install locations for this platform. Only folders that exist are returned.
    """
    candidates: list[Path] = []
    for var in FOOTPRINT_DIR_ENV_VARS:
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))
    cli = str(cli_path) if cli_path else shutil.which("kicad-cli")
    if cli:
        install = Path(cli).resolve().parent.parent
        candidates.extend(
            [
                install / "share" / "kicad" / "footprints",
                install / "SharedSupport" / "footprints",
            ]
        )
    candidates.extend(_standard_dirs())
    found: list[Path] = []
    for path in candidates:
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _standard_dirs() -> list[Path]:
    if sys.platform == "win32":
        root = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "KiCad"
        versions = sorted(root.glob("*"), key=_version_key, reverse=True) if root.is_dir() else []
        return [v / "share" / "kicad" / "footprints" for v in versions]
    if sys.platform == "darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")]
    return [Path("/usr/share/kicad/footprints"), Path("/usr/local/share/kicad/footprints")]


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name)) or (0,)


_VAR = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")


def read_fp_lib_table(path: str | Path, variables: dict[str, str] | None = None) -> dict[str, Path]:
    """The nicknames an ``fp-lib-table`` maps to ``.pretty`` folders.

    ``${VAR}`` is expanded from ``variables``, then the environment. A row of
    type ``Table`` (how KiCad's global table pulls in the stock one) is read in
    turn. Rows whose path still holds an unknown variable are left out.
    """
    lookup = {**os.environ, **(variables or {})}
    nicknames: dict[str, Path] = {}
    _read_table(Path(path), lookup, nicknames, set())
    return nicknames


def _read_table(path: Path, lookup: dict[str, str], into: dict[str, Path], seen: set[Path]) -> None:
    if path in seen or not path.is_file():
        return
    seen.add(path)
    table = parse(path.read_text(encoding="utf-8", errors="replace"))
    for lib in children(table, "lib"):
        name = value_of(child(lib, "name"))
        uri = value_of(child(lib, "uri"))
        if not name or not uri:
            continue
        expanded = _VAR.sub(lambda m: lookup.get(m.group(1) or m.group(2), m.group(0)), uri)
        if "${" in expanded or "$(" in expanded:
            continue
        if value_of(child(lib, "type")).lower() == "table":
            _read_table(Path(expanded), lookup, into, seen)
        else:
            into.setdefault(name, Path(expanded))


# ----------------------------------------------------------------- reading


class FootprintLibraries:
    """Find footprints by ``Nickname:Name`` or by bare name.

    ``dirs`` are folders of ``<nickname>.pretty`` libraries; by default,
    :func:`find_footprint_dirs`. ``tables`` are ``fp-lib-table`` files whose
    nicknames come first, and ``project_dir`` adds that project's own table,
    with ``${KIPRJMOD}`` set to it. ``${KICADn_FOOTPRINT_DIR}`` in a table
    resolves to the first of ``dirs`` when the environment does not set it.
    """

    def __init__(
        self,
        dirs: Iterable[str | Path] | None = None,
        *,
        tables: Iterable[str | Path] = (),
        project_dir: str | Path | None = None,
        cli_path: str | Path | None = None,
    ) -> None:
        self.dirs = [Path(d) for d in dirs] if dirs is not None else find_footprint_dirs(cli_path)
        variables: dict[str, str] = {}
        if self.dirs:
            variables.update(dict.fromkeys(FOOTPRINT_DIR_ENV_VARS, str(self.dirs[0])))
            variables.update({k: os.environ[k] for k in FOOTPRINT_DIR_ENV_VARS if k in os.environ})
        table_paths = [Path(t) for t in tables]
        if project_dir is not None:
            variables["KIPRJMOD"] = str(Path(project_dir))
            table_paths.append(Path(project_dir) / "fp-lib-table")
        self._table: dict[str, Path] = {}
        for path in table_paths:
            for name, target in read_fp_lib_table(path, variables).items():
                self._table.setdefault(name, target)
        self._cache: dict[Path, list[SExpr]] = {}
        self._chips: dict[str, ChipCopper] | None = None

    def library_path(self, nickname: str) -> Path | None:
        """The ``.pretty`` folder a nickname resolves to, or None."""
        if nickname in self._table:
            return self._table[nickname]
        for folder in self.dirs:
            candidate = folder / f"{nickname}.pretty"
            if candidate.is_dir():
                return candidate
        return None

    def nicknames(self) -> list[str]:
        """Every library nickname: the tables' first, then the folders'."""
        names = list(self._table)
        for folder in self.dirs:
            for pretty in sorted(folder.glob("*.pretty")):
                if pretty.stem not in names:
                    names.append(pretty.stem)
        return names

    def find(self, name: str) -> list[tuple[str, Path]]:
        """Every ``(nickname, file)`` holding a footprint called ``name``.

        A ``Nickname:Name`` looks in that library only. A bare name is looked
        for in every library, table libraries first.
        """
        nickname, sep, bare = name.rpartition(":")
        if sep:
            library = self.library_path(nickname)
            file = library / f"{bare}.kicad_mod" if library else None
            return [(nickname, file)] if file is not None and file.is_file() else []
        found = []
        for nick in self.nicknames():
            library = self.library_path(nick)
            if library is not None and (library / f"{name}.kicad_mod").is_file():
                found.append((nick, library / f"{name}.kicad_mod"))
        return found

    def resolve(self, name: str) -> tuple[str, Path]:
        """The first ``(nickname, file)`` for ``name``; see :meth:`find`."""
        found = self.find(name)
        if not found:
            places = [str(d) for d in self.dirs] + sorted(self._table)
            raise FootprintNotFound(
                f"no footprint {name!r} in {', '.join(places) or 'no libraries'}"
            )
        return found[0]

    def load(self, name: str) -> list[SExpr]:
        """The footprint's s-expression from its ``.kicad_mod`` file."""
        _, path = self.resolve(name)
        if path not in self._cache:
            node = parse(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(node, list):
                raise FootprintNotFound(f"{path} holds no footprint")
            self._cache[path] = node
        return self._cache[path]


def footprint_node(board: FileBoard | str | Path, reference: str) -> list[SExpr]:
    """A placed footprint's s-expression, read from the board file."""
    path = board.path if isinstance(board, FileBoard) else Path(board)
    document = parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in children(document, "footprint"):
        for prop in children(node, "property"):
            if value_of(prop) == "Reference" and value_of(prop, 2) == reference:
                return node
    raise FootprintNotFound(f"no footprint {reference} on {path}")


# ----------------------------------------------------------------- copper


@dataclass(frozen=True)
class CopperPad:
    """A pad with copper, in the footprint's own frame (origin, no rotation).

    ``angle`` is the pad's rotation relative to the footprint, in degrees.
    """

    number: str
    kind: str
    shape: str
    x: float
    y: float
    width: float
    height: float
    angle: float = 0.0
    drill: float | None = None


def copper_pads(footprint: list[SExpr]) -> tuple[CopperPad, ...]:
    """The copper pads of a footprint, from a board or a ``.kicad_mod`` file.

    Pads on no copper layer (paste-only apertures, plain NPTH holes) are left
    out: they are not part of what is soldered.

    A footprint flipped to the back is stored with its pads mirrored top to
    bottom in its own frame (pcbnew's flip, either direction, comes down to
    that plus a rotation). That mirror is undone here, so a back-side part
    reads as the library draws it.
    """
    rotation = _numbers(child(footprint, "at"))
    own_angle = rotation[2] if len(rotation) > 2 else 0.0
    back = value_of(child(footprint, "layer")) == "B.Cu"
    pads = []
    for node in children(footprint, "pad"):
        layers = [str(a) for a in (child(node, "layers") or [])[1:] if isinstance(a, str)]
        if not any(layer.endswith(".Cu") for layer in layers):
            continue
        kind = value_of(node, 2)
        if kind == "np_thru_hole":
            continue
        at = _numbers(child(node, "at"))
        size = _numbers(child(node, "size"))
        drill = _numbers(child(node, "drill"))
        y = at[1] if len(at) > 1 else 0.0
        angle = (at[2] if len(at) > 2 else 0.0) - own_angle
        pads.append(
            CopperPad(
                number=value_of(node),
                kind=kind,
                shape=value_of(node, 3),
                x=at[0] if at else 0.0,
                y=-y if back else y,
                width=size[0] if size else 0.0,
                height=size[1] if len(size) > 1 else (size[0] if size else 0.0),
                angle=(-angle if back else angle) % 360,
                drill=drill[0] if drill else None,
            )
        )
    return tuple(pads)


@dataclass(frozen=True)
class PadDifference:
    """One way a board pad differs from the library's.

    ``issue`` is ``"missing"`` (in the library, not on the board), ``"extra"``
    (on the board only), ``"position"``, ``"size"`` or ``"shape"``.
    """

    number: str
    issue: str
    board: CopperPad | None
    library: CopperPad | None
    detail: str = ""


@dataclass(frozen=True)
class CopperComparison:
    """How a board footprint's copper compares with a library footprint.

    ``mirrored`` is ``""`` when the copper matched as it is, or ``"x"``/``"y"``
    when it matched only as the mirror image across that axis (allowed with
    ``allow_flipped``). ``differences`` is empty when the copper matches.
    """

    library_footprint: str
    library_path: Path | None
    mirrored: str
    differences: tuple[PadDifference, ...]

    @property
    def matches(self) -> bool:
        return not self.differences


def compare_copper(
    board_footprint: list[SExpr] | Iterable[CopperPad],
    library_footprint: list[SExpr] | Iterable[CopperPad],
    *,
    allow_flipped: bool = False,
    tolerance_mm: float = 0.01,
    library_name: str = "",
    library_path: Path | None = None,
) -> CopperComparison:
    """Compare two footprints' copper pads, pad by pad.

    Pads pair up by number (several pads may share one, as thermal or shield
    pads do), nearest first. Position is relative to the footprint origin;
    size is compared with each pad's own rotation taken into account, so a
    0.8 x 0.95 pad turned 90 degrees equals a 0.95 x 0.8 one. With
    ``allow_flipped``, a board footprint whose copper is the library's mirror
    image (across either axis) also matches, and ``mirrored`` says which.
    """
    board = _as_pads(board_footprint)
    library = _as_pads(library_footprint)
    best = _differences(board, library, tolerance_mm)
    mirrored = ""
    if best and allow_flipped:
        for axis in ("x", "y"):
            found = _differences(tuple(_mirror(p, axis) for p in board), library, tolerance_mm)
            if len(found) < len(best):
                best, mirrored = found, axis
            if not found:
                break
    return CopperComparison(library_name, library_path, mirrored, tuple(best))


def compare_to_library(
    board: FileBoard | str | Path,
    reference: str,
    *,
    libraries: FootprintLibraries | None = None,
    name: str | None = None,
    allow_flipped: bool = False,
    tolerance_mm: float = 0.01,
) -> CopperComparison:
    """Compare a placed footprint's copper with the library footprint it names.

    The library footprint is resolved by name (``name``, or the placed
    footprint's own) with or without a nickname; a bare name is looked for in
    every library. Raises :class:`FootprintNotFound` when there is none.
    """
    node = footprint_node(board, reference)
    libraries = libraries or FootprintLibraries()
    wanted = name or value_of(node)
    nickname, path = libraries.resolve(wanted)
    library = libraries.load(f"{nickname}:{path.stem}")
    return compare_copper(
        node,
        library,
        allow_flipped=allow_flipped,
        tolerance_mm=tolerance_mm,
        library_name=f"{nickname}:{path.stem}",
        library_path=path,
    )


def _as_pads(footprint: list[SExpr] | Iterable[CopperPad]) -> tuple[CopperPad, ...]:
    if isinstance(footprint, list) and footprint and isinstance(footprint[0], str):
        return copper_pads(footprint)
    return tuple(p for p in footprint if isinstance(p, CopperPad))


def _mirror(pad: CopperPad, axis: str) -> CopperPad:
    x, y = (-pad.x, pad.y) if axis == "x" else (pad.x, -pad.y)
    return CopperPad(
        pad.number,
        pad.kind,
        pad.shape,
        x,
        y,
        pad.width,
        pad.height,
        (-pad.angle) % 360,
        pad.drill,
    )


def _extent(pad: CopperPad) -> tuple[float, float, float]:
    """Width, height and residual angle (0-90) of a pad, rotation folded in."""
    angle = pad.angle % 180
    width, height = pad.width, pad.height
    if angle >= 90 - 1e-6:
        width, height, angle = height, width, angle - 90
    if pad.shape == "circle":
        angle = 0.0
    return width, height, round(angle, 3) % 90


def _differences(
    board: tuple[CopperPad, ...], library: tuple[CopperPad, ...], tolerance: float
) -> list[PadDifference]:
    differences: list[PadDifference] = []
    numbers = sorted({p.number for p in (*board, *library)}, key=_natural)
    for number in numbers:
        ours = [p for p in board if p.number == number]
        theirs = [p for p in library if p.number == number]
        while ours and theirs:
            a, b = min(
                ((a, b) for a in ours for b in theirs),
                key=lambda pair: math.dist((pair[0].x, pair[0].y), (pair[1].x, pair[1].y)),
            )
            ours.remove(a)
            theirs.remove(b)
            offset = math.dist((a.x, a.y), (b.x, b.y))
            if offset > tolerance:
                differences.append(
                    PadDifference(
                        number,
                        "position",
                        a,
                        b,
                        f"at ({a.x:g}, {a.y:g}), library ({b.x:g}, {b.y:g})",
                    )
                )
            wa, ha, ra = _extent(a)
            wb, hb, rb = _extent(b)
            if abs(wa - wb) > tolerance or abs(ha - hb) > tolerance or abs(ra - rb) > 0.01:
                differences.append(
                    PadDifference(number, "size", a, b, f"{wa:g} x {ha:g}, library {wb:g} x {hb:g}")
                )
            if a.shape != b.shape:
                differences.append(
                    PadDifference(number, "shape", a, b, f"{a.shape}, library {b.shape}")
                )
        differences.extend(PadDifference(number, "extra", a, None) for a in ours)
        differences.extend(PadDifference(number, "missing", None, b) for b in theirs)
    return differences


def _natural(text: str) -> tuple[object, ...]:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text))


def _numbers(node: list[SExpr] | None) -> list[float]:
    values = []
    for atom in (node or [])[1:]:
        if isinstance(atom, str):
            try:
                values.append(float(atom))
            except ValueError:
                continue
    return values


# ------------------------------------------------------------ chip sizes


@dataclass(frozen=True)
class ChipCopper:
    """The copper of a two-pad chip footprint, measured along its pad axis.

    ``length_mm`` runs from the outer edge of one pad to the outer edge of the
    other, ``gap_mm`` between their inner edges, and ``width_mm`` across the
    pads (the wider of the two).
    """

    length_mm: float
    width_mm: float
    gap_mm: float


def chip_copper(footprint: list[SExpr] | Iterable[CopperPad]) -> ChipCopper | None:
    """Measure a two-pad footprint's copper; None unless it has exactly two pads."""
    pads = _as_pads(footprint)
    if len(pads) != 2:
        return None
    a, b = pads
    distance = math.dist((a.x, a.y), (b.x, b.y))
    if distance <= 0:
        return None
    axis = math.atan2(b.y - a.y, b.x - a.x)

    def along(pad: CopperPad) -> tuple[float, float]:
        # Extent of the pad along and across the line joining the pads.
        turn = math.radians(pad.angle) - axis
        c, s = abs(math.cos(turn)), abs(math.sin(turn))
        return pad.width * c + pad.height * s, pad.width * s + pad.height * c

    (la, wa), (lb, wb) = along(a), along(b)
    length = distance + la / 2 + lb / 2
    gap = distance - la / 2 - lb / 2
    return ChipCopper(round(length, 4), round(max(wa, wb), 4), round(gap, 4))


@dataclass(frozen=True)
class ChipSizeMatch:
    """The stock chip footprint a two-pad copper pattern matches best."""

    size: str
    """Imperial size code, e.g. ``"0603"``."""
    metric: str
    """Metric size code, e.g. ``"1608"``."""
    footprint: str
    """The stock footprint matched, e.g. ``"Resistor_SMD:R_0603_1608Metric"``."""
    error_mm: float
    """The largest difference in length, width or gap from that footprint."""


_CHIP_NAME = re.compile(r"^[RCL]_(\d{4})_(\d{4})Metric(?:_.*)?$")
_CHIP_LIBRARIES = ("Resistor_SMD", "Capacitor_SMD", "Inductor_SMD")


def stock_chip_footprints(
    libraries: FootprintLibraries | None = None,
) -> dict[str, ChipCopper]:
    """Every stock ``R_*``/``C_*``/``L_*`` chip footprint in ``Resistor_SMD``,
    ``Capacitor_SMD`` and ``Inductor_SMD`` (hand-solder variants included), by
    ``Nickname:Name``. Inductors bring sizes the other two lack, such as 1008."""
    libraries = libraries or FootprintLibraries()
    if libraries._chips is not None:
        return dict(libraries._chips)
    found: dict[str, ChipCopper] = {}
    for nickname in _CHIP_LIBRARIES:
        folder = libraries.library_path(nickname)
        if folder is None:
            continue
        for file in sorted(folder.glob("*.kicad_mod")):
            if not _CHIP_NAME.match(file.stem):
                continue
            measured = chip_copper(libraries.load(f"{nickname}:{file.stem}"))
            if measured is not None:
                found[f"{nickname}:{file.stem}"] = measured
    libraries._chips = found
    return dict(found)


def chip_size_from_copper(
    footprint: list[SExpr] | Iterable[CopperPad],
    *,
    libraries: FootprintLibraries | None = None,
    tolerance_mm: float = 0.05,
) -> ChipSizeMatch | None:
    """The stock chip size (0201, 0402, 0603, 0805, 1206...) whose copper this is.

    The footprint's two pads are measured (:func:`chip_copper`) and compared
    with every stock chip resistor, capacitor and inductor footprint; the closest one
    within ``tolerance_mm`` on length, width and gap wins. None for a
    footprint that is not two pads, or matches no stock chip that closely. The
    name of the footprint plays no part, so a renamed or custom footprint is
    sized by what is actually on the board.
    """
    measured = chip_copper(footprint)
    if measured is None:
        return None
    best: ChipSizeMatch | None = None
    for name, stock in stock_chip_footprints(libraries).items():
        error = max(
            abs(measured.length_mm - stock.length_mm),
            abs(measured.width_mm - stock.width_mm),
            abs(measured.gap_mm - stock.gap_mm),
        )
        if error > tolerance_mm or (best is not None and error >= best.error_mm):
            continue
        match = _CHIP_NAME.match(name.split(":", 1)[1])
        assert match is not None
        best = ChipSizeMatch(match.group(1), match.group(2), name, round(error, 4))
    return best
