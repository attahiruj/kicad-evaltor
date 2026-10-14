from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.regulators import regulator_output
from kicad_evaltor.schematic_file import NetlistUnavailable
from kicad_evaltor.values import values_match


@dataclass
class RegulatorOutputParams(CheckParams):
    """``expected`` volts, within ``tolerance`` (a fraction: 0.03 is 3 %).

    The tolerance absorbs E-series resistor rounding in a divider, so a
    3.3 V rail built from standard values still passes.
    """

    reference: str
    expected: float
    tolerance: float = 0.03

    def validate(self) -> None:
        if not self.reference:
            raise ValueError("reference is required")
        if not 0 <= self.tolerance < 1:
            raise ValueError("tolerance must be a fraction between 0 and 1")


@register
class RegulatorOutputCheck(Check[RegulatorOutputParams]):
    id = "sch.regulator.output_voltage"
    name = "Regulator Output Voltage"
    description = "Works out a linear regulator's output from its part and divider"
    category = CheckCategory.SCHEMATIC
    Params: ClassVar[type[RegulatorOutputParams]] = RegulatorOutputParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_schematic():
            return CheckResult.skip(self.id, "No schematic available")

        params = self.params
        try:
            netlist = ctx.schematic.get_netlist()
        except NetlistUnavailable as exc:
            return CheckResult.error(self.id, str(exc))

        output = regulator_output(netlist, params.reference)
        details = {
            "reference": params.reference,
            "expected": params.expected,
            "actual": output.volts,
            "method": output.method,
            "parts": list(output.parts),
        }
        if output.volts is None:
            # Not knowing is not the same as being wrong; say why instead.
            return CheckResult.error(
                self.id, f"Cannot work out {params.reference}'s output: {output.reason}", **details
            )
        if values_match(output.volts, params.expected, params.tolerance):
            return CheckResult.pass_(
                self.id,
                f"{params.reference} outputs {output.volts:g} V ({output.method})",
                **details,
            )
        return CheckResult.fail(
            self.id,
            f"{params.reference} outputs {output.volts:g} V, expected {params.expected:g} V "
            f"within {params.tolerance:.0%} ({output.method})",
            **details,
        )
