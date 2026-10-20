"""Reading component values as numbers.

A schematic writes values the way engineers do: ``10k``, ``4k7``, ``2R2``,
``100nF``, ``0.1u``, ``3V3``, ``1M``, ``10k 1%``. :func:`parse_value` turns
those into floats in base units, so ``"4k7"`` and ``"4.7k"`` and ``"4700"``
compare equal. Anything it cannot read with confidence comes back as None
rather than a guess.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Quantity:
    value: float | None
    dimension: str | None
    token: str
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None


_UNITS = {
    "ohm": "resistance",
    "ohms": "resistance",
    "Ω": "resistance",
    "F": "capacitance",
    "Hz": "frequency",
    "H": "inductance",
    "V": "voltage",
    "A": "current",
    "W": "power",
    "s": "time",
}


def parse_quantity(text: str | None, *, dimension: str | None = None) -> Quantity:
    """Read annotated quantities, with an optional caller-supplied dimension.

    Explicit incompatible units are never reinterpreted. A bare engineering
    value such as 100n may take the requested dimension. Multiple candidates
    of that dimension are reported as ambiguous rather than guessed.
    """
    if dimension is not None and dimension not in set(_UNITS.values()):
        raise ValueError(f"Unknown dimension {dimension!r}")
    cleaned = (text or "").strip().replace("µ", "u").replace("μ", "u")
    cleaned = re.sub(r"([\d.])\s+(?=[A-Za-zΩ])", r"\1", cleaned)
    explicit: list[Quantity] = []
    untyped: list[Quantity] = []
    for token in re.split(r"[\s/,;=()_]+", cleaned):
        token = re.sub(r"(?i:ohms?)$", "ohm", token).replace("Ω", "Ω")
        token = re.sub(r"(?i:hz)$", "Hz", token)
        value = parse_value(token)
        if value is None:
            continue
        unit = next((u for u in sorted(_UNITS, key=len, reverse=True) if token.endswith(u)), None)
        kind = _UNITS.get(unit) if unit else None
        if kind is None and re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)?R\d*", token):
            kind = "resistance"
        if kind is None and re.fullmatch(r"[+-]?\d*V\d+", token):
            kind = "voltage"
        if kind is not None:
            if dimension is None or dimension == kind:
                explicit.append(Quantity(value, kind, token))
        elif not token.endswith("%"):
            untyped.append(Quantity(value, dimension, token))
    choices = explicit + untyped if dimension is None else explicit or untyped
    if len(choices) == 1:
        return choices[0]
    return Quantity(
        None, dimension, cleaned, "ambiguous quantity" if choices else "no matching quantity"
    )


PREFIXES: dict[str, float] = {
    "f": 1e-15,
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "R": 1.0,
    "V": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "MEG": 1e6,
    "G": 1e9,
    "T": 1e12,
}
"""SI multipliers by letter. ``m`` is milli and ``M`` mega, as SPICE and KiCad
read them; ``MEG`` (any case) is also mega. ``R`` and ``V`` multiply by one and
only mark the decimal point, as in ``2R2`` and ``3V3``."""

_DECIMAL_MARKS = ("R", "V")

_VALUE = re.compile(
    r"""^
    (?P<sign>[+-]?)
    (?P<int>\d*)
    (?:\.(?P<frac>\d+))?
    (?P<prefix>MEG|[fpnumRVkKMGT])?
    (?P<tail>\d*)
    (?P<unit>ohms|ohm|Ω|Hz|F|H|V|A|W|s)?
    $""",
    re.VERBOSE,
)


def parse_value(text: str | None) -> float | None:
    """The numeric value of a component value string, in base units.

    Reads plain and engineering notation (``4700``, ``4.7k``, ``4.7e3``), RKM
    notation with the multiplier as the decimal point (``4k7``, ``2R2``,
    ``R47``, ``3V3``), an optional unit (``100nF``, ``100 nF``, ``10 ohm``,
    ``3.3V``), and ignores whatever follows, such as a tolerance or rating
    (``10k 1%``, ``100n/50V``). Returns None for anything else, including an
    empty value and words like ``DNP``.
    """
    if not text:
        return None
    cleaned = text.strip().replace("µ", "u").replace("μ", "u")
    # "100 nF" and "10 ohm": a unit or prefix written apart from its number.
    cleaned = re.sub(r"^([+-]?[\d.]+)\s+(?=[A-Za-zΩ])", r"\1", cleaned)
    word = re.split(r"[\s/,;_]+", cleaned, maxsplit=1)[0]
    word = re.sub(r"(?i)meg", "MEG", word)
    if not word:
        return None

    number = _plain_float(word)
    if number is not None:
        return number

    match = _VALUE.match(word)
    if match is None:
        return None
    sign, integer, frac, prefix, tail, unit = match.group(
        "sign", "int", "frac", "prefix", "tail", "unit"
    )
    if not (integer or frac or tail):
        return None
    if prefix is None:
        # Only a unit after the number, as in "5V" or "10ohm".
        return None if tail else _clean(float(f"{sign}{integer or 0}.{frac or 0}"))
    if tail and frac:
        return None  # "4.7k7" means nothing
    if prefix in _DECIMAL_MARKS and unit is not None:
        return None  # "3VV", "2RΩ"
    mantissa = float(f"{sign}{integer or 0}.{tail or frac or 0}")
    return _clean(mantissa * PREFIXES[prefix])


def _plain_float(word: str) -> float | None:
    """A word that is a number on its own, scientific notation included."""
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", word):
        return None
    value = float(word)
    return value if math.isfinite(value) else None


def _clean(value: float) -> float | None:
    """Undo binary rounding noise, so ``4k7`` is exactly ``4700.0``."""
    return float(f"{value:.12g}") if math.isfinite(value) else None


def values_match(a: float, b: float, tolerance: float) -> bool:
    """Whether ``a`` is within ``tolerance`` (a fraction, 0.05 = 5 %) of ``b``."""
    if b == 0:
        return abs(a) <= tolerance
    return abs(a - b) <= abs(b) * tolerance
