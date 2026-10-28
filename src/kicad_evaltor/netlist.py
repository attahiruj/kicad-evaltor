"""The netlist kicad-cli exports, read into plain objects.

Two export formats are understood. ``kicadxml`` is the default because it is
the more complete of the two: ``kicadsexpr`` leaves out every symbol marked
"exclude from board", so a part wired correctly but built off the board (a
battery pack on flying leads, a panel connector) would vanish from the nets.
Net names are reported the same way from both: the s-expression export writes
KiCad's escape tokens (``Net-(U1-XTAL1{slash}PB6)``) where the XML writes the
character, and the tokens are undone here so a name never depends on the
format it came through.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from kicad_evaltor.sexpr import SExpr, child, children, parse, value_of

NetlistFormat = Literal["kicadxml", "kicadsexpr"]
NETLIST_FORMATS: tuple[NetlistFormat, ...] = ("kicadxml", "kicadsexpr")


@dataclass
class FileNetNode:
    """One pin on one net.

    ``pinfunction`` is the pin's name in the symbol (``D+``, ``FB``), which is
    what tells two pins of a part apart; ``pintype`` is its electrical type
    (``input``, ``power_in``, ``passive``). Both are empty when KiCad leaves
    them out, as it does for unnamed pins.

    KiCad 10 writes the pin function as ``<name>_<number>`` (``FB_10``); the
    number is removed here, so ``pinfunction`` is the name as drawn in the
    symbol whichever version exported it.
    """

    ref: str
    pin: str
    net: str
    pinfunction: str = ""
    pintype: str = ""


@dataclass
class FileNet:
    name: str
    nodes: list[FileNetNode]
    code: str = ""


@dataclass
class FileNetComponent:
    """One component as the netlist lists it.

    ``lib_id`` is ``Library:Symbol``. ``fields`` are the user-visible fields
    (Footprint, Datasheet, any custom ones); ``properties`` are everything else
    KiCad attaches, ``ki_keywords`` included. The three flags are the symbol's
    own: do-not-populate, excluded from the BOM, and excluded from the board.
    """

    ref: str
    value: str = ""
    footprint: str = ""
    lib_id: str = ""
    description: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    properties: dict[str, str] = field(default_factory=dict)
    sheet_path: str = "/"
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_board: bool = False


class FileNetlist:
    """Nets and components, with the lookups a check needs.

    Built once from an export and not changed afterwards, so the indexes are
    computed on first use and kept.
    """

    def __init__(
        self,
        nets: list[FileNet],
        components: dict[str, FileNetComponent] | None = None,
    ) -> None:
        self.nets = nets
        self.components: dict[str, FileNetComponent] = components or {}
        self._by_pin: dict[tuple[str, str], FileNetNode] | None = None
        self._by_ref: dict[str, list[FileNetNode]] | None = None
        self._by_name: dict[str, FileNet] | None = None

    def net(self, name: str) -> FileNet | None:
        """The net called ``name``, or None."""
        if self._by_name is None:
            self._by_name = {net.name: net for net in self.nets}
        return self._by_name.get(name)

    def net_named(self, name: str, *, any_sheet: bool = True) -> FileNet | None:
        """The net whose name is ``name`` on whichever sheet it was drawn.

        KiCad prefixes a local or hierarchical label with its sheet path, so
        one design's ground may be ``GND``, ``/GND`` or ``/power/GND``. With
        ``any_sheet`` (the default) a net matches when its name is ``name``
        after taking away a sheet path: ``/``, or the sheet path of any
        component in the netlist (``/power/``). A leading ``/`` on ``name`` is
        ignored. The sheet path has to be a known one, so ``/shared/PA8/PB0``
        (a label ``PA8/PB0`` on sheet ``/shared/``) is not found as ``PB0``;
        a netlist with no components accepts any ``/``-led prefix.

        Returns None when no net matches and raises :class:`AmbiguousNet`,
        naming every candidate, when more than one does: ``GND`` next to a
        local ``/power/GND`` are two nets, and picking one would be a guess.
        With ``any_sheet=False`` this is :meth:`net`.
        """
        if not any_sheet:
            return self.net(name)
        leaf = name[1:] if name.startswith("/") else name
        if not leaf:
            return None
        sheets = {"", "/"} | {c.sheet_path for c in self.components.values()}
        found = [
            net
            for net in self.nets
            if net.name.endswith(leaf)
            and (
                (prefix := net.name[: len(net.name) - len(leaf)]) in sheets
                or (not self.components and prefix.startswith("/") and prefix.endswith("/"))
            )
        ]
        if len(found) > 1:
            raise AmbiguousNet(leaf, tuple(sorted(net.name for net in found)))
        return found[0] if found else None

    def merged(
        self,
        bridge: Callable[[FileNetComponent, Sequence[FileNetNode]], Iterable[Collection[str]]],
    ) -> MergedNetlist:
        """A copy in which the nets joined inside conducting parts are one net.

        ``bridge`` is a connectivity bridge (see :mod:`kicad_evaltor.connectivity`),
        and says which pins of a part conduct, for instance fitted 0 ohm links
        and ferrite beads::

            netlist.merged(any_of(series_parts("R", max_ohms=0), series_parts("FB")))

        A part marked do-not-populate never joins anything, whatever the bridge
        says. Each merged net takes the name of one of its members: a named net
        before an automatic ``Net-(...)`` or ``unconnected-(...)`` one, then
        the alphabetically first. The result's ``raw_names`` maps each merged
        name to the original names it covers, ``merged_name`` maps the other
        way, and ``joined_by`` lists the parts that did the joining, so board
        lookups can still be made by the raw names.
        """
        parent = {net.name: net.name for net in self.nets}

        def root(name: str) -> str:
            while parent[name] != name:
                parent[name] = parent[parent[name]]
                name = parent[name]
            return name

        crossings: list[tuple[str, str]] = []
        for component in self.components.values():
            if component.dnp:
                continue
            pins = self.pins_of(component.ref)
            nets_by_pin = {p.pin: p.net for p in pins}
            for group in bridge(component, pins):
                touched = sorted({nets_by_pin[n] for n in group if n in nets_by_pin})
                if len(touched) < 2:
                    continue
                crossings.append((component.ref, touched[0]))
                for other in touched[1:]:
                    a, b = root(touched[0]), root(other)
                    if a != b:
                        parent[b] = a

        groups: dict[str, list[str]] = {}
        for net in self.nets:
            groups.setdefault(root(net.name), []).append(net.name)
        chosen = {key: min(members, key=_merged_name_key) for key, members in groups.items()}
        raw_names = {chosen[key]: tuple(sorted(members)) for key, members in groups.items()}
        merged_name = {net.name: chosen[root(net.name)] for net in self.nets}

        nets: dict[str, FileNet] = {}
        for net in self.nets:
            name = merged_name[net.name]
            target = nets.setdefault(name, FileNet(name, [], code=net.code))
            if net.name == name:
                target.code = net.code
            target.nodes.extend(
                FileNetNode(n.ref, n.pin, name, n.pinfunction, n.pintype) for n in net.nodes
            )
        joins: dict[str, set[str]] = {}
        for ref, raw in crossings:
            joins.setdefault(merged_name[raw], set()).add(ref)
        return MergedNetlist(
            list(nets.values()),
            dict(self.components),
            raw_names=raw_names,
            merged_name=merged_name,
            joined_by={k: tuple(sorted(v, key=_ref_sort_key)) for k, v in joins.items()},
        )

    def node(self, ref: str, pin: str) -> FileNetNode | None:
        """The node for pin ``pin`` of ``ref``, or None when it is on no net."""
        if self._by_pin is None:
            self._by_pin = {(n.ref, n.pin): n for n in self.iter_nodes()}
        return self._by_pin.get((ref, pin))

    def net_of(self, ref: str, pin: str) -> str | None:
        """The name of the net pin ``pin`` of ``ref`` is on, or None."""
        node = self.node(ref, pin)
        return node.net if node else None

    def pins_of(self, ref: str) -> list[FileNetNode]:
        """Every connected pin of ``ref``, in pin-number order."""
        if self._by_ref is None:
            by_ref: dict[str, list[FileNetNode]] = {}
            for node in self.iter_nodes():
                by_ref.setdefault(node.ref, []).append(node)
            for nodes in by_ref.values():
                nodes.sort(key=lambda n: _pin_sort_key(n.pin))
            self._by_ref = by_ref
        return list(self._by_ref.get(ref, []))

    def nodes_on(self, net: str) -> list[FileNetNode]:
        """Every pin on the net called ``net``; empty when there is no such net."""
        found = self.net(net)
        return list(found.nodes) if found else []

    def iter_nodes(self) -> Iterator[FileNetNode]:
        for net in self.nets:
            yield from net.nodes

    def refs_on(self, net: str) -> set[str]:
        return {n.ref for n in self.nodes_on(net)}

    def pins_named(self, ref: str, *names: str, case_sensitive: bool = True) -> list[FileNetNode]:
        """All matching pins, preserving duplicate roles in multi-channel parts."""
        wanted = set(names if case_sensitive else (n.casefold() for n in names))
        return [
            p
            for p in self.pins_of(ref)
            if (p.pinfunction if case_sensitive else p.pinfunction.casefold()) in wanted
        ]

    def pin_roles(self, ref: str) -> dict[str, tuple[FileNetNode, ...]]:
        roles: dict[str, list[FileNetNode]] = {}
        for pin in self.pins_of(ref):
            roles.setdefault(pin.pinfunction, []).append(pin)
        return {name: tuple(pins) for name, pins in roles.items()}

    def between(self, a: str, b: str, *, predicate=None) -> list[FileNetComponent]:
        """Two-terminal components between nets; no implicit component-kind policy."""
        return [
            c
            for c in self.components.values()
            if (predicate is None or predicate(c))
            and len(self.pins_of(c.ref)) == 2
            and {p.net for p in self.pins_of(c.ref)} == {a, b}
        ]

    def select_components(
        self,
        pattern: str = "*",
        *,
        lib_id: str = "*",
        fields: dict[str, str] | None = None,
        dnp: bool | None = None,
        on_board: bool | None = None,
        in_bom: bool | None = None,
        predicate=None,
    ):
        """Caller-defined selection; imposes no count, sourcing, or DNP policy."""
        from fnmatch import fnmatchcase

        from kicad_evaltor.selection import Selection

        items = tuple(
            c
            for c in self.components.values()
            if fnmatchcase(c.ref, pattern)
            and fnmatchcase(c.lib_id, lib_id)
            and (dnp is None or c.dnp == dnp)
            and (on_board is None or c.exclude_from_board != on_board)
            and (in_bom is None or c.exclude_from_bom != in_bom)
            and all(
                k in c.fields and fnmatchcase(c.fields[k], v) for k, v in (fields or {}).items()
            )
            and (predicate is None or predicate(c))
        )
        return Selection(items, pattern, len(self.components))


class AmbiguousNet(LookupError):
    """A net name, read without its sheet path, belongs to more than one net."""

    def __init__(self, name: str, candidates: tuple[str, ...]) -> None:
        self.name = name
        self.candidates = candidates
        super().__init__(
            f"net name {name!r} matches {len(candidates)} nets: {', '.join(candidates)}; "
            "ask by the full name"
        )


class MergedNetlist(FileNetlist):
    """A :class:`FileNetlist` whose nets were joined through conducting parts.

    Made by :meth:`FileNetlist.merged`. Every lookup works as on the original,
    by merged names. ``raw_names`` maps each merged net to the original nets it
    covers (itself alone when nothing joined it), ``merged_name`` maps an
    original net to its merged one, and ``joined_by`` names, per merged net
    that covers more than one original net, the parts that conduct across it.
    """

    def __init__(
        self,
        nets: list[FileNet],
        components: dict[str, FileNetComponent] | None = None,
        *,
        raw_names: dict[str, tuple[str, ...]],
        merged_name: dict[str, str],
        joined_by: dict[str, tuple[str, ...]],
    ) -> None:
        super().__init__(nets, components)
        self.raw_names = raw_names
        self.merged_name = merged_name
        self.joined_by = joined_by


def _merged_name_key(name: str) -> tuple[int, str]:
    automatic = name.startswith(("Net-(", "unconnected-("))
    return (1 if automatic else 0, name)


def _ref_sort_key(ref: str) -> tuple[str, int]:
    letters = ref.rstrip("0123456789")
    digits = ref[len(letters) :]
    return (letters, int(digits) if digits else -1)


def _pin_sort_key(pin: str) -> tuple[int, int | str]:
    """Numeric pins in number order, then named pins (``A1``, ``EP``) by name."""
    return (0, int(pin)) if pin.isdigit() else (1, pin)


def _strip_pin_numbers(nets: list[FileNet]) -> list[FileNet]:
    """Drop the ``_<number>`` KiCad 10 appends to every pin function.

    Only done when every named pin carries its own number that way, which is
    how a suffixing export is told apart from one where a pin merely happens
    to be named ``VCC_1``.
    """
    named = [n for net in nets for n in net.nodes if n.pinfunction]
    if not named or not all(n.pinfunction.endswith(f"_{n.pin}") for n in named):
        return nets
    for node in named:
        node.pinfunction = node.pinfunction[: -len(node.pin) - 1]
    return nets


class NetlistParseError(ValueError):
    """The exported file was not a netlist in the format asked for."""


def parse_netlist(text: str, fmt: NetlistFormat) -> FileNetlist:
    """Read a netlist exported in ``fmt``."""
    if fmt == "kicadxml":
        return _parse_xml(text)
    if fmt == "kicadsexpr":
        return _parse_sexpr(text)
    raise ValueError(f"unknown netlist format {fmt!r}; expected one of {NETLIST_FORMATS}")


def _parse_xml(text: str) -> FileNetlist:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NetlistParseError(f"unreadable XML netlist: {exc}") from exc

    components: dict[str, FileNetComponent] = {}
    for comp in root.iterfind("./components/comp"):
        ref = comp.get("ref", "")
        if not ref:
            continue
        properties = {
            prop.get("name", ""): prop.get("value", "") for prop in comp.iterfind("property")
        }
        libsource = comp.find("libsource")
        lib, part = (
            (libsource.get("lib", ""), libsource.get("part", ""))
            if libsource is not None
            else ("", "")
        )
        sheetpath = comp.find("sheetpath")
        components[ref] = FileNetComponent(
            ref=ref,
            value=comp.findtext("value", ""),
            footprint=comp.findtext("footprint", ""),
            lib_id=f"{lib}:{part}" if lib and part else part,
            description=comp.findtext("description", ""),
            fields={f.get("name", ""): f.text or "" for f in comp.iterfind("./fields/field")},
            properties=properties,
            sheet_path=sheetpath.get("names", "/") if sheetpath is not None else "/",
            # KiCad writes each flag as a bare, value-less property when set.
            dnp="dnp" in properties,
            exclude_from_bom="exclude_from_bom" in properties,
            exclude_from_board="exclude_from_board" in properties,
        )

    nets: list[FileNet] = []
    for net in root.iterfind("./nets/net"):
        name = net.get("name", "")
        if not name:
            continue
        nodes = [
            FileNetNode(
                ref=node.get("ref", ""),
                pin=node.get("pin", ""),
                net=name,
                pinfunction=node.get("pinfunction", ""),
                pintype=node.get("pintype", ""),
            )
            for node in net.iterfind("node")
            if node.get("ref")
        ]
        nets.append(FileNet(name, nodes, code=net.get("code", "")))
    return FileNetlist(_strip_pin_numbers(nets), components)


def _parse_sexpr(text: str) -> FileNetlist:
    document = parse(text)
    if not isinstance(document, list):
        raise NetlistParseError("unreadable s-expression netlist")

    components: dict[str, FileNetComponent] = {}
    for comp in children(child(document, "components"), "comp"):
        ref = value_of(child(comp, "ref"))
        if not ref:
            continue
        libsource = child(comp, "libsource")
        lib = value_of(child(libsource, "lib"))
        part = value_of(child(libsource, "part"))
        properties = {
            value_of(child(prop, "name")): value_of(child(prop, "value"))
            for prop in children(comp, "property")
        }
        fields = {
            value_of(child(f, "name")): _field_text(f)
            for f in children(child(comp, "fields"), "field")
        }
        components[ref] = FileNetComponent(
            ref=ref,
            value=value_of(child(comp, "value")),
            footprint=value_of(child(comp, "footprint")),
            lib_id=f"{lib}:{part}" if lib and part else part,
            description=value_of(child(comp, "description")),
            fields=fields,
            properties=properties,
            sheet_path=value_of(child(child(comp, "sheetpath"), "names")) or "/",
            dnp="dnp" in properties,
            exclude_from_bom="exclude_from_bom" in properties,
            exclude_from_board="exclude_from_board" in properties,
        )

    nets: list[FileNet] = []
    for net in children(child(document, "nets"), "net"):
        name = unescape_net_name(value_of(child(net, "name")))
        if not name:
            continue
        nodes = []
        for node in children(net, "node"):
            ref = value_of(child(node, "ref"))
            if ref:
                nodes.append(
                    FileNetNode(
                        ref=ref,
                        pin=value_of(child(node, "pin")),
                        net=name,
                        pinfunction=value_of(child(node, "pinfunction")),
                        pintype=value_of(child(node, "pintype")),
                    )
                )
        nets.append(FileNet(name, nodes, code=value_of(child(net, "code"))))
    return FileNetlist(_strip_pin_numbers(nets), components)


_ESCAPES = {
    "{slash}": "/",
    "{backslash}": "\\",
    "{dblquote}": '"',
    "{quote}": "'",
    "{lt}": "<",
    "{gt}": ">",
    "{bar}": "|",
    "{comma}": ",",
    "{colon}": ":",
    "{space}": " ",
    "{dollar}": "$",
    "{tab}": "\t",
    "{return}": "\n",
}
_ESCAPE_TOKEN = re.compile("|".join(re.escape(token) for token in _ESCAPES))


def unescape_net_name(name: str) -> str:
    """Undo KiCad's ``{slash}``-style escapes, leaving ``~{...}`` overbar markup."""
    return _ESCAPE_TOKEN.sub(lambda m: _ESCAPES[m.group(0)], name) if "{" in name else name


def _field_text(node: SExpr) -> str:
    """A ``(field (name "X") "text")`` value: the bare string after the name."""
    if not isinstance(node, list):
        return ""
    for item in node[1:]:
        if isinstance(item, str):
            return item
    return ""
