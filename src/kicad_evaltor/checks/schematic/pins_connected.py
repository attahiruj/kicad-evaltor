"""Pin-specific connectivity with caller-selected conducting pin pairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.connectivity import Connectivity, any_of, never, pin_pairs
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.schematic_file import NetlistUnavailable


def endpoint(value: str) -> tuple[str, str]:
    ref, sep, pin = value.partition(".")
    if not sep or not ref or not pin:
        raise ValueError("Pins must be REF.NUMBER, for example U1.3")
    return ref, pin


@dataclass
class PinsConnectedParams(CheckParams):
    pin_a: str
    pin_b: str
    connected: bool = True
    through: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    """Explicit internal conducting pairs, e.g. {"R1": [("1", "2")]}."""

    def validate(self) -> None:
        endpoint(self.pin_a)
        endpoint(self.pin_b)
        for ref, pairs in self.through.items():
            if not ref or any(len(pair) != 2 or not all(pair) for pair in pairs):
                raise ValueError("through needs references and nonempty pin pairs")


@register
class PinsConnectedCheck(Check[PinsConnectedParams]):
    id = "sch.pins.connected"
    name = "Pins Connected"
    description = "Require or forbid a connection between specific pins"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[PinsConnectedParams]] = PinsConnectedParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")
        p = self.params
        try:
            netlist = ctx.schematic.get_netlist()
        except NetlistUnavailable as exc:
            return CheckResult.error(self.id, f"Cannot read the netlist: {exc}")
        a, b = netlist.node(*endpoint(p.pin_a)), netlist.node(*endpoint(p.pin_b))
        if a is None or b is None:
            return CheckResult.error(
                self.id,
                "Endpoint absent from exported netlist",
                missing=[name for name, node in ((p.pin_a, a), (p.pin_b, b)) if node is None],
            )
        bridge = any_of(*(pin_pairs(ref, *pairs) for ref, pairs in p.through.items()))
        reached = Connectivity(netlist).reach(a.net, through=bridge, stop_at=never)
        connected = b.net in reached.nets
        result = CheckResult.pass_ if connected == p.connected else CheckResult.fail
        return result(
            self.id,
            f"{p.pin_a} and {p.pin_b}: connected={connected}",
            pin_a=p.pin_a,
            pin_b=p.pin_b,
            net_a=a.net,
            net_b=b.net,
            connected=connected,
            expected_connected=p.connected,
            reachable_nets=sorted(reached.nets),
            crossed_components=reached.crossed,
        )
