from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.connectivity import Connectivity
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.netlist import FileNetlist


@dataclass
class ComponentsConnectedParams(CheckParams):
    reference_a: str
    reference_b: str
    min_connections: int = 1
    through_passives: bool = False
    """Also count connections made through series resistors, inductors and
    ferrite beads, stopping at power rails."""

    def validate(self) -> None:
        if not self.reference_a:
            raise ValueError("reference_a is required")
        if not self.reference_b:
            raise ValueError("reference_b is required")
        if self.min_connections < 1:
            raise ValueError("min_connections must be >= 1")


@register
class ComponentsConnectedCheck(Check[ComponentsConnectedParams]):
    id = "sch.components.connected"
    name = "Components Connected"
    description = "Checks if two components are electrically connected"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[ComponentsConnectedParams]] = ComponentsConnectedParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        netlist = ctx.schematic.get_netlist()

        if params.through_passives:
            nets_with_both = _nets_reaching(netlist, params.reference_a, params.reference_b)
        else:
            nets_with_both = []
            for net in netlist.nets:
                nodes = net.nodes
                refs = {node.ref for node in nodes}
                if params.reference_a in refs and params.reference_b in refs:
                    nets_with_both.append(net.name)

        if len(nets_with_both) >= params.min_connections:
            return CheckResult.pass_(
                self.id,
                f"Components {params.reference_a} and {params.reference_b} share {len(nets_with_both)} net(s)",
                reference_a=params.reference_a,
                reference_b=params.reference_b,
                shared_nets=nets_with_both,
                count=len(nets_with_both),
            )

        return CheckResult.fail(
            self.id,
            f"Components {params.reference_a} and {params.reference_b} share only {len(nets_with_both)} net(s), "
            f"minimum required: {params.min_connections}",
            reference_a=params.reference_a,
            reference_b=params.reference_b,
            shared_nets=nets_with_both,
            count=len(nets_with_both),
        )


def _nets_reaching(netlist: FileNetlist, ref_a: str, ref_b: str) -> list[str]:
    """The nets of ``ref_a`` that reach ``ref_b`` across series passives.

    One entry per net of ``ref_a``, so two pins of ``ref_a`` on one net count
    once, as they do in the direct comparison.
    """
    graph = Connectivity(netlist)
    found: list[str] = []
    for node in netlist.pins_of(ref_a):
        if node.net in found:
            continue
        reach = graph.reach(node.net)
        # The series part itself is not the far component: a resistor between
        # A and B reaches both, and that is the connection being asked about.
        if any(n.ref == ref_b for n in reach.nodes):
            found.append(node.net)
    return found
