"""Small, faithful edits to a schematic or board file, for building test fixtures.

A fixture is easiest to make by changing a reference design: put a different
part in, change a value, embed the real library symbol. The edit has to leave
a file KiCad accepts *for the reason the fixture is about*. If a swapped part's
embedded copy is not exactly what the library holds, ERC raises
``lib_symbol_mismatch`` and the fixture fails for the wrong reason, so symbols
are always embedded from :class:`~kicad_evaltor.symbol_library.SymbolLibraries`
with ``extends`` resolved.

The same goes for a board: a failing case is made by taking a real, clean board
and breaking one thing (delete a net's tracks, remove a part, move a part onto
another), so everything else about it stays exactly as KiCad wrote it.

Everything works on the file's s-expression tree in memory.
:func:`load_document` and :func:`save_document` read and write it, and nothing
here touches a file it was not asked to write.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from kicad_evaltor.board_file import _net_name, net_codes
from kicad_evaltor.sexpr import Quoted, SExpr, child, children, dumps, head, parse, value_of
from kicad_evaltor.symbol_library import SymbolLibraries, symbol_signature


class EditError(ValueError):
    """The edit cannot be made to this schematic or board."""


def load_document(path: str | Path) -> list[SExpr]:
    """A schematic or board file's s-expression tree, ready to edit."""
    document = parse(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, list) or head(document) not in ("kicad_sch", "kicad_pcb"):
        raise EditError(f"Not a KiCad schematic or board: {path}")
    return document


def save_document(document: list[SExpr], path: str | Path) -> Path:
    """Write the tree to ``path``, creating its folder; returns the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(document), encoding="utf-8")
    return target


def placed_symbols(document: list[SExpr], reference: str) -> list[list[SExpr]]:
    """Every placed instance of ``reference``: one per unit of a multi-unit part.

    The reference is matched against the ``Reference`` property and against
    each hierarchical instance path, so a symbol in a reused sheet is found by
    any of the references it is given.
    """
    found = []
    for node in children(document, "symbol"):
        if child(node, "lib_id") is None:
            continue
        if _property(node, "Reference") == reference or reference in _instance_refs(node):
            found.append(node)
    return found


def embed_symbol(document: list[SExpr], lib_id: str, libs: SymbolLibraries) -> list[SExpr]:
    """Put the library's ``lib_id`` into ``lib_symbols``, replacing any copy.

    Returns the embedded node. The copy is flattened and named the way KiCad
    names it, so ERC compares it equal to the library.
    """
    table = _lib_symbols(document)
    embedded = libs.embeddable(lib_id)
    for i, node in enumerate(table):
        if isinstance(node, list) and head(node) == "symbol" and value_of(node) == lib_id:
            table[i] = embedded
            return embedded
    table.append(embedded)
    table[1:] = sorted(table[1:], key=lambda n: value_of(n) if isinstance(n, list) else "")
    return embedded


def swap_symbol(
    document: list[SExpr],
    reference: str,
    lib_id: str,
    libs: SymbolLibraries,
    *,
    update_value: bool | None = None,
) -> None:
    """Make ``reference`` an instance of the library symbol ``lib_id``.

    Every unit of the part is switched, the new symbol is embedded, and the old
    copy is dropped once nothing places it. Each instance's per-pin uuids are
    rebuilt for the new pins, keeping the uuid of any pin number that carries
    over. Position, other fields and the reference are kept.

    ``update_value`` decides whether Value becomes the new symbol's default.
    Left as None, it does so only when Value still is the old symbol's default:
    a ``2N7002`` swapped for a ``BSS138`` is renamed, a ``10k`` resistor swapped
    for another resistor symbol stays ``10k``.
    """
    instances = placed_symbols(document, reference)
    if not instances:
        raise EditError(f"no placed symbol {reference!r}")
    old_defaults = {
        value_of(n): _property(n, "Value")
        for n in _lib_symbols(document)[1:]
        if isinstance(n, list) and head(n) == "symbol"
    }

    embedded = embed_symbol(document, lib_id, libs)
    signature = symbol_signature(embedded, include_graphics=False)
    old_names = set()
    for node in instances:
        old_name = value_of(child(node, "lib_name")) or value_of(child(node, "lib_id"))
        old_names.add(old_name)
        rename = (
            update_value
            if update_value is not None
            else _property(node, "Value") == old_defaults.get(old_name)
        )
        _remove(node, "lib_name")
        lib_id_node = child(node, "lib_id")
        assert lib_id_node is not None  # placed_symbols only returns these
        lib_id_node[1] = Quoted(lib_id)
        unit = int(float(value_of(child(node, "unit")) or 1))
        style = int(float(value_of(child(node, "body_style")) or 1))
        numbers = [
            p.number for p in signature.pins if p.unit in (0, unit) and p.body_style in (0, style)
        ]
        _rebuild_pins(node, list(dict.fromkeys(numbers)))
        if rename:
            set_property(node, "Value", _property(embedded, "Value"))

    still_used = {
        value_of(child(n, "lib_name")) or value_of(child(n, "lib_id"))
        for n in children(document, "symbol")
        if child(n, "lib_id") is not None
    }
    table = _lib_symbols(document)
    table[:] = [
        n
        for n in table
        if not (
            isinstance(n, list)
            and head(n) == "symbol"
            and value_of(n) in old_names - still_used - {lib_id}
        )
    ]


def set_property(node: list[SExpr], name: str, value: str) -> None:
    """Set a field's text on a placed symbol (or any node with properties).

    The field must already exist; a new field needs a position, which only the
    caller can choose.
    """
    for prop in children(node, "property"):
        if value_of(prop) == name:
            prop[2] = Quoted(value)
            return
    raise EditError(f"no {name!r} field to set")


def set_symbol_property(document: list[SExpr], reference: str, name: str, value: str) -> None:
    """:func:`set_property` on every placed unit of ``reference``."""
    instances = placed_symbols(document, reference)
    if not instances:
        raise EditError(f"no placed symbol {reference!r}")
    for node in instances:
        set_property(node, name, value)


def _lib_symbols(document: list[SExpr]) -> list[SExpr]:
    table = child(document, "lib_symbols")
    if table is None:
        table = ["lib_symbols"]
        document.insert(_after_header(document), table)
    return table


def _after_header(document: list[SExpr]) -> int:
    """Where ``lib_symbols`` belongs: after version, generator, uuid, paper, title."""
    index = 1
    for i, node in enumerate(document[1:], start=1):
        if isinstance(node, list) and head(node) in (
            "version",
            "generator",
            "generator_version",
            "uuid",
            "paper",
            "title_block",
        ):
            index = i + 1
    return index


def _property(node: list[SExpr], name: str) -> str:
    for prop in children(node, "property"):
        if value_of(prop) == name:
            return value_of(prop, 2)
    return ""


def _instance_refs(node: list[SExpr]) -> set[str]:
    refs = set()
    for project in children(child(node, "instances"), "project"):
        for path in children(project, "path"):
            ref = value_of(child(path, "reference"))
            if ref:
                refs.add(ref)
    return refs


def _remove(node: list[SExpr], keyword: str) -> None:
    node[:] = [n for n in node if not (isinstance(n, list) and head(n) == keyword)]


def _rebuild_pins(node: list[SExpr], numbers: list[str]) -> None:
    old = {value_of(p): p for p in children(node, "pin")}
    insert_at = next(
        (i for i, n in enumerate(node) if isinstance(n, list) and head(n) == "pin"),
        None,
    )
    _remove(node, "pin")
    if insert_at is None:
        instances = next(
            (i for i, n in enumerate(node) if isinstance(n, list) and head(n) == "instances"),
            len(node),
        )
        insert_at = instances
    pins: list[SExpr] = [
        old.get(number) or ["pin", Quoted(number), ["uuid", Quoted(str(uuid.uuid4()))]]
        for number in numbers
    ]
    node[insert_at:insert_at] = pins


def board_footprint(document: list[SExpr], reference: str) -> list[SExpr]:
    """The placed footprint whose Reference is ``reference``."""
    for node in children(document, "footprint"):
        if _property(node, "Reference") == reference:
            return node
    raise EditError(f"No footprint {reference} on the board")


def remove_footprint(document: list[SExpr], reference: str) -> None:
    """Delete a footprint from the board, leaving its tracks dangling."""
    document.remove(board_footprint(document, reference))


def move_footprint(document: list[SExpr], reference: str, x: float, y: float) -> None:
    """Move a footprint's origin to ``(x, y)`` mm, keeping its rotation.

    Pad and field positions are stored relative to the footprint, so they move
    with it; tracks do not, which is what makes this a useful breakage.
    """
    at = child(board_footprint(document, reference), "at")
    if at is None:
        raise EditError(f"Footprint {reference} has no position")
    at[1:3] = [_number(x), _number(y)]


def remove_net_tracks(document: list[SExpr], net: str) -> int:
    """Delete every track segment and arc on ``net``; returns how many went."""
    codes = net_codes(document)
    doomed = [
        node
        for node in document
        if isinstance(node, list)
        and head(node) in ("segment", "arc")
        and _net_name(child(node, "net"), codes) == net
    ]
    for node in doomed:
        document.remove(node)
    return len(doomed)


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")
