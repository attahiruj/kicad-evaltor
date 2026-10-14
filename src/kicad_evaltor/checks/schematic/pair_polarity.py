from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.connectivity import (
    Bridge,
    Connectivity,
    any_of,
    pin_pairs,
    same_pin_name,
    two_terminal_passives,
)
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.pin_roles import RULES
from kicad_evaltor.schematic_file import NetlistUnavailable


@dataclass
class PairPolarityParams(CheckParams):
    """Check one differential pair between two parts, half by half.

    ``rule`` names a pin-name rule in :data:`kicad_evaltor.pin_roles.RULES`
    (``usb``, ``can``, ``diff``). The pair is followed through series
    resistors, inductors and ferrite beads; ``through`` adds parts whose
    same-named pins conduct (flow-through protection arrays drawn that way),
    and ``through_pins`` adds parts by explicit pin pairs, as
    ``{"U3": [["1", "10"], ["2", "9"]]}``. Parts are a reference or a
    ``lib_id`` glob.
    """

    from_ref: str
    to_ref: str
    rule: str = "usb"
    through: list[str] = field(default_factory=list)
    through_pins: dict[str, list[list[str]]] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.from_ref or not self.to_ref:
            raise ValueError("from_ref and to_ref are required")
        if self.rule not in RULES:
            raise ValueError(f"unknown rule {self.rule!r}; known: {', '.join(sorted(RULES))}")
        for selector, pairs in self.through_pins.items():
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError(f"through_pins[{selector!r}] must be a list of pin pairs")


@register
class PairPolarityCheck(Check[PairPolarityParams]):
    id = "sch.pair.polarity"
    name = "Pair Polarity"
    description = "Checks each half of a differential pair reaches the same half, never the other"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[PairPolarityParams]] = PairPolarityParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        try:
            netlist = ctx.schematic.get_netlist()
        except NetlistUnavailable as exc:
            return CheckResult.error(self.id, str(exc))

        rule = RULES[params.rule]
        graph = Connectivity(netlist)
        through = self._bridge()

        sources = {
            pin.pin: (pin, rule.classify(pin.pinfunction))
            for pin in netlist.pins_of(params.from_ref)
        }
        halves = {num: pol for num, (_, pol) in sources.items() if pol is not None}
        if not halves:
            return CheckResult.fail(
                self.id,
                f"{params.from_ref} has no connected {params.rule} pair pins",
                from_ref=params.from_ref,
            )

        problems: list[str] = []
        routes: list[dict[str, Any]] = []
        reached_nets: dict[str, set[str]] = {"P": set(), "N": set()}
        for number, polarity in sorted(halves.items()):
            pin = sources[number][0]
            reach = graph.reach(pin.net, through=through)
            reached_nets[polarity] |= reach.nets
            targets = [
                (t.pin, t.pinfunction, rule.classify(t.pinfunction))
                for t in reach.pins_of(params.to_ref)
            ]
            same = [t for t in targets if t[2] == polarity]
            other = [t for t in targets if t[2] not in (None, polarity)]
            label = f"{params.from_ref}.{pin.pinfunction or number}"
            routes.append(
                {
                    "from": label,
                    "polarity": polarity,
                    "reaches": [f"{params.to_ref}.{name or num}" for num, name, _ in targets],
                    "through": reach.crossed,
                }
            )
            if other:
                names = ", ".join(f"{params.to_ref}.{name or num}" for num, name, _ in other)
                problems.append(f"{label} reaches the opposite half: {names}")
            if not same:
                problems.append(f"{label} does not reach a {polarity} pin of {params.to_ref}")

        shorted = reached_nets["P"] & reached_nets["N"]
        if shorted:
            problems.append(f"the two halves are joined on {', '.join(sorted(shorted))}")

        details = {
            "from_ref": params.from_ref,
            "to_ref": params.to_ref,
            "rule": params.rule,
            "routes": routes,
        }
        if problems:
            return CheckResult.fail(self.id, "; ".join(problems), problems=problems, **details)
        return CheckResult.pass_(
            self.id,
            f"{params.rule} pair from {params.from_ref} reaches {params.to_ref} with polarity kept",
            **details,
        )

    def _bridge(self) -> Bridge:
        params = self.params
        bridges: list[Bridge] = [two_terminal_passives]
        if params.through:
            bridges.append(same_pin_name(*params.through))
        for selector, pairs in params.through_pins.items():
            bridges.append(pin_pairs(selector, *((a, b) for a, b in pairs)))
        return any_of(*bridges)
