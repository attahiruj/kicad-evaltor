"""Caller-defined exact or inclusive net topology."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.checks.schematic.pins_connected import endpoint
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.schematic_file import NetlistUnavailable


@dataclass
class NetMembersParams(CheckParams):
    net: str
    pins: list[str]
    exact: bool = True

    def validate(self) -> None:
        if not self.net or not self.pins:
            raise ValueError("Give a net and at least one expected pin")
        for pin in self.pins:
            endpoint(pin)


@register
class NetMembersCheck(Check[NetMembersParams]):
    id = "sch.net.members"
    name = "Net Members"
    description = "Check a net's expected pins, optionally forbidding extra members"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[NetMembersParams]] = NetMembersParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")
        p = self.params
        try:
            netlist = ctx.schematic.get_netlist()
        except NetlistUnavailable as exc:
            return CheckResult.error(self.id, f"Cannot read the netlist: {exc}")
        net = netlist.net(p.net)
        if net is None:
            return CheckResult.fail(self.id, f"Net {p.net!r} not found")
        actual = {f"{node.ref}.{node.pin}" for node in net.nodes}
        expected = set(p.pins)
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        result = CheckResult.fail if missing or (p.exact and extra) else CheckResult.pass_
        return result(
            self.id,
            f"Net {p.net}: {len(actual)} pins",
            net=p.net,
            actual=sorted(actual),
            expected=sorted(expected),
            missing=missing,
            extra=extra,
            exact=p.exact,
            selected_count=len(actual),
        )
