"""Does a catalogue record describe the part a symbol selects?

The rules are platform-neutral: they read only :class:`PartRecord` fields.
What each part must be -- the MPN stem a named IC's listing carries, the
category word a connector's listing carries, which parts are compared by value
-- is caller data, given as :class:`PartRule` objects. The first rule that
selects a part decides what its record must show; the chip size check applies
to every part whose footprint names a chip size.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from kicad_evaltor.sourcing.record import CHIP_SIZES, PartRecord, colours_in
from kicad_evaltor.values import parse_quantity

_DEFAULT_VALUE_ATTRIBUTES = {
    "resistance": ("Resistance",),
    "capacitance": ("Capacitance",),
    "inductance": ("Inductance",),
}

# KiCad's stock chip footprints name both sizes: R_0402_1005Metric.
_FOOTPRINT_CHIP_SIZE = re.compile(r"_(\d{4,5})_(\d{4})Metric")


@dataclass(frozen=True)
class PartRule:
    """What the catalogue record of each selected part must show.

    A rule selects parts by case-sensitive glob on ``reference``, ``lib_id``
    and ``value``; all three must match. Then, for each option given:

    - ``mpn_stem``: the record's MPN contains this stem, ignoring case and
      hyphens (``"BQ24074"`` accepts ``BQ24074RGTR``).
    - ``category``: the record's category contains one of these words,
      ignoring case (``("USB",)``, ``("LED",)``).
    - ``dimension``: the part's Value and the record's value attribute are
      both read as this quantity (``"resistance"``, ``"capacitance"``,
      ``"inductance"``) and agree within ``tolerance``. ``value_attributes``
      names the record attributes to read; it defaults to the dimension's
      usual name (``Resistance``...). A record without a readable value
      attribute is a mismatch, not a pass.
    - ``colour``: when the part names a colour (in Value or ``colour_fields``),
      the record's emitted LED colours must equal it.
    """

    reference: str = "*"
    lib_id: str = "*"
    value: str = "*"
    mpn_stem: str | None = None
    category: tuple[str, ...] = ()
    dimension: str | None = None
    value_attributes: tuple[str, ...] = ()
    tolerance: float = 0.01
    colour: bool = False
    colour_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.category, str):
            object.__setattr__(self, "category", (self.category,))
        if isinstance(self.value_attributes, str):
            object.__setattr__(self, "value_attributes", (self.value_attributes,))
        if isinstance(self.colour_fields, str):
            object.__setattr__(self, "colour_fields", (self.colour_fields,))
        if self.dimension is not None and self.dimension not in _DEFAULT_VALUE_ATTRIBUTES:
            if not self.value_attributes:
                raise ValueError(
                    f"dimension {self.dimension!r} needs value_attributes naming the "
                    "record attribute to compare"
                )
            parse_quantity("", dimension=self.dimension)  # rejects unknown dimensions
        if not math.isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError("tolerance must be a non-negative finite fraction")
        if self.mpn_stem is not None and not self.mpn_stem.strip():
            raise ValueError("mpn_stem must not be blank")

    def selects(self, part: PartView) -> bool:
        return (
            fnmatchcase(part.reference, self.reference)
            and fnmatchcase(part.lib_id, self.lib_id)
            and fnmatchcase(part.value, self.value)
        )

    @property
    def attributes_for_value(self) -> tuple[str, ...]:
        if self.value_attributes:
            return self.value_attributes
        return _DEFAULT_VALUE_ATTRIBUTES.get(self.dimension or "", ())


@dataclass(frozen=True)
class PartView:
    """The symbol facts the rules read, from any component-like object."""

    reference: str
    lib_id: str
    value: str
    footprint: str
    fields: Mapping[str, str]

    @classmethod
    def of(cls, component: Any) -> PartView:
        """Read a :class:`FileSymbol`, a :class:`Component`, or this view."""
        if isinstance(component, PartView):
            return component
        footprint = getattr(component, "footprint", "") or ""
        if not isinstance(footprint, str):
            footprint = getattr(getattr(footprint, "text", None), "value", "") or ""
        fields: dict[str, str] = {}
        for name in ("fields", "user_fields", "properties"):
            source = getattr(component, name, None)
            if isinstance(source, Mapping):
                fields.update({str(k): str(v) for k, v in source.items()})
        return cls(
            reference=str(getattr(component, "reference", "")),
            lib_id=str(getattr(component, "lib_id", "")),
            value=str(getattr(component, "value", "") or ""),
            footprint=footprint,
            fields=fields,
        )

    @property
    def footprint_name(self) -> str:
        return self.footprint.rsplit(":", 1)[-1]

    @property
    def chip_size(self) -> str | None:
        """The imperial chip size a stock KiCad chip footprint names."""
        match = _FOOTPRINT_CHIP_SIZE.search(self.footprint_name)
        if match and CHIP_SIZES.get(match.group(1)) == match.group(2):
            return match.group(1)
        return None


def rule_for(component: Any, rules: Iterable[PartRule]) -> PartRule | None:
    """The first rule that selects ``component``."""
    part = PartView.of(component)
    return next((rule for rule in rules if rule.selects(part)), None)


def _norm_mpn(text: str) -> str:
    return re.sub(r"[\s\-_/]", "", text).upper()


def part_colours(component: Any, rule: PartRule | None = None) -> tuple[str, ...]:
    """Colour words a part names in its Value and the rule's colour fields."""
    part = PartView.of(component)
    texts = [part.value]
    if rule is not None:
        texts += [part.fields.get(name, "") for name in rule.colour_fields]
    found: set[str] = set()
    for text in texts:
        found |= colours_in(text)
    return tuple(sorted(found))


def _quantity(text: str, dimension: str) -> float | None:
    # "\u2126" is the ohm sign some catalogues write in place of omega.
    return parse_quantity(text.replace("\u2126", "\u03a9"), dimension=dimension).value


def catalogue_mismatch(
    component: Any,
    record: PartRecord,
    rules: Sequence[PartRule] = (),
    *,
    check_chip_size: bool = True,
) -> str | None:
    """Why ``record`` cannot be the part ``component`` selects, or None.

    ``component`` is a placed symbol or :class:`Component`. The first of
    ``rules`` that selects it applies (see :class:`PartRule`). With
    ``check_chip_size``, a part on a stock chip footprint (``R_0402_1005Metric``)
    also needs a record whose package metadata names that chip size; a record
    with no chip size, such as a through-hole part, is a mismatch.
    """
    part = PartView.of(component)
    rule = rule_for(part, rules)
    if rule is not None:
        reason = _rule_mismatch(part, record, rule)
        if reason:
            return reason
    if check_chip_size:
        wanted = part.chip_size
        if wanted is not None and record.chip_size != wanted:
            listed = f"{record.chip_size} " if record.chip_size else ""
            package = record.package or "unknown package"
            return f"is a {listed}part ({package}), not {wanted} as the footprint {part.footprint_name} needs"
    return None


def _rule_mismatch(part: PartView, record: PartRecord, rule: PartRule) -> str | None:
    if rule.mpn_stem is not None and _norm_mpn(rule.mpn_stem) not in _norm_mpn(record.mpn):
        return f"is {record.mpn or 'a part with no MPN'}, not a {rule.mpn_stem}"
    if rule.category:
        category = record.category.upper()
        if not any(word.upper() in category for word in rule.category):
            words = " or ".join(rule.category)
            return f"is a {record.category or 'part with no category'}, not a {words} part"
    if rule.dimension is not None:
        wanted = _quantity(part.value, rule.dimension)
        if wanted is None:
            return f"cannot be compared: Value {part.value!r} is not a {rule.dimension}"
        names = rule.attributes_for_value
        listed_text = next(
            (record.attributes[name] for name in names if record.attributes.get(name)), None
        )
        if listed_text is None:
            return f"lists no {' or '.join(names)} to confirm {part.value}"
        listed = _quantity(listed_text, rule.dimension)
        if listed is None:
            return f"lists {rule.dimension} {listed_text!r}, which cannot be read"
        if not math.isclose(listed, wanted, rel_tol=rule.tolerance, abs_tol=1e-15):
            return f"is {listed_text}, not {part.value}"
    if rule.colour:
        wanted_colours = part_colours(part, rule)
        if wanted_colours:
            if not record.led_colours:
                return f"states no emitted colour to confirm {'/'.join(wanted_colours)}"
            if tuple(record.led_colours) != wanted_colours:
                return f"emits {'/'.join(record.led_colours)}, not {'/'.join(wanted_colours)}"
    return None


def part_identity(
    component: Any, rules: Sequence[PartRule] = (), *, include_value: bool = False
) -> tuple[Any, ...]:
    """What tells one selected part from another, for part number reuse.

    Parts compared by value (a rule with ``dimension``) differ by that value,
    LEDs (a rule with ``colour``) by colour, and every part by its reference
    letters and footprint. Other parts ignore Value, which is often a role name
    (two identical buttons ``RESET`` and ``BOOT``). ``include_value`` makes
    the raw Value part of every identity, so role names must also agree.
    """
    part = PartView.of(component)
    letters = re.match(r"[A-Za-z_#]*", part.reference)
    kind = letters.group(0) if letters else ""
    rule = rule_for(part, rules)
    detail: Any = None
    if rule is not None and rule.dimension is not None:
        quantity = _quantity(part.value, rule.dimension)
        detail = quantity if quantity is not None else part.value.strip().upper()
    elif rule is not None and rule.colour:
        detail = part_colours(part, rule)
    identity: tuple[Any, ...] = (kind, detail, part.footprint_name)
    if include_value:
        identity += (part.value.strip(),)
    return identity


def same_part(records: Sequence[PartRecord], *, mpn_stem: str | None = None) -> str | None:
    """Why records from several catalogues cannot all be one part, or None.

    With an ``mpn_stem`` every record's MPN must carry it. Without one,
    another manufacturer's equivalent is acceptable, so records only have to
    agree on the facts each states: part type, chip size and LED colours.
    """
    if len(records) < 2:
        return None
    if mpn_stem is not None:
        stem = _norm_mpn(mpn_stem)
        off = [r for r in records if stem not in _norm_mpn(r.mpn)]
        if off:
            listed = ", ".join(f"{r.platform} {r.number} is {r.mpn or 'no MPN'}" for r in off)
            return f"records disagree on the {mpn_stem} stem: {listed}"
        return None
    for fact, label in (
        ("part_type", "type"),
        ("chip_size", "chip size"),
        ("led_colours", "colour"),
    ):
        stated = {(r.platform, r.number): getattr(r, fact) for r in records if getattr(r, fact)}
        if len(set(stated.values())) > 1:
            listed = ", ".join(f"{p} {n}: {v}" for (p, n), v in stated.items())
            return f"records disagree on {label}: {listed}"
    return None
