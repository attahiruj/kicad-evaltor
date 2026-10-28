"""KiCad symbol libraries: finding them, reading symbols, and comparing them.

A schematic embeds a copy of every symbol it places, under ``lib_symbols``.
Questions about where a symbol came from need the library side too: "is this
R1 an unchanged stock resistor?" compares the embedded copy against the
library; "give me a stock symbol to put in a fixture" reads it out.

Libraries are found the way KiCad finds them: a ``KICADn_SYMBOL_DIR``
environment variable, the install next to ``kicad-cli``, or the standard
install locations. A project's own ``sym-lib-table`` can add or remap
nicknames.

A symbol defined with ``(extends "Parent")`` holds only its own fields; its
pins and body are the parent's. :func:`flatten_symbol` resolves that into one
standalone symbol, the same way KiCad does before it embeds one in a schematic.

Comparison is by :func:`symbol_signature`, which keeps what makes a symbol
electrically and visually what it is (pins, their names and types, the body)
and ignores what may legitimately differ between copies: field text, uuids
and names.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from kicad_evaltor.sexpr import Quoted, SExpr, child, children, head, is_hidden, parse, value_of

SYMBOL_DIR_ENV_VARS = (
    "KICAD10_SYMBOL_DIR",
    "KICAD9_SYMBOL_DIR",
    "KICAD8_SYMBOL_DIR",
    "KICAD_SYMBOL_DIR",
)


class SymbolNotFound(LookupError):
    """The library, or the symbol inside it, does not exist."""


# --------------------------------------------------------------- discovery


def find_symbol_dirs(cli_path: str | Path | None = None) -> list[Path]:
    """Folders holding KiCad's stock ``.kicad_sym`` libraries, best first.

    Checked in order: the ``KICADn_SYMBOL_DIR`` variables, the install that
    ``cli_path`` (or ``kicad-cli`` on ``PATH``) belongs to, then the standard
    install locations for this platform, newest version first. Only folders
    that exist are returned.
    """
    candidates: list[Path] = []
    for var in SYMBOL_DIR_ENV_VARS:
        if os.environ.get(var):
            candidates.append(Path(os.environ[var]))

    cli = str(cli_path) if cli_path else shutil.which("kicad-cli")
    if cli:
        candidates.extend(_dirs_near_cli(Path(cli)))

    candidates.extend(_standard_dirs())

    found: list[Path] = []
    for path in candidates:
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _dirs_near_cli(cli: Path) -> list[Path]:
    install = cli.resolve().parent.parent
    return [
        install / "share" / "kicad" / "symbols",  # Windows and Linux layouts
        install / "SharedSupport" / "symbols",  # macOS: Contents/MacOS/kicad-cli
    ]


def _standard_dirs() -> list[Path]:
    if sys.platform == "win32":
        root = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "KiCad"
        versions = sorted(root.glob("*"), key=_version_key, reverse=True) if root.is_dir() else []
        return [v / "share" / "kicad" / "symbols" for v in versions]
    if sys.platform == "darwin":
        return [
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"),
        ]
    return [
        Path("/usr/share/kicad/symbols"),
        Path("/usr/local/share/kicad/symbols"),
    ]


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", path.name)) or (0,)


_VAR = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")


def read_lib_table(path: Path, variables: dict[str, str] | None = None) -> dict[str, Path]:
    """The nicknames a ``sym-lib-table`` file maps, with ``${VAR}`` expanded.

    ``variables`` are looked up before the environment, which is how
    ``${KIPRJMOD}`` is supplied. A row whose path still holds an unknown
    variable is left out, since there is no file to point it at.
    """
    table = parse(path.read_text(encoding="utf-8", errors="replace"))
    lookup = {**os.environ, **(variables or {})}
    nicknames: dict[str, Path] = {}
    for lib in children(table, "lib"):
        name = value_of(child(lib, "name"))
        uri = value_of(child(lib, "uri"))
        if not name or not uri:
            continue
        expanded = _VAR.sub(lambda m: lookup.get(m.group(1) or m.group(2), m.group(0)), uri)
        if "${" in expanded or "$(" in expanded:
            continue
        nicknames[name] = Path(expanded)
    return nicknames


# ----------------------------------------------------------------- reading


class SymbolLibraries:
    """Read symbols from a set of libraries by ``Nickname:Symbol`` id.

    ``dirs`` are folders of ``<nickname>.kicad_sym`` files (or
    ``<nickname>.kicad_symdir`` folders); by default, :func:`find_symbol_dirs`.
    ``tables`` are ``sym-lib-table`` files whose nicknames take precedence over
    the folders, and ``project_dir`` adds that project's own table, with
    ``${KIPRJMOD}`` set to it. Libraries are read on first use and kept.
    """

    def __init__(
        self,
        dirs: Iterable[str | Path] | None = None,
        *,
        tables: Iterable[str | Path] = (),
        project_dir: str | Path | None = None,
        cli_path: str | Path | None = None,
    ) -> None:
        self.dirs = [Path(d) for d in dirs] if dirs is not None else find_symbol_dirs(cli_path)
        self._table: dict[str, Path] = {}
        table_paths = [Path(t) for t in tables]
        variables: dict[str, str] = {}
        if project_dir is not None:
            variables["KIPRJMOD"] = str(Path(project_dir))
            project_table = Path(project_dir) / "sym-lib-table"
            if project_table.is_file():
                table_paths.append(project_table)
        for path in table_paths:
            self._table.update(read_lib_table(path, variables))
        self._libraries: dict[str, dict[str, list[SExpr]]] = {}
        self._index: dict[str, tuple[str, ...]] | None = None

    def library_path(self, nickname: str) -> Path | None:
        """The file or folder a nickname resolves to, or None."""
        if nickname in self._table:
            return self._table[nickname]
        for folder in self.dirs:
            for candidate in (
                folder / f"{nickname}.kicad_sym",
                folder / f"{nickname}.kicad_symdir",
            ):
                if candidate.exists():
                    return candidate
        return None

    def symbols(self, nickname: str) -> dict[str, list[SExpr]]:
        """Every symbol in a library, by name, as written (``extends`` unresolved)."""
        if nickname not in self._libraries:
            path = self.library_path(nickname)
            if path is None:
                raise SymbolNotFound(f"no symbol library {nickname!r} in {self._searched()}")
            self._libraries[nickname] = _read_library(path)
        return self._libraries[nickname]

    def get(self, lib_id: str) -> list[SExpr]:
        """The symbol as the library writes it, ``extends`` and all."""
        nickname, name = split_lib_id(lib_id)
        found = self.symbols(nickname).get(name)
        if found is None:
            raise SymbolNotFound(f"{name!r} is not in library {nickname!r}")
        return found

    def resolve(self, lib_id: str) -> list[SExpr]:
        """The symbol with ``extends`` resolved, still named as in the library.

        A new tree every call, so the caller may change it freely.
        """
        nickname, name = split_lib_id(lib_id)
        library = self.symbols(nickname)
        if name not in library:
            raise SymbolNotFound(f"{name!r} is not in library {nickname!r}")
        return flatten_symbol(name, library.get)

    def embeddable(self, lib_id: str) -> list[SExpr]:
        """The symbol as a schematic embeds it in ``lib_symbols``.

        Flattened and renamed ``Nickname:Symbol``, which is what KiCad compares
        a placed symbol against; a copy that differs raises
        ``lib_symbol_mismatch`` in ERC.
        """
        resolved = self.resolve(lib_id)
        resolved[1] = Quoted(lib_id)
        return resolved

    def nicknames(self) -> list[str]:
        """Every library nickname on offer: the tables' first, then each folder's."""
        names = list(self._table)
        for folder in self.dirs:
            for path in sorted(folder.glob("*.kicad_sym")) + sorted(folder.glob("*.kicad_symdir")):
                if path.stem not in names:
                    names.append(path.stem)
        return names

    def symbol_index(self) -> dict[str, tuple[str, ...]]:
        """Every symbol name in every library, mapped to the ``lib_id``s holding it.

        Built on first use by scanning the files for their top-level symbols
        rather than parsing them, which takes a fraction of a second for the
        whole stock set, and kept. A library that cannot be read is left out.
        """
        if self._index is None:
            index: dict[str, list[str]] = {}
            for nickname in self.nicknames():
                path = self.library_path(nickname)
                if path is None:
                    continue
                try:
                    names = _symbol_names(path)
                except OSError:
                    continue
                for name in names:
                    index.setdefault(name, []).append(f"{nickname}:{name}")
            self._index = {name: tuple(ids) for name, ids in index.items()}
        return self._index

    def lib_ids_named(self, name: str) -> tuple[str, ...]:
        """The ``Nickname:Symbol`` ids of every library symbol called ``name``.

        How a copied symbol (``Logger:BQ24074RGT``) is traced back to the
        stock one (``Battery_Management:BQ24074RGT``) by name alone.
        """
        return self.symbol_index().get(name, ())

    def __contains__(self, lib_id: object) -> bool:
        if not isinstance(lib_id, str):
            return False
        try:
            self.get(lib_id)
        except (SymbolNotFound, ValueError):
            return False
        return True

    def _searched(self) -> str:
        places = [str(d) for d in self.dirs] + sorted(self._table)
        return ", ".join(places) if places else "no library folders (set KICAD10_SYMBOL_DIR)"


def split_lib_id(lib_id: str) -> tuple[str, str]:
    """``("Device", "R")`` from ``"Device:R"``."""
    nickname, sep, name = lib_id.partition(":")
    if not sep or not nickname or not name:
        raise ValueError(f"lib_id must be 'Library:Symbol', got {lib_id!r}")
    return nickname, name


def _read_library(path: Path) -> dict[str, list[SExpr]]:
    files = sorted(path.glob("*.kicad_sym")) if path.is_dir() else [path]
    symbols: dict[str, list[SExpr]] = {}
    for file in files:
        document = parse(file.read_text(encoding="utf-8", errors="replace"))
        for symbol in children(document, "symbol"):
            name = value_of(symbol)
            if name:
                symbols[name] = symbol
    return symbols


_TOP_SYMBOL = re.compile(rb'^\t\(symbol "((?:[^"\\]|\\.)*)"', re.M)


def _symbol_names(path: Path) -> list[str]:
    """Top-level symbol names in a library file or ``.kicad_symdir`` folder.

    KiCad writes each top-level symbol one tab in, its units two; a file
    written some other way is parsed instead.
    """
    files = sorted(path.glob("*.kicad_sym")) if path.is_dir() else [path]
    names: list[str] = []
    for file in files:
        data = file.read_bytes()
        found = [m.group(1).decode("utf-8", "replace") for m in _TOP_SYMBOL.finditer(data)]
        if not found and b"(symbol" in data:
            found = list(_read_library(file))
        names.extend(found)
    return names


# -------------------------------------------------------------- flattening

# Nodes merged by their own rules rather than as plain settings.
_INHERITED_SKIP = ("extends", "property", "symbol")


def flatten_symbol(name: str, lookup: Callable[[str], list[SExpr] | None]) -> list[SExpr]:
    """Resolve ``extends`` for the symbol called ``name``.

    ``lookup`` returns a symbol of the same library by name. The result takes
    the root ancestor's settings, units and graphics; the derived symbol's own
    settings replace the ancestor's; its properties replace same-named ones in
    place and the rest follow; and the units are renamed after ``name``. A
    symbol without ``extends`` comes back as a copy of itself.

    Raises :class:`SymbolNotFound` for a missing parent and ``ValueError`` for
    a cycle.
    """
    return _flatten(name, lookup, ())


def _flatten(
    name: str, lookup: Callable[[str], list[SExpr] | None], seen: tuple[str, ...]
) -> list[SExpr]:
    if name in seen:
        raise ValueError(f"symbol inheritance cycle: {' -> '.join((*seen, name))}")
    own = lookup(name)
    if own is None:
        raise SymbolNotFound(
            f"symbol {name!r} not found (needed by {seen[-1]!r})" if seen else name
        )
    parent_name = value_of(child(own, "extends"))
    if not parent_name:
        return copy.deepcopy(own)

    parent = _flatten(parent_name, lookup, (*seen, name))
    merged: list[SExpr] = ["symbol", Quoted(name)]

    own_settings = {
        head(n): n for n in own[2:] if isinstance(n, list) and head(n) not in _INHERITED_SKIP
    }
    for node in parent[2:]:
        if isinstance(node, list) and head(node) not in ("property", "symbol", "embedded_fonts"):
            merged.append(copy.deepcopy(own_settings.pop(head(node), node)))
    leftover_settings = [n for n in own_settings.values() if head(n) != "embedded_fonts"]
    merged.extend(copy.deepcopy(leftover_settings))

    own_props = {value_of(p): p for p in children(own, "property")}
    for prop in children(parent, "property"):
        merged.append(copy.deepcopy(own_props.pop(value_of(prop), prop)))
    merged.extend(copy.deepcopy(list(own_props.values())))

    for unit in children(parent, "symbol"):
        renamed = copy.deepcopy(unit)
        suffix = value_of(unit)[len(parent_name) :]
        renamed[1] = Quoted(name + suffix)
        merged.append(renamed)

    fonts = child(own, "embedded_fonts") or child(parent, "embedded_fonts")
    if fonts is not None:
        merged.append(copy.deepcopy(fonts))
    return merged


# --------------------------------------------------------------- signature


@dataclass(frozen=True, order=True)
class PinSignature:
    unit: int
    body_style: int
    number: str
    name: str
    electrical_type: str
    shape: str
    x: float
    y: float
    angle: float
    length: float
    hidden: bool


@dataclass(frozen=True)
class SymbolSignature:
    """What makes a symbol the symbol it is, minus names, fields and uuids.

    Two signatures are equal when the symbols have the same pins (per unit and
    body style: number, name, electrical type, graphic shape, position,
    orientation, length, visibility) and, unless built without graphics, the
    same body drawing.
    """

    pins: tuple[PinSignature, ...]
    graphics: tuple[tuple[int, int, str], ...] = field(default=())
    power: bool = False


_GRAPHIC_HEADS = ("rectangle", "polyline", "circle", "arc", "bezier", "text", "text_box")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def symbol_signature(node: list[SExpr], *, include_graphics: bool = True) -> SymbolSignature:
    """The comparable signature of a flattened symbol.

    ``node`` must already be flattened (see :func:`flatten_symbol`); a symbol
    that still says ``extends`` has no pins of its own to compare.
    """
    if child(node, "extends") is not None:
        raise ValueError("flatten the symbol before taking its signature")
    pins: list[PinSignature] = []
    graphics: list[tuple[int, int, str]] = []
    for unit_node in children(node, "symbol"):
        unit, style = _unit_of(value_of(unit_node))
        for pin in children(unit_node, "pin"):
            pins.append(_pin_signature(pin, unit, style))
        if include_graphics:
            for item in unit_node[2:]:
                if isinstance(item, list) and head(item) in _GRAPHIC_HEADS:
                    graphics.append((unit, style, _canonical(item)))
    return SymbolSignature(
        pins=tuple(sorted(pins)),
        graphics=tuple(sorted(graphics)),
        power=child(node, "power") is not None,
    )


def equivalent_symbols(
    embedded: list[SExpr],
    trusted: SymbolLibraries,
    candidates: Iterable[str],
    *,
    include_graphics: bool = True,
) -> tuple[str, ...]:
    """Equivalent definitions among caller-selected trusted candidates.

    Library nicknames and embedded names do not establish fidelity. Missing
    trusted definitions raise SymbolNotFound rather than prove inequality.
    """
    signature = symbol_signature(embedded, include_graphics=include_graphics)
    return tuple(
        lib_id
        for lib_id in candidates
        if symbol_signature(trusted.resolve(lib_id), include_graphics=include_graphics) == signature
    )


def _unit_of(unit_name: str) -> tuple[int, int]:
    parts = unit_name.rsplit("_", 2)
    try:
        return int(parts[-2]), int(parts[-1])
    except (IndexError, ValueError):
        return 0, 0


def _pin_signature(pin: list[SExpr], unit: int, style: int) -> PinSignature:
    at = [_num(a) for a in (child(pin, "at") or [])[1:]]
    return PinSignature(
        unit=unit,
        body_style=style,
        number=value_of(child(pin, "number")),
        name=value_of(child(pin, "name")),
        electrical_type=value_of(pin, 1),
        shape=value_of(pin, 2),
        x=at[0] if at else 0.0,
        y=at[1] if len(at) > 1 else 0.0,
        angle=at[2] % 360 if len(at) > 2 else 0.0,
        length=_num(value_of(child(pin, "length"), default="0")),
        hidden=is_hidden(pin),
    )


def _num(atom: SExpr) -> float:
    try:
        return round(float(atom), 4) if isinstance(atom, str) else 0.0
    except ValueError:
        return 0.0


def _canonical(node: SExpr) -> str:
    """A graphic as text with numbers normalised and uuids dropped."""
    if isinstance(node, list):
        if head(node) == "uuid":
            return ""
        return "(" + " ".join(s for s in (_canonical(n) for n in node) if s) + ")"
    if _NUMBER.match(node):
        return f"{float(node):g}"
    return node


def symbol_diff(expected: SymbolSignature, actual: SymbolSignature) -> list[str]:
    """Readable differences between two signatures; empty when they match."""
    problems: list[str] = []

    def key(p: PinSignature) -> tuple[int, int, str]:
        return (p.unit, p.body_style, p.number)

    want = {key(p): p for p in expected.pins}
    have = {key(p): p for p in actual.pins}
    for k in sorted(want.keys() - have.keys()):
        problems.append(f"missing pin {_pin_label(want[k])}")
    for k in sorted(have.keys() - want.keys()):
        problems.append(f"extra pin {_pin_label(have[k])}")
    for k in sorted(want.keys() & have.keys()):
        a, b = want[k], have[k]
        for attr in ("name", "electrical_type", "shape", "x", "y", "angle", "length", "hidden"):
            if getattr(a, attr) != getattr(b, attr):
                problems.append(
                    f"pin {_pin_label(a)} {attr}: expected {getattr(a, attr)!r}, "
                    f"found {getattr(b, attr)!r}"
                )
    if expected.graphics != actual.graphics:
        missing = len(set(expected.graphics) - set(actual.graphics))
        extra = len(set(actual.graphics) - set(expected.graphics))
        problems.append(f"body drawing differs: {missing} shape(s) missing, {extra} extra")
    if expected.power != actual.power:
        problems.append(f"power symbol: expected {expected.power}, found {actual.power}")
    return problems


def _pin_label(pin: PinSignature) -> str:
    unit = f" (unit {pin.unit})" if pin.unit else ""
    name = f" {pin.name}" if pin.name and pin.name != "~" else ""
    return f"{pin.number}{name}{unit}"
