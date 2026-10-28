"""A catalogue listing in one shape, whichever distributor it came from.

Each provider maps its own field names into :class:`PartRecord`. The facts the
checks compare -- what kind of part it is, the colour an LED emits, a chip
size -- are inferred once here, and each keeps the source text it was read
from, so a reason can say *why* a record was taken to be, say, a 0402 resistor.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# Imperial chip size codes and their metric names.
CHIP_SIZES: Mapping[str, str] = MappingProxyType(
    {
        "01005": "0402",
        "0201": "0603",
        "0402": "1005",
        "0603": "1608",
        "0805": "2012",
        "1206": "3216",
        "1210": "3225",
        "1812": "4532",
        "2010": "5025",
        "2512": "6432",
    }
)

# Part types read from catalogue text, most specific first: an "LED driver" is
# an IC, not an LED, and a "switching regulator" is not a switch.
TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ic", r"LED\s+DRIVERS?|SWITCHING\s+(?:CONTROLLERS?|REGULATORS?)"),
    ("led", r"\bLEDs?\b|LIGHT EMITTING DIODE"),
    ("ferrite", r"FERRITE|MAGNETIC BEAD"),
    ("resistor", r"RESISTORS?\b|RESISTANCE"),
    ("capacitor", r"CAPACITORS?\b|CAPACITANCE"),
    ("inductor", r"INDUCTORS?\b|INDUCTANCE|CHOKE"),
    ("connector", r"CONNECTOR|RECEPTACLE|SOCKET|HEADER|TERMINAL BLOCK"),
    ("switch", r"SWITCHES|\bSWITCH\b|PUSHBUTTON"),
    ("mosfet", r"MOSFET|FIELD EFFECT TRANSISTOR"),
    ("transistor", r"TRANSISTOR"),
    ("diode", r"DIODE|RECTIFIER"),
    (
        "ic",
        r"\bICs?\b|INTEGRATED CIRCUIT|MICROCONTROLLER|REGULATOR|POWER MANAGEMENT|"
        r"BATTERY MANAGEMENT|DC.DC CONVERTER|DRIVER|AMPLIFIER|TRANSCEIVER|"
        r"BATTERY.*CHARG|CHARG.*BATTERY",
    ),
    ("module", r"MODULE"),
    ("sensor", r"SENSOR|ACCELEROMETER|GYROSCOPE|IMU\b"),
    ("crystal", r"CRYSTAL|RESONATOR|OSCILLATOR"),
)

# Attribute names that state the colour an LED emits. Lens colour ("Lens
# Color": "Water Clear") is deliberately not among them.
EMITTED_COLOUR_ATTRIBUTE = re.compile(
    r"^(?:illumination|emitted|emission|emitting|led)\s+colou?r$|^colou?r$", re.IGNORECASE
)

COLOURS: tuple[str, ...] = (
    "red",
    "green",
    "blue",
    "yellow",
    "orange",
    "amber",
    "white",
    "purple",
    "pink",
    "infrared",
    "ultraviolet",
)

# Types whose package metadata may carry a chip size code.
_CHIP_TYPES = frozenset({"resistor", "capacitor", "inductor", "ferrite", "led"})
_NOT_CHIP_PACKAGE = re.compile(r"SOT|SOP|QFN|DFN|BGA|TO-|DIP|PLUGIN|THROUGH", re.IGNORECASE)
_CHIP_SIZE = re.compile(r"(?<!\d)(" + "|".join(CHIP_SIZES) + r")(?!\d)")


@dataclass(frozen=True)
class Inferred:
    """One fact read from a record, with the record text it came from."""

    value: Any
    evidence: Mapping[str, str]


@dataclass(frozen=True)
class PartRecord:
    """A distributor listing, normalised across platforms.

    ``number`` is the distributor's own part number as the catalogue spells
    it, ``platform`` the provider that returned it (``"lcsc"``, ``"digikey"``,
    ``"mouser"``). ``inferred`` holds ``"type"``, ``"led_colours"`` and
    ``"chip_size"`` when the record supports them; a fact is absent, never
    guessed, when it does not.
    """

    number: str
    platform: str
    mpn: str
    manufacturer: str
    category: str
    package: str
    attributes: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    title: str = ""
    description: str = ""
    inferred: Mapping[str, Inferred] = field(default_factory=dict)

    @property
    def part_type(self) -> str | None:
        fact = self.inferred.get("type")
        return fact.value if fact else None

    @property
    def led_colours(self) -> tuple[str, ...]:
        fact = self.inferred.get("led_colours")
        return tuple(fact.value) if fact else ()

    @property
    def chip_size(self) -> str | None:
        """The imperial chip size code (``"0402"``) from package metadata."""
        fact = self.inferred.get("chip_size")
        return fact.value if fact else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "platform": self.platform,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "category": self.category,
            "package": self.package,
            "attributes": dict(self.attributes),
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "inferred": {
                name: {"value": fact.value, "evidence": dict(fact.evidence)}
                for name, fact in self.inferred.items()
            },
        }


def clean_text(text: Any) -> str:
    """Catalogue text with markup removed and entities decoded."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())


def colours_in(text: str) -> set[str]:
    """Colour words named in ``text``; "RGB" names all three primaries."""
    upper = re.sub(r"YELLOW[\s/-]*GREEN", "GREEN", text.upper())
    if re.search(r"\bRGB\b", upper):
        return {"red", "green", "blue"}
    return {colour for colour in COLOURS if re.search(rf"\b{colour}\b", upper, re.IGNORECASE)}


def make_record(
    *,
    number: str,
    platform: str,
    mpn: Any,
    manufacturer: Any,
    category: Any,
    package: Any,
    attributes: Mapping[str, Any],
    url: Any = None,
    title: Any = "",
    description: Any = "",
) -> PartRecord:
    """Build a record from provider fields and infer its facts with evidence."""
    attrs = {clean_text(name): clean_text(value) for name, value in attributes.items()}
    record = PartRecord(
        number=number,
        platform=platform,
        mpn=clean_text(mpn),
        manufacturer=clean_text(manufacturer),
        category=clean_text(category),
        package=clean_text(package),
        attributes=attrs,
        url=str(url) if url else None,
        title=clean_text(title),
        description=clean_text(description),
    )
    return _with_inferred(record)


def _with_inferred(record: PartRecord) -> PartRecord:
    inferred: dict[str, Inferred] = {}
    for name in ("category", "title", "description"):
        text = getattr(record, name)
        kind = next(
            (kind for kind, pattern in TYPE_PATTERNS if re.search(pattern, text, re.IGNORECASE)),
            None,
        )
        if kind:
            inferred["type"] = Inferred(kind, {name: text})
            break
    part_type = inferred["type"].value if "type" in inferred else None

    if part_type == "led":
        emitted = {
            name: value
            for name, value in record.attributes.items()
            if EMITTED_COLOUR_ATTRIBUTE.search(name.strip())
        }
        found: set[str] = set()
        for value in emitted.values():
            found |= colours_in(value)
        if found:
            inferred["led_colours"] = Inferred(tuple(sorted(found)), emitted)

    # Chip size codes are read from package metadata only: an MPN, a voltage
    # rating or a wavelength can contain "0603" without meaning a size.
    package = record.package
    size = _CHIP_SIZE.search(package)
    if (
        size
        and part_type in _CHIP_TYPES
        and not _NOT_CHIP_PACKAGE.search(package)
        and not re.search(rf"{size.group(1)}\s*METRIC", package, re.IGNORECASE)
    ):
        inferred["chip_size"] = Inferred(size.group(1), {"package": package})

    return PartRecord(
        number=record.number,
        platform=record.platform,
        mpn=record.mpn,
        manufacturer=record.manufacturer,
        category=record.category,
        package=record.package,
        attributes=record.attributes,
        url=record.url,
        title=record.title,
        description=record.description,
        inferred=inferred,
    )
