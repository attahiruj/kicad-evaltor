"""Telling the two halves of a differential pair apart by pin name.

Symbols spell the same signal many ways. A USB bridge's D+ may be ``D+``,
``UD+``, ``USBDP``, ``USBD+``, ``USB_D+`` or ``DP``, and an MCU pin may carry it
as one of several functions (``PA12/USB_DP``). A :class:`PairRule` turns all
of those into "positive" or "negative", so a check can ask whether each half
of a pair lands on the same half at the far end and never on the other.

Rules for USB and CAN are built in, plus :func:`suffix_rule` for the generic
``X+``/``X-`` and ``X_P``/``X_N`` spellings of LVDS, MIPI, Ethernet and the
like. Others are a :class:`PairRule` away. RS-485 is deliberately not built
in, because vendors disagree on which of A and B is the inverting line.

The same spelling problem applies to any datasheet pin name: ``~{CHG}``,
``/CHG``, ``nCHG`` and ``CHG_N`` are one pin. :func:`normalise_pin_name`,
:func:`pin_name_alternatives` and :func:`match_pin_name` read those as one,
and :func:`kicad_evaltor.parts.pins_named` uses them to find pins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Polarity = Literal["P", "N"]


@dataclass(frozen=True)
class PairRule:
    """Pin-name patterns for the positive and negative half of a pair.

    Patterns are full-match regular expressions, applied case-insensitively
    to the pin name and to each ``/``-separated alternative in it, after
    KiCad's overbar markup (``~{...}``) is removed.
    """

    name: str
    positive: tuple[str, ...]
    negative: tuple[str, ...]

    def classify(self, pin_name: str) -> Polarity | None:
        """``"P"``, ``"N"``, or None when the name is neither half."""
        for candidate in _candidates(pin_name):
            if any(re.fullmatch(p, candidate, re.I) for p in self.positive):
                return "P"
            if any(re.fullmatch(p, candidate, re.I) for p in self.negative):
                return "N"
        return None


def _candidates(pin_name: str) -> list[str]:
    # Overbar markup off, and a typographic minus read as the ASCII one.
    plain = re.sub(r"~\{([^}]*)\}", r"\1", pin_name).replace("\u2212", "-").strip()
    parts = [p.strip() for p in plain.split("/") if p.strip()]
    return [plain, *parts] if len(parts) > 1 else [plain]


USB = PairRule(
    "usb",
    positive=(r"(USB_?)?U?D\+", r"(USB_?)?D_?P", r"USB_?D_?P\d?", r"D_?P\d", r"USB_?D\+\d?"),
    negative=(
        r"(USB_?)?U?D-",
        r"(USB_?)?D_?[MN]",
        r"USB_?D_?[MN]\d?",
        r"D_?[MN]\d",
        r"USB_?D-\d?",
    ),
)
"""USB 2.0 data lines: ``D+``/``D-``, ``UD+``, ``USBD+``, ``USB_D+``,
``USBDP``/``USBDM``, ``DP``/``DM``/``DN``, with an optional port number."""

CAN = PairRule(
    "can",
    positive=(r"CAN_?H\d?", r"CAN\d?_?H", r"CAN\+"),
    negative=(r"CAN_?L\d?", r"CAN\d?_?L", r"CAN-"),
)
"""CAN bus lines: ``CANH``/``CANL``, ``CAN_H``, ``CAN1H``, ``CAN+``/``CAN-``."""


def suffix_rule(name: str = "diff") -> PairRule:
    """Any pair spelled with a ``+``/``-`` or ``_P``/``_N`` suffix.

    ``TX+``/``TX-``, ``CLK_P``/``CLK_N``. A bare trailing letter (``RXP``) is
    not enough, since ``EN`` and ``VCCP`` end the same way; a family that
    spells pairs like that needs its own :class:`PairRule`.
    """
    return PairRule(name, positive=(r".+(\+|_P)",), negative=(r".+(-|_N)",))


RULES: dict[str, PairRule] = {"usb": USB, "can": CAN, "diff": suffix_rule()}
"""Built-in rules by name; add to it to make a rule available by name."""


def classify(pin_name: str, rule: PairRule | str = USB) -> Polarity | None:
    """Which half of the pair ``pin_name`` is, by ``rule`` (an object or a name)."""
    resolved = RULES[rule] if isinstance(rule, str) else rule
    return resolved.classify(pin_name)


def usb_polarity(pin_name: str) -> Literal["D+", "D-"] | None:
    """``"D+"``, ``"D-"`` or None for a pin name, by the USB rule."""
    polarity = USB.classify(pin_name)
    if polarity is None:
        return None
    return "D+" if polarity == "P" else "D-"


# ------------------------------------------------------------ pin spellings

PinNameMatch = Literal["exact", "normalised", "alternate"]

_ACTIVE_LOW_PREFIX = re.compile(r"^(?:[/!#*]|n(?=[A-Z0-9]))")
_ACTIVE_LOW_SUFFIX = re.compile(r"(?:_N|#|\*|_BAR)$", re.I)
_MARKUP = re.compile(r"[~_^]\{([^}]*)\}")


def normalise_pin_name(name: str) -> str:
    """One spelling for the many ways a datasheet pin name is written.

    Overbar markup comes off (``~{CHG}`` is ``CHG``), and so do the other
    active-low markers: a leading ``/``, ``!``, ``#`` or ``*``, a lowercase
    ``n`` before an upper-case letter or digit (``nCHG``), and a trailing
    ``_N``, ``#``, ``*`` or ``_BAR`` (``CHG_N``, ``RST#``). Subscript and
    superscript markup is read as plain text (``V_{CC}`` is ``VCC``). The
    result is upper case with surrounding spaces and underscores removed, so
    ``~{CHG}``, ``/CHG``, ``nCHG``, ``CHG_N`` and ``chg`` all read ``CHG``.

    Polarity is deliberately not kept: the question this answers is "which
    pin is the datasheet's CHG", not whether the symbol drew its bar. The
    ``_N`` rule also reads a differential ``CLK_N`` as ``CLK``, so a caller
    looking up one half of a pair should ask by its full name, which matches
    exactly first.
    """
    plain = _MARKUP.sub(r"\1", name).replace("\u2212", "-").strip(" _")
    plain = _ACTIVE_LOW_PREFIX.sub("", plain, count=1)
    if len(plain) > 2:
        plain = _ACTIVE_LOW_SUFFIX.sub("", plain)
    return plain.strip(" _").upper()


def pin_name_alternatives(name: str) -> tuple[str, ...]:
    """The ``/``-separated alternative functions of a pin, each normalised.

    ``GPIO16/U0TXD`` gives ``("GPIO16", "U0TXD")`` and ``DAT3_/_CS`` gives
    ``("DAT3", "CS")``. A leading ``/`` is an active-low marker, not a
    separator, so ``/CHG`` has no alternatives. Empty when the name has only
    one function.
    """
    plain = _MARKUP.sub(r"\1", name).strip()
    body = plain[1:] if plain.startswith("/") else plain
    parts = [normalise_pin_name(p) for p in body.split("/")]
    parts = [p for p in parts if p]
    return tuple(parts) if len(parts) > 1 else ()


def match_pin_name(pin_name: str, wanted: str) -> PinNameMatch | None:
    """How a symbol's pin name matches a wanted datasheet name, if it does.

    ``"exact"`` when the text is identical, ``"normalised"`` when the two read
    the same after :func:`normalise_pin_name`, ``"alternate"`` when the wanted
    name is one of the pin's ``/``-separated functions
    (:func:`pin_name_alternatives`), and None otherwise.
    """
    if not pin_name or not wanted:
        return None
    if pin_name == wanted:
        return "exact"
    key = normalise_pin_name(wanted)
    if not key:
        return None
    if normalise_pin_name(pin_name) == key:
        return "normalised"
    if key in pin_name_alternatives(pin_name):
        return "alternate"
    return None
