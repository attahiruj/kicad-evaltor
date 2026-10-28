"""Structured facts about the findings in KiCad's ERC and DRC reports.

A KiCad report names the items behind each finding only in English text:
``Symbol #FLG02 Pin 1 [Power output, Line]``, ``Pad 2 [/SDA] of J1 on F.Cu``,
``Footprint H1``. This module reads those descriptions once, so a caller can
filter findings on fields (the pin type, the owning reference, the symbol's
library id) instead of matching text, and decide its own allowances::

    def flag_on_bidirectional(finding):
        types = sorted(item["pin_type"] for item in finding["items"])
        return finding["type"] == "pin_to_pin" and types == ["Bidirectional", "Power output"]

The formats parsed are those kicad-cli 10 writes, collected from its ERC and
DRC reports on real projects (KiCad's demos and the test fixtures):

ERC
    ``Symbol U5 Pin 1 [SDO/SA0, Bidirectional, Line]`` (name, type, shape),
    ``Symbol #FLG02 Pin 1 [Power output, Line]`` (an unnamed pin),
    ``Symbol #PWR02 Hidden pin 1 [+12V, Power input, Line]``,
    ``Symbol C107 [C]`` (the symbol and its value).
DRC
    ``Pad 2 [/NET] of J1 on F.Cu``, ``PTH pad 1 [+BATT] of J2``,
    ``NPTH pad of H4``, ``Footprint J1``, ``Reference field of J9``,
    ``Value field of POLY (VAL**)``, ``Segment of J1 on F.Silkscreen``,
    ``Polygon [<no net>] of POLY on F.Cu``, ``Segment on Edge.Cuts``,
    ``Track [GND] on F.Cu, length 1.0000 mm``, ``Via [GND] on F.Cu - B.Cu``,
    ``Zone 'GND' [GND] on F.Cu and B.Cu, priority 0``.

A description in none of these shapes still yields ``kind`` (its first word)
and, when it ends ``of <reference>``, the reference.

It also runs a check on a copy of the project with its silenced rules turned
back on (:func:`unsuppressed_project`), and says which rule or exclusion hid
each finding in the real project (:class:`ProjectSuppressions`).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ENVIRONMENT = "environment"
"""``origin`` of a finding caused by where the design is checked, not by the
design: KiCad's library configuration lacks a library the design names."""

DESIGN = "design"
"""``origin`` of every other finding."""

_ENVIRONMENT_TEXT = re.compile(
    r"current configuration does not include the (?:symbol|footprint) library", re.IGNORECASE
)

# ERC: "Symbol U5 Pin 1 [SDO/SA0, Bidirectional, Line]", "... Hidden pin 1 [...]"
_SYMBOL_PIN = re.compile(
    r"^Symbol (?P<ref>\S+) (?P<hidden>Hidden pin|Pin) (?P<pin>\S+) \[(?P<attrs>.*)\]$"
)
# ERC: "Symbol C107 [C]", "Symbol C107"
_SYMBOL = re.compile(r"^Symbol (?P<ref>\S+)(?: \[(?P<value>.*)\])?$")
# DRC: "Footprint J1"
_FOOTPRINT = re.compile(r"^Footprint (?P<ref>\S+)$")
# DRC: "Pad 2 [/NET] of J1 on F.Cu", "PTH pad 1 [+BATT] of J2", "NPTH pad of H4"
_PAD = re.compile(
    r"^(?:(?P<plating>N?PTH) pad|Pad)(?: (?P<pin>[^\s\[]+))?(?: \[(?P<net>.*?)\])?"
    r" of (?P<ref>\S+)(?: on (?P<layer>.+))?$"
)
# DRC: "Reference field of J9", "Value field of POLY (VAL**)"
_FIELD = re.compile(r"^(?P<field>.+?) field of (?P<ref>\S+)(?: \((?P<text>.*)\))?$")
# DRC: "Track [GND] on F.Cu, length 1.0000 mm"
_TRACK = re.compile(r"^Track \[(?P<net>.*?)\] on (?P<layer>[^,]+)(?:, length .*)?$")
# DRC: "Via [GND] on F.Cu - B.Cu"
_VIA = re.compile(r"^Via \[(?P<net>.*?)\] on (?P<layer>.+)$")
# DRC: "Zone 'GND' [GND] on F.Cu and B.Cu, priority 0", "Zone [GND] on bottom_copper, ..."
_ZONE = re.compile(
    r"^Zone(?: '(?P<name>.*?)')? \[(?P<net>.*?)\] on (?P<layer>.+?)(?:, priority .*)?$"
)
# DRC: "Segment of J1 on F.Silkscreen", "Polygon [<no net>] of POLY on F.Cu"
_FOOTPRINT_GRAPHIC = re.compile(
    r"^(?P<shape>[A-Z][a-z]+)(?: \[(?P<net>.*?)\])? of (?P<ref>\S+) on (?P<layer>.+)$"
)
# DRC: "Segment on Edge.Cuts"
_BOARD_GRAPHIC = re.compile(r"^(?P<shape>[A-Z][a-z]+) on (?P<layer>\S+)$")
_ANY_OWNER = re.compile(r" of (?P<ref>[^\s\[\]()]+)(?: on .*| \(.*\))?$")
_BRACKETS = re.compile(r"\[[^\]]*\]")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def parse_item(description: str) -> dict[str, Any]:
    """The facts one report item's description states.

    Always present: ``kind`` (``pin``, ``symbol``, ``pad``, ``footprint``,
    ``field``, ``graphic``, ``track``, ``via``, ``zone``, or the first word of
    an unknown shape, lower-cased), ``reference`` (the symbol or footprint the
    item belongs to), ``pin`` (pin or pad number), ``pin_name`` and
    ``pin_type``, each None when the description does not say. ``pin_type`` is
    KiCad's own text: ``Power output``, ``Bidirectional``, ``Passive``...

    Present when stated: ``pin_shape``, ``hidden`` (a hidden pin), ``value``,
    ``plating`` (``PTH``/``NPTH``), ``net``, ``layer``, ``field``, ``zone``,
    ``shape``.
    """
    text = description.strip()
    item: dict[str, Any] = {
        "kind": None,
        "reference": None,
        "pin": None,
        "pin_name": None,
        "pin_type": None,
    }
    if m := _SYMBOL_PIN.match(text):
        attrs = [a.strip() for a in m["attrs"].split(",")]
        item.update(kind="pin", reference=m["ref"], pin=m["pin"])
        if len(attrs) >= 2:
            item["pin_shape"] = attrs[-1]
            item["pin_type"] = attrs[-2]
            if len(attrs) > 2:
                item["pin_name"] = ", ".join(attrs[:-2])
        elif attrs and attrs[0]:
            item["pin_type"] = attrs[0]
        if m["hidden"] != "Pin":
            item["hidden"] = True
    elif m := _SYMBOL.match(text):
        item.update(kind="symbol", reference=m["ref"])
        if m["value"] is not None:
            item["value"] = m["value"]
    elif m := _FOOTPRINT.match(text):
        item.update(kind="footprint", reference=m["ref"])
    elif m := _PAD.match(text):
        item.update(kind="pad", reference=m["ref"], pin=m["pin"])
        _optional(item, plating=m["plating"], net=m["net"], layer=m["layer"])
    elif m := _FIELD.match(text):
        item.update(kind="field", reference=m["ref"], field=m["field"])
        _optional(item, value=m["text"])
    elif m := _TRACK.match(text):
        item.update(kind="track", net=m["net"], layer=m["layer"])
    elif m := _VIA.match(text):
        item.update(kind="via", net=m["net"], layer=m["layer"])
    elif m := _ZONE.match(text):
        item.update(kind="zone", net=m["net"], layer=m["layer"])
        _optional(item, zone=m["name"])
    elif m := _FOOTPRINT_GRAPHIC.match(text):
        item.update(kind="graphic", reference=m["ref"], shape=m["shape"], layer=m["layer"])
        _optional(item, net=m["net"])
    elif m := _BOARD_GRAPHIC.match(text):
        item.update(kind="graphic", shape=m["shape"], layer=m["layer"])
    else:
        words = text.split()
        item["kind"] = words[0].lower() if words else None
        if m := _ANY_OWNER.search(_BRACKETS.sub("", text)):
            item["reference"] = m["ref"]
    return item


def _optional(item: dict[str, Any], **values: str | None) -> None:
    item.update({k: v for k, v in values.items() if v is not None})


def finding_origin(violation: Mapping[str, Any]) -> str:
    """:data:`ENVIRONMENT` or :data:`DESIGN`.

    Environment findings are KiCad saying its library configuration lacks a
    library the design names (``lib_symbol_issues``, ``footprint_link_issues``,
    ``lib_footprint_issues`` reading "The current configuration does not
    include the ... library 'X'"). They appear when a design is checked without
    its project's ``sym-lib-table``/``fp-lib-table``, and say nothing about the
    design. A library that is configured but lacks the named part is a design
    finding.
    """
    if _ENVIRONMENT_TEXT.search(str(violation.get("description") or "")):
        return ENVIRONMENT
    return DESIGN


def structured_items(
    violation: Mapping[str, Any], lib_ids: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """The finding's items, each with the fields of :func:`parse_item` added.

    The report's own keys (``description``, ``pos``, ``uuid``) are kept. With
    ``lib_ids`` (reference to library id, from the schematic), items naming a
    symbol also carry its ``lib_id``, or None when the reference is not in it.
    """
    items = []
    for raw in violation.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        item = {**raw, **parse_item(str(raw.get("description", "")))}
        if lib_ids is not None and item["kind"] in ("pin", "symbol"):
            item["lib_id"] = lib_ids.get(item["reference"])
        items.append(item)
    return items


def finding_references(violation: Mapping[str, Any]) -> list[str | None]:
    """The reference each item of a finding belongs to, None for an item of none.

    Board outline, tracks, vias and zones belong to no footprint. A finding
    with no items (``missing_footprint``: "Missing footprint R13 (10K)") names
    its part only in its description, which is read instead.
    """
    items = violation.get("items") or []
    if not items:
        m = re.search(r"\b[Ff]ootprint (?P<ref>[^\s()]+)", str(violation.get("description", "")))
        return [m["ref"]] if m else []
    refs: list[str | None] = []
    for raw in items:
        if isinstance(raw, Mapping):
            ref = raw.get("reference")
            if ref is None and "kind" not in raw:
                ref = parse_item(str(raw.get("description", "")))["reference"]
            refs.append(ref)
    return refs


NEVER_OWNED = frozenset({"unconnected_items"})
"""Finding types that no part owns, whatever their items name.

An ``unconnected_items`` finding is a missing link in a net, not a defect of
the copper at its ends, and KiCad picks those ends afresh on every run: the same
unrouted VBUS link was reported once as "J1 pad A4 to a track" and once as "J1
pad A4 to J1 pad A9". Read as ownership, the second form would let
``exclude_owned_by=["J1"]`` hide an unrouted net."""


def is_owned_by(violation: Mapping[str, Any], references: Collection[str]) -> bool:
    """Whether every item of the finding belongs to one of ``references``.

    A pad too close to its own footprint's hole is owned by that footprint. A
    finding that pairs it with the board edge, a track or another part is not:
    one of its items belongs elsewhere. A finding with no reference at all is
    never owned, and neither is one of the :data:`NEVER_OWNED` types.
    """
    if violation.get("type") in NEVER_OWNED:
        return False
    refs = finding_references(violation)
    return bool(refs) and all(ref is not None and ref in references for ref in refs)


def findings_owned_by(
    violations: Iterable[Mapping[str, Any]], references: Collection[str]
) -> list[Mapping[str, Any]]:
    """The findings whose items all belong to ``references`` (see :func:`is_owned_by`)."""
    wanted = set(references)
    return [v for v in violations if is_owned_by(v, wanted)]


def project_file_for(design: str | Path) -> Path | None:
    """The ``.kicad_pro`` beside a schematic or board, of the same name."""
    candidate = Path(design).with_suffix(".kicad_pro")
    return candidate if candidate.is_file() else None


Tool = Literal["erc", "drc"]


@dataclass(frozen=True)
class ProjectSuppressions:
    """What a project file silences: rule severities and excluded findings.

    ``severities`` maps a rule key (a finding ``type``) to the severity the
    project gives it; ``exclusions`` are KiCad's exclusion keys,
    ``type|x|y|uuid|uuid``, as the project stores them.
    """

    severities: Mapping[str, str] = field(default_factory=dict)
    exclusions: tuple[str, ...] = ()

    @classmethod
    def read(cls, project: str | Path | None, tool: Tool) -> ProjectSuppressions:
        """The ERC or DRC settings of a ``.kicad_pro``; empty when there is none."""
        if project is None:
            return cls()
        try:
            data = json.loads(Path(project).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        section = _settings_section(data, tool)
        severities = section.get("rule_severities")
        exclusions = section.get(_EXCLUSIONS_KEY[tool])
        return cls(
            severities={str(k): str(v) for k, v in (severities or {}).items() if isinstance(v, str)}
            if isinstance(severities, dict)
            else {},
            exclusions=tuple(key for key in (_exclusion_key(e) for e in exclusions or []) if key)
            if isinstance(exclusions, list)
            else (),
        )

    def exclusion_for(self, violation: Mapping[str, Any]) -> str | None:
        """The project's exclusion that names this finding, if one does.

        An exclusion key starts with the finding type and lists the uuids of the
        items involved. Positions are not compared: kicad-cli does not report
        the marker position the key was saved with.
        """
        kind = violation.get("type")
        uuids = {
            str(i.get("uuid"))
            for i in violation.get("items") or []
            if isinstance(i, Mapping) and i.get("uuid")
        }
        for exclusion in self.exclusions:
            parts = exclusion.split("|")
            wanted = {p for p in parts[1:] if _UUID.match(p) and p != _NIL_UUID}
            if parts[0] == kind and wanted and wanted <= uuids:
                return exclusion
        return None

    def hidden_by(self, violation: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """What hid the finding in the real project, as ``(hidden_by, hidden_kind)``.

        ``hidden_kind`` is ``"exclusion"`` (``hidden_by`` is the exclusion key)
        or ``"rule"`` (``hidden_by`` is the rule key, which the project sets to
        ``ignore``); both are None when the project did not hide it. A rule the
        project file does not list is taken as not hidden: KiCad writes every
        rule key when it saves a project.
        """
        exclusion = self.exclusion_for(violation)
        if exclusion is not None:
            return exclusion, "exclusion"
        kind = str(violation.get("type"))
        if self.severities.get(kind) == "ignore":
            return kind, "rule"
        return None, None

    def tag(self, violations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Each finding with ``hidden_by``, ``hidden_kind`` and ``project_severity``.

        ``project_severity`` is the severity the project file gives the rule,
        None when it does not list it.
        """
        tagged = []
        for violation in violations:
            hidden_by, hidden_kind = self.hidden_by(violation)
            tagged.append(
                {
                    **violation,
                    "hidden_by": hidden_by,
                    "hidden_kind": hidden_kind,
                    "project_severity": self.severities.get(str(violation.get("type"))),
                }
            )
        return tagged


SEVERITIES = ("error", "warning", "ignore")
"""The severities a project file can give a rule."""


def validate_severity_overrides(overrides: Mapping[str, str] | None) -> None:
    """Raise ValueError unless every override is one of :data:`SEVERITIES`."""
    for key, value in (overrides or {}).items():
        if value not in SEVERITIES:
            raise ValueError(f"severity_overrides[{key!r}] must be one of {', '.join(SEVERITIES)}")


_EXCLUSIONS_KEY: dict[str, str] = {"erc": "erc_exclusions", "drc": "drc_exclusions"}


def _settings_section(data: Any, tool: Tool) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    if tool == "erc":
        section = data.get("erc")
    else:
        board = data.get("board")
        section = board.get("design_settings") if isinstance(board, dict) else None
    return section if isinstance(section, dict) else {}


def _exclusion_key(entry: Any) -> str | None:
    # KiCad stores a bare key, or a [key, comment] pair when a comment was given.
    if isinstance(entry, str):
        return entry
    if isinstance(entry, list) and entry and isinstance(entry[0], str):
        return entry[0]
    return None


_LIB_TABLES = ("sym-lib-table", "fp-lib-table")


@contextmanager
def unsuppressed_project(
    design: str | Path,
    tool: Tool,
    *,
    reset_severities: bool = False,
    drop_exclusions: bool = False,
    severity_overrides: Mapping[str, str] | None = None,
) -> Iterator[tuple[Path, ProjectSuppressions]]:
    """A temporary copy of the project with its silenced findings let through.

    Yields the copy of ``design`` (the schematic or board to check) and the
    real project's :class:`ProjectSuppressions`, to tag each finding of the
    copy with what hid it. The original project is never written.

    - ``reset_severities`` removes the project's ``rule_severities`` for
      ``tool``, so every rule has KiCad's default severity. Rules KiCad itself
      ignores by default stay ignored.
    - ``severity_overrides`` then sets rule keys to the given severities
      (``error``, ``warning``, ``ignore``), e.g. to turn on a rule that is
      ignored by default.
    - ``drop_exclusions`` removes the project's exclusions for ``tool``.

    The project's KiCad files (``*.kicad_*``) are copied. Other entries,
    library folders and 3D models among them, are linked, or copied where
    links cannot be made. ``sym-lib-table`` and ``fp-lib-table`` are copied
    with ``${KIPRJMOD}`` replaced by the original folder, so project-local
    libraries resolve however they are reached.
    """
    source = Path(design).resolve()
    project = project_file_for(source)
    suppressions = ProjectSuppressions.read(project, tool)
    root = Path(tempfile.mkdtemp(prefix=f"kicad-evaltor-{tool}-project-"))
    try:
        _copy_project(source.parent, root)
        if project is not None:
            target = root / project.name
            data = json.loads(project.read_text(encoding="utf-8"))
            section = _ensure_section(data, tool)
            if reset_severities:
                section.pop("rule_severities", None)
            if severity_overrides:
                rules = section.setdefault("rule_severities", {})
                rules.update({str(k): str(v) for k, v in severity_overrides.items()})
            if drop_exclusions:
                section.pop(_EXCLUSIONS_KEY[tool], None)
            target.unlink(missing_ok=True)
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        yield root / source.name, suppressions
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _ensure_section(data: dict[str, Any], tool: Tool) -> dict[str, Any]:
    path = ("erc",) if tool == "erc" else ("board", "design_settings")
    node = data
    for key in path:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    return node


def _copy_project(source: Path, target: Path) -> None:
    for entry in source.iterdir():
        dest = target / entry.name
        if entry.is_file() and (entry.suffix.startswith(".kicad_") or entry.name in _LIB_TABLES):
            if entry.name in _LIB_TABLES:
                text = entry.read_text(encoding="utf-8", errors="replace")
                folder = source.as_posix()
                text = text.replace("${KIPRJMOD}", folder).replace("$(KIPRJMOD)", folder)
                dest.write_text(text, encoding="utf-8")
            else:
                shutil.copy2(entry, dest)
            continue
        try:
            dest.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            # Windows without the privilege to link: copy instead.
            if entry.is_dir():
                shutil.copytree(entry, dest, symlinks=True)
            else:
                shutil.copy2(entry, dest)
