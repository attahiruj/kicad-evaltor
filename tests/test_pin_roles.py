"""Differential pair pin names, and the pair polarity check."""

import pytest

from conftest import FakeContext, FakeSchematic, netlist_of
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.schematic.pair_polarity import PairPolarityCheck
from kicad_evaltor.pin_roles import CAN, USB, PairRule, classify, suffix_rule, usb_polarity


class TestUsbPolarity:
    @pytest.mark.parametrize(
        ("name", "polarity"),
        [
            ("D+", "D+"),
            ("D-", "D-"),
            ("UD+", "D+"),
            ("UD-", "D-"),
            ("USBDP", "D+"),
            ("USBDM", "D-"),
            ("USBD+", "D+"),
            ("USBD-", "D-"),
            ("USB_D+", "D+"),
            ("USB_D-", "D-"),
            ("DP", "D+"),
            ("DM", "D-"),
            ("DN", "D-"),
            ("PA12/USB_DP", "D+"),
            ("PA11/USB_DM", "D-"),
            ("~{D-}", "D-"),
            ("D" + chr(0x2212), "D-"),  # a typographic minus
        ],
    )
    def test_spellings(self, name, polarity):
        assert usb_polarity(name) == polarity

    @pytest.mark.parametrize("name", ["VBUS", "GND", "CC1", "SBU1", "DIP", "DMA", "D", "VDD", ""])
    def test_other_pins(self, name):
        assert usb_polarity(name) is None


class TestOtherRules:
    def test_can(self):
        assert [classify(n, CAN) for n in ("CANH", "CAN_L", "CAN1H", "CAN+", "CAN_TX")] == [
            "P",
            "N",
            "P",
            "P",
            None,
        ]

    def test_suffix_rule_needs_an_explicit_suffix(self):
        rule = suffix_rule()

        assert [rule.classify(n) for n in ("TX+", "TX-", "CLK_P", "CLK_N")] == ["P", "N", "P", "N"]
        assert [rule.classify(n) for n in ("EN", "VCCP", "RXP")] == [None, None, None]

    def test_rules_by_name_and_custom(self):
        custom = PairRule("lvds", positive=(r"RXP\d?",), negative=(r"RXN\d?",))

        assert classify("UD+", "usb") == "P"
        assert custom.classify("RXN0") == "N"
        assert USB.name == "usb"


def usb_link(*, swap=False, through_esd=False, short=False):
    """J1 (USB-B) to U1 (CH340) through 22R series resistors.

    ``swap`` crosses the pair at the bridge; ``through_esd`` puts a
    flow-through array U2 between connector and resistors; ``short`` ties
    both lines together through R3.
    """
    dp, dm = ("UD-", "UD+") if swap else ("UD+", "UD-")
    nets = [
        ("/CDP", [("J1", "3", "D+"), ("U2", "1", "IO1") if through_esd else ("R1", "1")]),
        ("/CDM", [("J1", "2", "D-"), ("U2", "3", "IO2") if through_esd else ("R2", "1")]),
        ("/DP", [("R1", "2"), ("U1", "5", dp)]),
        ("/DM", [("R2", "2"), ("U1", "6", dm)]),
        ("GND", [("J1", "4", "GND"), ("U1", "1", "GND")]),
    ]
    if through_esd:
        nets += [
            ("/EDP", [("U2", "6", "IO1"), ("R1", "1")]),
            ("/EDM", [("U2", "4", "IO2"), ("R2", "1")]),
        ]
    if short:
        nets[2][1].append(("R3", "1"))
        nets[3][1].append(("R3", "2"))
    return netlist_of(*nets)


def run(netlist, **params):
    params = {"from_ref": "J1", "to_ref": "U1", **params}
    return PairPolarityCheck(**params).run(FakeContext(schematic=FakeSchematic(netlist=netlist)))


class TestPairPolarityCheck:
    def test_straight_through_series_resistors_passes(self):
        result = run(usb_link())

        assert result.is_pass, result.message
        routes = {r["from"]: r for r in result.details["routes"]}
        assert routes["J1.D+"]["reaches"] == ["U1.UD+"]
        assert routes["J1.D+"]["through"] == ["R1"]

    def test_a_swapped_pair_fails(self):
        result = run(usb_link(swap=True))

        assert result.is_fail
        assert "J1.D+ reaches the opposite half: U1.UD-" in result.details["problems"]

    def test_a_shorted_pair_fails(self):
        result = run(usb_link(short=True))

        assert result.is_fail
        assert any("joined" in p or "opposite" in p for p in result.details["problems"])

    def test_flow_through_esd_needs_to_be_named(self):
        unnamed = run(usb_link(through_esd=True))
        by_name = run(usb_link(through_esd=True), through=["U2"])
        by_pins = run(usb_link(through_esd=True), through_pins={"U2": [["1", "6"], ["3", "4"]]})

        assert unnamed.is_fail
        assert "does not reach" in unnamed.message
        assert by_name.is_pass, by_name.message
        assert by_pins.is_pass, by_pins.message

    def test_no_pair_pins_on_the_source_fails(self):
        result = run(usb_link(), from_ref="U9")

        assert result.is_fail
        assert "no connected usb pair pins" in result.message

    def test_skips_without_schematic(self):
        check = PairPolarityCheck(from_ref="J1", to_ref="U1")

        assert check.run(FakeContext()).status == TestStatus.SKIP

    @pytest.mark.parametrize(
        "params",
        [
            {"from_ref": "", "to_ref": "U1"},
            {"from_ref": "J1", "to_ref": "U1", "rule": "rs485"},
            {"from_ref": "J1", "to_ref": "U1", "through_pins": {"U2": [["1"]]}},
        ],
    )
    def test_rejects_bad_params(self, params):
        with pytest.raises(ValueError):
            PairPolarityCheck(**params)
