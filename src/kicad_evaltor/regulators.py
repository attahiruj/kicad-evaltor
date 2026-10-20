"""Configured regulator setpoints under a model, not proof of operation.

"Is this rail 3.3 V?" cannot be answered from a symbol name alone: an
adjustable LDO with the right divider is as correct as a fixed 3.3 V part. This
module works it out the way a reviewer would:

* a **fixed** part outputs what its name says (``AMS1117-3.3``, ``L7805``);
* a **divider** part outputs ``Vref x (1 + Rtop / Rbottom)``, with the
  resistors read off the netlist around its feedback pin;
* a **current-reference** part (LT3080, LT3042/45) outputs ``Iset x Rset``,
  with ``Rset`` from its SET pin to ground.

What a part is, and its ``Vref`` or ``Iset``, is a :class:`RegulatorModel`.
Models for common stock ``Regulator_Linear`` parts ship in
``_regulators.json``, each citing its datasheet parameter; anything else can
be added with :func:`register_regulator` or passed straight in. When the
answer cannot be worked out, the result says why instead of guessing.

The divider is solved as a resistor network, not pattern-matched, so a top
resistor made of two in series, or a bottom one of two in parallel, gives the
right answer. Capacitors are ignored, as they are at DC; any other part on the
feedback network makes the result unknown.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from kicad_evaltor.connectivity import power_rails, ref_prefix
from kicad_evaltor.netlist import FileNetComponent, FileNetlist, FileNetNode
from kicad_evaltor.values import parse_value

RegulatorKind = Literal["fixed", "divider", "current_reference", "output_reference"]

OUT_PIN_NAMES = ("OUT", "VOUT", "VO", "OUTPUT", "V_OUT")
FEEDBACK_PIN_NAMES = ("ADJ", "FB", "SET", "ADJ/GND", "SENSE/ADJ", "VFB", "ADJ/NC", "NC/FB", "FB/NC")
GROUND_PIN_NAMES = ("GND", "VSS", "AGND", "PGND", "ADJ/GND")


@dataclass(frozen=True)
class RegulatorModel:
    """How a regulator sets its output.

    ``vout`` is a fixed part's output; ``vref`` the feedback reference of a
    divider part (negative for a negative regulator); ``iset`` the SET-pin
    current of a current-reference part. Pin names default to the common
    spellings and can be narrowed for a part that uses others.
    """

    kind: RegulatorKind
    vout: float | None = None
    vref: float | None = None
    iset: float | None = None
    out_pins: tuple[str, ...] = OUT_PIN_NAMES
    feedback_pins: tuple[str, ...] = FEEDBACK_PIN_NAMES
    source: str = ""
    manufacturer: str = ""
    revision: str = ""
    package: str = ""
    out_numbers: tuple[str, ...] = ()
    feedback_numbers: tuple[str, ...] = ()
    ground_numbers: tuple[str, ...] = ()
    preset: Callable[[FileNetlist, str], float | None] | None = None

    def __post_init__(self) -> None:
        from math import isfinite

        if self.kind not in ("fixed", "divider", "current_reference", "output_reference"):
            raise ValueError(f"Unknown regulator kind {self.kind!r}")
        needed = {
            "fixed": "vout",
            "divider": "vref",
            "output_reference": "vref",
            "current_reference": "iset",
        }[self.kind]
        if getattr(self, needed) is None:
            raise ValueError(f"a {self.kind} regulator model needs {needed}")
        if any(not isfinite(v) for v in (self.vout, self.vref, self.iset) if v is not None):
            raise ValueError("Regulator parameters must be finite")


@dataclass
class RegulatorModels:
    """Evaluation-local model registrations; never mutates global defaults."""

    entries: list[tuple[str, RegulatorModel]] = field(default_factory=list)
    include_builtin: bool = True

    def register(self, pattern: str, model: RegulatorModel) -> None:
        self.entries.insert(0, (pattern, model))


@dataclass
class RegulatorOutput:
    """The worked-out output, or why there is none.

    ``volts`` is None when it could not be determined, and ``reason`` then
    says what was missing. ``method`` names how it was found, and ``parts``
    lists the resistors that set it.
    """

    reference: str
    volts: float | None
    method: str = ""
    reason: str = ""
    parts: tuple[str, ...] = field(default=())

    @property
    def known(self) -> bool:
        return self.volts is not None


# ------------------------------------------------------------------ models

_DATA_FILE = Path(__file__).with_name("_regulators.json")
_registered: list[tuple[str, RegulatorModel]] = []
_builtin: list[tuple[str, RegulatorModel]] | None = None


def register_regulator(patterns: str | Iterable[str], model: RegulatorModel) -> None:
    """Teach :func:`regulator_model` a part, by ``lib_id`` glob.

    Registered models are consulted before the built-in table, newest first,
    so a registration can correct or extend it.
    """
    for pattern in [patterns] if isinstance(patterns, str) else patterns:
        _registered.insert(0, (pattern, model))


def clear_registered_regulators() -> None:
    """Forget every :func:`register_regulator` call."""
    _registered.clear()


def regulator_model(
    lib_id: str, value: str = "", description: str = "", *, models: RegulatorModels | None = None
) -> RegulatorModel | None:
    """The model for a part: registered, built in, or read from what it says.

    Without a table entry, a fixed output is read from the description KiCad's
    library gives the part ("Fixed Output 3.3V", "3.3V fixed output") and from
    the symbol name (or value) where its spelling is unambiguous: a 78xx/79xx
    code or a voltage with a decimal point, as in ``AMS1117-3.3``. Other name
    suffixes are not read: ``-33`` means 3.3 V on one part and ``-12`` means
    12 V on another, and ``-7`` is often an ordering code.

    When the name and the description both give a voltage and they disagree,
    there is no model rather than a guess. A description calling the part
    negative makes a voltage read from the name negative.
    """
    entries = _registered if models is None else models.entries
    builtin = _builtin_models() if models is None or models.include_builtin else []
    for pattern, model in [*entries, *builtin]:
        if fnmatch.fnmatchcase(lib_id, pattern):
            return model
    if models is not None and not models.include_builtin:
        return None

    described = fixed_voltage_from_description(description)
    named: tuple[float, str] | None = None
    for text in (lib_id.partition(":")[2] or lib_id, value):
        volts = fixed_voltage_from_name(text)
        if volts is not None:
            named = (-volts if volts > 0 and _says_negative(description) else volts, text)
            break

    if named is not None and described is not None and abs(named[0] - described) > 1e-9:
        return None
    if named is not None:
        return RegulatorModel("fixed", vout=named[0], source=f"from the name {named[1]!r}")
    if described is not None:
        return RegulatorModel("fixed", vout=described, source="from the library description")
    return None


_DESCRIBED = (
    re.compile(r"fixed\s+(?:output\s+)?(?:voltage\s+)?(?:vo\s+)?([+-]?\d+(?:\.\d+)?)\s*V\b", re.I),
    # "3.3V fixed output", but not "20V Fixed LDO", where the voltage is a rating.
    re.compile(
        r"([+-]?\d+(?:\.\d+)?)\s*V\s+fixed\s+(?:positive\s+|negative\s+)?o(?:utput)?\b", re.I
    ),
    re.compile(r"([+-]?\d+(?:\.\d+)?)\s*V\s+output\b", re.I),
)


def fixed_voltage_from_description(description: str) -> float | None:
    """A fixed output stated in a symbol description, or None.

    Reads "Fixed Output 3.3V", "3.3V fixed output", "fixed 3.8V output",
    "Fixed Vo 3.3V" and "1.8V Output". A positive number in a description of
    a negative regulator is made negative.
    """
    for pattern in _DESCRIBED:
        found = pattern.search(description)
        if found:
            volts = float(found.group(1))
            if volts > 0 and _says_negative(description):
                volts = -volts
            return volts
    return None


def _says_negative(description: str) -> bool:
    text = description.lower()
    return "negative" in text and "positive" not in text


def _builtin_models() -> list[tuple[str, RegulatorModel]]:
    global _builtin
    if _builtin is None:
        data = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        loaded: list[tuple[str, RegulatorModel]] = []
        for entry in data["models"]:
            model = RegulatorModel(
                kind=entry["kind"],
                vout=entry.get("vout"),
                vref=entry.get("vref"),
                iset=entry.get("iset"),
                source=entry.get("source", ""),
            )
            loaded.extend((pattern, model) for pattern in entry["patterns"])
        _builtin = loaded
    return _builtin


_SERIES_78 = re.compile(
    r"^(?:L|LM|MC|UA|KA|NCV)?(78|79)(?:M|L|S)?(\d{2}(?:\.\d)?)(?:[^\d]|$)", re.I
)
_EXPLICIT = re.compile(r"[-_](\d{1,2}\.\d{1,3})V?(?=$|[-_xX])")


def fixed_voltage_from_name(name: str) -> float | None:
    """A fixed regulator's output from its name, or None when it is not certain.

    ``AMS1117-3.3`` gives 3.3, ``L7805`` 5, ``L78L33`` 3.3, ``L7885`` 8.5,
    ``MC7905.2`` -5.2 and ``L7912`` -12 (the 79 series is negative). A name
    says nothing about polarity otherwise, so ``ADP7182AUJZ-3.3`` gives 3.3
    for what is a -3.3 V part; :func:`regulator_model` corrects that from the
    description.
    """
    series = _SERIES_78.match(name)
    if series:
        code = float(series.group(2))
        volts = code / 10 if code > 24 else code
        return -volts if series.group(1) == "79" else volts
    found = _EXPLICIT.findall(name)
    return float(found[-1]) if found else None


# --------------------------------------------------------------- solving


def regulator_output(
    netlist: FileNetlist,
    reference: str,
    *,
    model: RegulatorModel | None = None,
    models: RegulatorModels | None = None,
) -> RegulatorOutput:
    """Work out what ``reference`` outputs in this design."""
    component = netlist.components.get(reference)
    pins = netlist.pins_of(reference)
    if component is None and not pins:
        return RegulatorOutput(reference, None, reason=f"{reference} is not in the netlist")
    component = component or FileNetComponent(reference)
    model = model or regulator_model(
        component.lib_id, component.value, component.description, models=models
    )
    if model is None:
        return RegulatorOutput(
            reference,
            None,
            reason=f"no regulator model for {component.lib_id or reference}; "
            "pass model= or call register_regulator",
        )

    if model.preset is not None:
        preset = model.preset(netlist, reference)
        if preset is not None:
            import math

            if not math.isfinite(preset):
                return RegulatorOutput(
                    reference, None, reason="preset model returned a non-finite voltage"
                )
            return RegulatorOutput(reference, preset, method=f"preset ({model.source})")
    if model.kind == "fixed":
        assert model.vout is not None
        return RegulatorOutput(reference, model.vout, method=f"fixed ({model.source})")

    feedback = (
        next((p for p in pins if p.pin in model.feedback_numbers), None)
        if model.feedback_numbers
        else _pin_named(pins, model.feedback_pins)
    )
    if feedback is None:
        return RegulatorOutput(reference, None, reason=f"{reference} has no connected feedback pin")
    grounds = _ground_nets(netlist, pins)
    if model.ground_numbers:
        grounds = {p.net for p in pins if p.pin in model.ground_numbers}
    if not grounds:
        return RegulatorOutput(reference, None, reason=f"no ground net found around {reference}")

    if model.kind in ("divider", "output_reference"):
        assert model.vref is not None
        out = (
            next((p for p in pins if p.pin in model.out_numbers), None)
            if model.out_numbers
            else _pin_named(pins, model.out_pins)
        )
        if out is None:
            return RegulatorOutput(
                reference, None, reason=f"{reference} has no connected output pin"
            )
        if out.net in grounds:
            return RegulatorOutput(reference, None, reason=f"{reference} output pin is grounded")
        if feedback.net in grounds and model.kind == "output_reference":
            return RegulatorOutput(
                reference,
                _round(model.vref),
                method=f"Vref {model.vref:g} V above grounded adjustment pin ({model.source})",
            )
        if feedback.net in grounds:
            return RegulatorOutput(reference, None, reason=f"{reference} feedback pin is grounded")
        solved = _solve(
            netlist, reference, feedback.net, fixed={out.net: 1.0, **dict.fromkeys(grounds, 0.0)}
        )
        if isinstance(solved, str):
            return RegulatorOutput(reference, None, reason=solved)
        voltages, parts = solved
        ratio = voltages[feedback.net]
        factor = 1 - ratio if model.kind == "output_reference" else ratio
        if factor <= 0:
            return RegulatorOutput(
                reference, None, reason="feedback divider does not reach the output"
            )
        return RegulatorOutput(
            reference,
            _round(model.vref / factor),
            method=f"Vref {model.vref:g} V through divider ({model.source})",
            parts=parts,
        )

    assert model.iset is not None
    solved = _solve(
        netlist,
        reference,
        feedback.net,
        fixed=dict.fromkeys(grounds, 0.0),
        injected={feedback.net: model.iset},
    )
    if isinstance(solved, str):
        return RegulatorOutput(reference, None, reason=solved)
    voltages, parts = solved
    return RegulatorOutput(
        reference,
        _round(voltages[feedback.net]),
        method=f"Iset {model.iset:g} A into SET resistance ({model.source})",
        parts=parts,
    )


def _pin_named(pins: list[FileNetNode], names: tuple[str, ...]) -> FileNetNode | None:
    wanted = {n.upper() for n in names}
    for pin in pins:
        if pin.pinfunction.upper() in wanted:
            return pin
    return None


def _ground_nets(netlist: FileNetlist, pins: list[FileNetNode]) -> set[str]:
    grounds = {p.net for p in pins if p.pinfunction.upper() in GROUND_PIN_NAMES}
    grounds |= {
        net.name
        for net in netlist.nets
        if re.match(r"^(/.*/)?(A|D|P)?GND[A-Z0-9_]*$|^(/.*/)?VSS[A-Z0-9_]*$", net.name, re.I)
    }
    return grounds


def _is_resistor(component: FileNetComponent, pins: list[FileNetNode]) -> bool:
    return ref_prefix(component.ref).upper() == "R" and len(pins) == 2


def _is_capacitor(component: FileNetComponent) -> bool:
    return ref_prefix(component.ref).upper() == "C"


def _solve(
    netlist: FileNetlist,
    regulator: str,
    start: str,
    *,
    fixed: dict[str, float],
    injected: dict[str, float] | None = None,
) -> tuple[dict[str, float], tuple[str, ...]] | str:
    """Node voltages of the resistor network around ``start``.

    Nets in ``fixed`` are held at their voltage; the rest found by following
    resistors from ``start`` are solved by nodal analysis, with ``injected``
    currents flowing in. Returns the voltages and the resistors used, or a
    reason the network cannot be solved.
    """
    unknown: list[str] = []
    resistors: dict[str, tuple[str, str, float]] = {}
    queue = [start]
    seen = {start}
    while queue:
        net = queue.pop(0)
        unknown.append(net)
        for node in netlist.nodes_on(net):
            if node.ref == regulator:
                continue
            component = netlist.components.get(node.ref) or FileNetComponent(node.ref)
            pins = netlist.pins_of(node.ref)
            if _is_capacitor(component):
                continue
            if not _is_resistor(component, pins):
                return f"{node.ref} on net {net} is not a resistor or capacitor"
            if node.ref in resistors:
                continue
            ohms = parse_value(component.value)
            if ohms is None or ohms < 0:
                return f"cannot read {node.ref}'s value {component.value!r} as a resistance"
            other = pins[1].net if pins[0].net == net else pins[0].net
            resistors[node.ref] = (net, other, ohms)
            if other in fixed or other in seen:
                continue
            net_obj = netlist.net(other)
            if net_obj is not None and power_rails(net_obj):
                return f"{node.ref} connects the feedback network to the rail {other}"
            seen.add(other)
            queue.append(other)

    from kicad_evaltor.circuits.resistive import DCNetwork, NetworkUnavailable, Resistor

    try:
        network = DCNetwork(Resistor(ref, a, b, ohms) for ref, (a, b, ohms) in resistors.items())
        solution = network.solve(fixed, injected=injected)
        if start not in solution.voltages:
            return "the feedback network has no path to a fixed voltage"
    except NetworkUnavailable as exc:
        return f"the feedback network has no path to a fixed voltage: {exc}"
    return solution.voltages, tuple(sorted(resistors))


def _round(volts: float) -> float:
    return float(f"{volts:.6g}")
