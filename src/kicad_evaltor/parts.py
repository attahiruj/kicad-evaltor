"""Finding a part, its pins and its facts, whatever library it came from.

The same chip reaches a schematic many ways: placed from KiCad's stock
library (``Battery_Management:BQ24074RGT``), copied into a project library
(``Logger:BQ24074RGT``), or drawn from scratch with only the Value saying what
it is (``BQ24074RGTR``). Its pins may be spelt ``~{CHG}``, ``/CHG`` or
``CHG_N``, and the facts a test wants (an LED's colour, a connector's
series) may sit in the Value, the Description or any custom field.

The helpers here look in all of those places and say where they found
what they return, so a caller's test states the policy and this module only
reports facts:

- :func:`text_of` and :func:`fields_matching` search every text a component
  carries;
- :func:`led_colors` reads colour words and wavelengths;
- :func:`pins_named` finds pins by datasheet name across spellings, falling
  back to the stock KiCad symbol's pin numbers;
- :func:`signal_pins` leaves out mounting, shield and exposed pads;
- :func:`find_part` finds exactly one part by name stem, Value or marker text,
  or says why every candidate was turned down.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from kicad_evaltor.netlist import FileNetComponent, FileNetlist, FileNetNode
from kicad_evaltor.pin_roles import match_pin_name
from kicad_evaltor.symbol_library import SymbolLibraries, SymbolNotFound, symbol_signature

# ------------------------------------------------------------------ text


_SKIPPED_PROPERTIES = frozenset(
    {"Sheetname", "Sheetfile", "dnp", "exclude_from_bom", "exclude_from_board"}
)


def text_of(component: FileNetComponent) -> dict[str, str]:
    """Every text a component carries, by where it came from.

    Keys are ``lib_id``, ``footprint``, ``Value`` and ``Description``, then
    each field by its own name (``LCSC``, ``MPN``, ``Datasheet``...), then any
    property the fields do not already hold (``ki_keywords``). Empty texts
    and the sheet and flag properties are left out. The reference is not
    included: a part is what it is whatever letter it was given.
    """
    texts: dict[str, str] = {}

    def add(name: str, text: str) -> None:
        if text and name not in texts:
            texts[name] = text

    add("lib_id", component.lib_id)
    add("footprint", component.footprint or component.fields.get("Footprint", ""))
    add("Value", component.value)
    add("Description", component.description or component.fields.get("Description", ""))
    for name, text in component.fields.items():
        if name not in ("Footprint", "Description", "Value"):
            add(name, text)
    for name, text in component.properties.items():
        if name not in _SKIPPED_PROPERTIES and name not in ("Footprint", "Description", "Value"):
            add(name, text)
    return texts


@dataclass(frozen=True)
class FieldMatch:
    """One field of a component that matched: its name, its text, what matched."""

    field: str
    text: str
    match: str


def fields_matching(
    component: FileNetComponent, pattern: str | re.Pattern[str], *, ignore_case: bool = True
) -> tuple[FieldMatch, ...]:
    """The fields of :func:`text_of` in which ``pattern`` (a regex) is found.

    In :func:`text_of` order, one entry per matching field with the first
    match in it. Pass a compiled pattern to control its flags yourself;
    a string is compiled case-insensitively unless ``ignore_case`` is False.
    """
    compiled = (
        pattern
        if isinstance(pattern, re.Pattern)
        else re.compile(pattern, re.I if ignore_case else 0)
    )
    found = []
    for name, text in text_of(component).items():
        match = compiled.search(text)
        if match:
            found.append(FieldMatch(name, text, match.group(0)))
    return tuple(found)


# ------------------------------------------------------------- LED colour


@dataclass(frozen=True)
class LedColor:
    """A colour read from a component, the field it was in and the words read."""

    color: str
    field: str
    evidence: str


_COLOR_WORDS: dict[str, tuple[str, ...]] = {
    "red": ("red",),
    "green": ("green",),
    "blue": ("blue",),
    "yellow": ("yellow",),
    "orange": ("orange",),
    "amber": ("amber",),
    "white": ("white",),
    "pink": ("pink",),
    "purple": ("purple",),
    "violet": ("violet",),
    "cyan": ("cyan",),
    "infrared": ("infrared",),
    "ir": ("infrared",),
    "ultraviolet": ("ultraviolet",),
    "uv": ("ultraviolet",),
    "rgb": ("red", "green", "blue"),
}
_COLOR_WORD = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(_COLOR_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.I,
)
_WAVELENGTH = re.compile(r"(?<![\d.])(\d{3,4}(?:\.\d+)?)\s*nm(?![A-Za-z])", re.I)

WAVELENGTH_BANDS: tuple[tuple[float, str], ...] = (
    (400, "ultraviolet"),
    (450, "violet"),
    (500, "blue"),
    (570, "green"),
    (590, "yellow"),
    (620, "orange"),
    (750, "red"),
)
"""Upper bounds (exclusive, in nm) of the colour each wavelength reads as.

Below 400 nm ultraviolet, 400-449 violet, 450-499 blue (470 nm), 500-569
green (520-525 nm), 570-589 yellow, 590-619 orange (amber parts sit near
590-605 nm), 620-749 red; 750 nm and up is infrared (850, 940 nm). These are
the usual dominant-wavelength bands of LED datasheets; a caller with other
bands passes its own through ``extra``."""


def wavelength_color(nanometres: float) -> str:
    """The colour name :data:`WAVELENGTH_BANDS` gives a wavelength."""
    for limit, color in WAVELENGTH_BANDS:
        if nanometres < limit:
            return color
    return "infrared"


def led_colors(
    component: FileNetComponent, *, extra: Mapping[str, str] | None = None
) -> tuple[LedColor, ...]:
    """Every colour a component's texts name, with where each was read.

    Reads colour words (``red``, ``Green``, ``WHITE``, ``RGB`` as all three,
    ``IR``/``UV``) as whole words, and wavelengths (``470nm`` is blue, by
    :data:`WAVELENGTH_BANDS`), in every text of :func:`text_of`. ``extra``
    maps a caller's own regex to a colour, matched case-insensitively against
    the same texts, for codes only a vendor's datasheet explains
    (``{"KT-0603R": "red"}``); no such codes are built in.

    One entry per colour per field, in field order; an LED whose Value says
    RED and Description says green returns both, and it is the caller's call
    what that means.
    """
    patterns = [(re.compile(p, re.I), color) for p, color in (extra or {}).items()]
    found: list[LedColor] = []
    seen: set[tuple[str, str]] = set()

    def add(color: str, name: str, evidence: str) -> None:
        if (color, name) not in seen:
            seen.add((color, name))
            found.append(LedColor(color, name, evidence))

    for name, text in text_of(component).items():
        for match in _COLOR_WORD.finditer(text):
            for color in _COLOR_WORDS[match.group(1).lower()]:
                add(color, name, match.group(0))
        for match in _WAVELENGTH.finditer(text):
            add(wavelength_color(float(match.group(1))), name, match.group(0))
        for pattern, color in patterns:
            hit = pattern.search(text)
            if hit:
                add(color, name, hit.group(0))
    return tuple(found)


# ------------------------------------------------------------------ pins


@dataclass(frozen=True)
class PinMatch:
    """A pin found by name, and how.

    ``how`` is ``"exact"``, ``"normalised"`` or ``"alternate"`` (see
    :func:`~kicad_evaltor.pin_roles.match_pin_name`) when the component's own
    pin name matched, or ``"stock_symbol"`` when the pin was found by its
    number on the stock KiCad symbol ``source``. ``spelling`` is the pin name
    that matched: the component's, or the stock symbol's.
    """

    node: FileNetNode
    how: str
    spelling: str
    source: str = ""

    @property
    def pin(self) -> str:
        return self.node.pin

    @property
    def net(self) -> str:
        return self.node.net


@functools.cache
def _stock_libraries() -> SymbolLibraries:
    return SymbolLibraries()


def stock_symbols_for(
    component: FileNetComponent, libraries: SymbolLibraries | None = None
) -> tuple[str, ...]:
    """The stock symbols a component is most likely a copy of, best first.

    Tried in turn, stopping at the first that finds any: the component's own
    ``lib_id`` when ``libraries`` has it; library symbols with the same name
    as the ``lib_id``'s (a copy in a project library); then symbols whose
    name is a stem of the ``lib_id`` name or the Value (``BQ24074RGT`` for a
    Value of ``BQ24074RGTR``), or that start with one (``TPS63020DSJ`` for
    ``TPS63020``), of at least four characters and closest in length. Empty
    when there are no stock libraries or nothing fits.
    """
    libs = libraries if libraries is not None else _stock_libraries()
    if component.lib_id and component.lib_id in libs:
        return (component.lib_id,)
    name = component.lib_id.rpartition(":")[2]
    if name and (same := libs.lib_ids_named(name)):
        return same
    keys = [k.upper() for k in (name, component.value) if len(k) >= 4]
    best: list[tuple[int, str]] = []
    for symbol, ids in libs.symbol_index().items():
        upper = symbol.upper()
        if len(upper) < 4:
            continue
        for key in keys:
            if key.startswith(upper) or upper.startswith(key):
                best.extend((abs(len(upper) - len(key)), lib_id) for lib_id in ids)
                break
    if not best:
        return ()
    closest = min(distance for distance, _ in best)
    return tuple(sorted(lib_id for distance, lib_id in best if distance == closest))


def pins_named(
    netlist: FileNetlist,
    ref: str,
    name: str,
    *,
    libraries: SymbolLibraries | None = None,
    stock: bool = True,
) -> tuple[PinMatch, ...]:
    """The pins of ``ref`` that are the datasheet's ``name``, however spelt.

    The component's own pin names are tried in tiers, and the first tier with
    any match is returned: the exact name, then the same name after
    :func:`~kicad_evaltor.pin_roles.normalise_pin_name` (``~{CHG}``, ``/CHG``,
    ``nCHG``, ``CHG_N``), then one of a pin's ``/``-separated functions
    (``U0TXD`` in ``GPIO16/U0TXD``). Several pins may come back: a part with
    three GND pins has three GND matches.

    When none match and ``stock`` is on, the stock KiCad symbol of the part
    (:func:`stock_symbols_for`) is read for a pin of that name, and the
    component's pins with the same numbers are returned, as ``"stock_symbol"``
    matches. That is how a copied symbol whose pins were renamed
    (``DAT3_/_CS``) is still read by datasheet name. When several stock
    symbols fit and disagree on the numbers, nothing is returned rather than
    a guess. ``libraries`` defaults to the stock libraries KiCad installs.
    """
    pins = netlist.pins_of(ref)
    tiers: dict[str, list[PinMatch]] = {"exact": [], "normalised": [], "alternate": []}
    for pin in pins:
        how = match_pin_name(pin.pinfunction, name)
        if how is not None:
            tiers[how].append(PinMatch(pin, how, pin.pinfunction))
    for found in tiers.values():
        if found:
            return tuple(found)
    component = netlist.components.get(ref)
    if not stock or not pins or component is None:
        return ()
    libs = libraries if libraries is not None else _stock_libraries()
    numbers: dict[str, str] = {}
    source = ""
    for lib_id in stock_symbols_for(component, libs):
        try:
            signature = symbol_signature(libs.resolve(lib_id), include_graphics=False)
        except (SymbolNotFound, ValueError):
            continue
        these = {p.number: p.name for p in signature.pins if match_pin_name(p.name, name)}
        if not these:
            continue
        if numbers and set(these) != set(numbers):
            return ()
        if not numbers:
            numbers, source = these, lib_id
    return tuple(
        PinMatch(pin, "stock_symbol", numbers[pin.pin], source)
        for pin in pins
        if pin.pin in numbers
    )


@dataclass(frozen=True)
class SignalPins:
    """A part's pins, split into signal pins and the pads left out (with why)."""

    pins: tuple[FileNetNode, ...]
    dropped: tuple[tuple[FileNetNode, str], ...] = ()

    @property
    def nets(self) -> tuple[str, ...]:
        """The nets the signal pins join, each once, in pin order."""
        return tuple(dict.fromkeys(p.net for p in self.pins))


_MECHANICAL_NUMBER = re.compile(r"^(?:MP|SH|EP|MH)\d*$", re.I)
_MECHANICAL_NAME = re.compile(
    r"^(?:SHIELD|SHELL|SH|MOUNT(?:ING)?(?:PIN|PAD|HOLE)?|MP|MH|EP|EPAD|EXPOSEDPAD|THERMALPAD)\d*$"
)


def signal_pins(netlist: FileNetlist, ref: str) -> SignalPins:
    """The pins of ``ref`` that carry signals, without mechanical pads.

    A pin is left out when its number is ``MP``, ``SH``, ``EP`` or ``MH``
    (optionally numbered: ``MP1``), or its name, read without case, spaces,
    ``_`` or ``-``, is a shield (``SHIELD``, ``SHELL``, ``SH``), a mounting
    pin (``MountPin``, ``MOUNTING_PAD``, ``MP``, ``MH``) or an exposed pad
    (``EP``, ``EPAD``, ``Exposed_Pad``, ``Thermal_Pad``). Those are the pads
    stock connector and package symbols add (``Conn_01x02_MountingPin``'s
    ``MP``, a USB-C ``SH``, a QFN's ``EPAD``). A thermal pad the symbol names
    after its net (``VSS``, ``GND``) is a pin of that net and stays. Each
    dropped pin comes with the rule that dropped it.
    """
    kept: list[FileNetNode] = []
    dropped: list[tuple[FileNetNode, str]] = []
    for pin in netlist.pins_of(ref):
        squashed = re.sub(r"[\s_\-]", "", re.sub(r"~\{([^}]*)\}", r"\1", pin.pinfunction)).upper()
        if _MECHANICAL_NUMBER.match(pin.pin):
            dropped.append((pin, f"pad number {pin.pin!r} is a mounting, shield or exposed pad"))
        elif squashed and _MECHANICAL_NAME.match(squashed):
            dropped.append(
                (pin, f"pin name {pin.pinfunction!r} is a mounting, shield or exposed pad")
            )
        else:
            kept.append(pin)
    return SignalPins(tuple(kept), tuple(dropped))


# ----------------------------------------------------------- part lookup


@dataclass(frozen=True)
class PartCandidate:
    """A component :func:`find_part` considered: why it matched, why it was refused."""

    ref: str
    matched_by: tuple[str, ...]
    rejected: str = ""


@dataclass(frozen=True)
class FoundPart:
    """The one part :func:`find_part` found, how, and its required pins."""

    component: FileNetComponent
    matched_by: tuple[str, ...]
    pins: dict[str, tuple[PinMatch, ...]] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.component.ref


class PartNotFound(LookupError):
    """No component, or more than one, passed :func:`find_part`.

    ``candidates`` lists every component that matched the stem or a marker,
    with the reason each was refused (empty for the ones that passed).
    """

    def __init__(self, stem: str, candidates: tuple[PartCandidate, ...], message: str) -> None:
        self.stem = stem
        self.candidates = candidates
        super().__init__(message)


def find_part(
    netlist: FileNetlist,
    stem: str,
    *,
    markers: Iterable[str] = (),
    required_pins: Iterable[str] = (),
    fitted: bool = True,
    libraries: SymbolLibraries | None = None,
    stock: bool = True,
) -> FoundPart:
    """The one component that is the part called ``stem``.

    A component is a candidate when, ignoring case, its ``lib_id`` symbol name
    starts or ends with ``stem`` (``BQ24074RGT``, ``power_PWR_FLAG``), its
    Value starts with ``stem`` (``USBLC6-2P6`` for ``USBLC6-2``), or any of
    ``markers`` appears in its Value, ``lib_id`` or footprint (``DM3AT``,
    ``TYPE-C``). The reference letter plays no part: a USB-C receptacle
    numbered ``U1`` is found like one numbered ``J1``. An empty ``stem``
    matches by markers alone.

    A candidate is refused when ``fitted`` is set and it is marked DNP, or
    when any of ``required_pins`` is missing by :func:`pins_named` (stock
    symbol fallback included unless ``stock`` is off). Exactly one candidate
    must remain; otherwise :class:`PartNotFound` is raised naming every
    candidate and why it was refused, or that none matched at all.
    """
    key = stem.upper()
    wanted = [m for m in markers if m]
    required = tuple(required_pins)
    candidates: list[PartCandidate] = []
    accepted: list[FoundPart] = []
    for component in sorted(netlist.components.values(), key=lambda c: _ref_key(c.ref)):
        matched_by = _identity(component, key, stem, wanted)
        if not matched_by:
            continue
        if fitted and component.dnp:
            candidates.append(PartCandidate(component.ref, matched_by, "marked DNP"))
            continue
        pins = {
            name: pins_named(netlist, component.ref, name, libraries=libraries, stock=stock)
            for name in required
        }
        missing = [name for name, found in pins.items() if not found]
        if missing:
            candidates.append(
                PartCandidate(component.ref, matched_by, f"no pin named {', '.join(missing)}")
            )
            continue
        candidates.append(PartCandidate(component.ref, matched_by))
        accepted.append(FoundPart(component, matched_by, pins))
    if len(accepted) == 1:
        return accepted[0]
    seen = tuple(candidates)
    label = stem or " / ".join(wanted)
    if not seen:
        raise PartNotFound(
            stem,
            seen,
            f"no component matches {label!r} by lib_id name, Value or marker "
            f"({len(netlist.components)} components checked)",
        )
    listing = "; ".join(
        f"{c.ref} ({', '.join(c.matched_by)}): {c.rejected or 'accepted'}" for c in seen
    )
    count = "no candidate" if not accepted else f"{len(accepted)} candidates"
    raise PartNotFound(stem, seen, f"{count} for {label!r} passed: {listing}")


def _identity(
    component: FileNetComponent, key: str, stem: str, markers: list[str]
) -> tuple[str, ...]:
    reasons: list[str] = []
    name = component.lib_id.rpartition(":")[2]
    if key:
        if name.upper().startswith(key):
            reasons.append(f"lib_id name {name!r} starts with {stem!r}")
        elif name.upper().endswith(key):
            reasons.append(f"lib_id name {name!r} ends with {stem!r}")
        if component.value.upper().startswith(key):
            reasons.append(f"Value {component.value!r} starts with {stem!r}")
    places = {
        "Value": component.value,
        "lib_id": component.lib_id,
        "footprint": component.footprint,
    }
    for marker in markers:
        hits = [where for where, text in places.items() if marker.upper() in text.upper()]
        if hits:
            reasons.append(f"marker {marker!r} in {', '.join(hits)}")
    return tuple(reasons)


def _ref_key(ref: str) -> tuple[str, int, str]:
    letters = ref.rstrip("0123456789")
    digits = ref[len(letters) :]
    return (letters, int(digits) if digits else -1, ref)
