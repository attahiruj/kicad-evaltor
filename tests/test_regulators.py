"""Component values as numbers, and regulator output voltages."""

import pytest

from conftest import FakeContext, FakeSchematic, FakeSymbol, netlist_of
from kicad_evaltor.checks.base import TestStatus
from kicad_evaltor.checks.schematic.component_value import ComponentValueCheck
from kicad_evaltor.checks.schematic.regulator_output import RegulatorOutputCheck
from kicad_evaltor.netlist import FileNetComponent
from kicad_evaltor.regulators import (
    RegulatorModel,
    clear_registered_regulators,
    fixed_voltage_from_description,
    fixed_voltage_from_name,
    register_regulator,
    regulator_model,
    regulator_output,
)
from kicad_evaltor.values import parse_value, values_match


class TestParseValue:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("4k7", 4700),
            ("4.7k", 4700),
            ("4700", 4700),
            ("4.7e3", 4700),
            ("10K", 10e3),
            ("2R2", 2.2),
            ("R47", 0.47),
            ("10R", 10),
            ("0R", 0),
            ("100n", 100e-9),
            ("100nF", 100e-9),
            ("100 nF", 100e-9),
            ("0.1u", 100e-9),
            ("0.1µF", 100e-9),
            ("2u2", 2.2e-6),
            ("22p", 22e-12),
            ("10uH", 10e-6),
            ("1M", 1e6),
            ("1m", 1e-3),
            ("1Meg", 1e6),
            ("3V3", 3.3),
            ("3.3V", 3.3),
            ("-5V", -5),
            ("100mA", 0.1),
            ("10 ohm", 10),
            ("10Ω", 10),
            ("10k 1%", 10e3),
            ("100n/50V", 100e-9),
        ],
    )
    def test_reads(self, text, expected):
        assert parse_value(text) == pytest.approx(expected, rel=1e-12, abs=1e-18)

    @pytest.mark.parametrize("text", [None, "", "DNP", "k", "4.7k7", "abc", "3VV"])
    def test_refuses(self, text):
        assert parse_value(text) is None

    def test_rkm_is_exact(self):
        assert parse_value("4k7") == 4700.0

    def test_values_match(self):
        assert values_match(3.35, 3.3, 0.03)
        assert not values_match(3.5, 3.3, 0.03)
        assert values_match(0.0, 0.0, 0.0)


class TestNumericComponentValue:
    def ctx(self, value):
        return FakeContext(schematic=FakeSchematic(symbols=[FakeSymbol("R1", value=value)]))

    def test_equal_numbers_in_different_spellings_match(self):
        assert (
            ComponentValueCheck(reference="R1", expected="4.7k", numeric=True)
            .run(self.ctx("4k7"))
            .is_pass
        )

    def test_tolerance(self):
        check = ComponentValueCheck(reference="R1", expected="10k", numeric=True, tolerance=0.05)

        assert check.run(self.ctx("10.2k")).is_pass
        assert check.run(self.ctx("11k")).is_fail

    def test_a_non_numeric_actual_fails(self):
        result = ComponentValueCheck(reference="R1", expected="10k", numeric=True).run(
            self.ctx("DNP")
        )

        assert result.is_fail
        assert "not a number" in result.message

    def test_a_non_numeric_expectation_is_rejected(self):
        with pytest.raises(ValueError, match="not a number"):
            ComponentValueCheck(reference="R1", expected="lots", numeric=True)


class TestModels:
    @pytest.fixture(autouse=True)
    def clean_registry(self):
        clear_registered_regulators()
        yield
        clear_registered_regulators()

    @pytest.mark.parametrize(
        ("name", "volts"),
        [
            ("AMS1117-3.3", 3.3),
            ("LP2951-3.3_SOIC", 3.3),
            ("L7805", 5.0),
            ("L78L33_TO92", 3.3),
            ("L7885", 8.5),
            ("L7912", -12.0),
            ("MC7905.2", -5.2),
            ("KA78M05_TO252", 5.0),
            ("AP1117-33", None),
            ("LT3015xQ-12", None),
            ("APE8865N-12-HF-3", None),
            ("LM317_TO-220", None),
        ],
    )
    def test_fixed_voltage_from_name(self, name, volts):
        assert fixed_voltage_from_name(name) == volts

    @pytest.mark.parametrize(
        ("description", "volts"),
        [
            ("Positive 500mA 20V Linear Regulator, Fixed Output 2.5V, TO-252", 2.5),
            ("0.3A Low Dropout regulator, positive, 3.3V fixed output, SOT-89", 3.3),
            ("150mA low dropout linear regulator, fixed 3.8V output, SOT-23-5", 3.8),
            ("Low-drop Voltage Regulator, Io up to 500mA, Fixed Vo 3.3V, TO-252", 3.3),
            ("150mA, 28V LDO Regulator With Shutdown, 1.8V Fixed Output, SOT-89-5", 1.8),
            ("Negative 1.5A Linear Regulator, Fixed Output 5V, TO-220", -5.0),
            ("1A 20V Fixed LDO Linear Regulator, 1.2V, SOT-89", None),
            ("Adjustable regulator", None),
        ],
    )
    def test_fixed_voltage_from_description(self, description, volts):
        assert fixed_voltage_from_description(description) == volts

    def test_table_first(self):
        model = regulator_model("Regulator_Linear:LM317_TO-220")

        assert model is not None
        assert (model.kind, model.vref) == ("divider", 1.25)
        assert "LM317" in model.source

    def test_negative_description_makes_a_named_voltage_negative(self):
        model = regulator_model(
            "Regulator_Linear:ADP7182AUJZ-3.3",
            description="Regulator, Negative, -3.3V Fixed Output",
        )

        assert model is not None
        assert model.vout == -3.3

    def test_name_and_description_that_disagree_give_no_model(self):
        # KiCad's L79L12 says "Fixed Output -5V"; neither can be trusted alone.
        assert (
            regulator_model(
                "Regulator_Linear:L79L12_TO92", description="Negative, Fixed Output -5V"
            )
            is None
        )

    def test_value_is_read_when_the_name_says_nothing(self):
        model = regulator_model("MyLib:LDO", value="AMS1117-3.3")

        assert model is not None
        assert model.vout == 3.3

    def test_registration_overrides_the_table(self):
        register_regulator("Regulator_Linear:LM317*", RegulatorModel("divider", vref=1.2))

        assert regulator_model("Regulator_Linear:LM317_TO-220").vref == 1.2

    def test_a_model_needs_its_parameter(self):
        with pytest.raises(ValueError, match="vref"):
            RegulatorModel("divider")


def ldo(top="10k", bottom="3k3", lib="Regulator_Linear:LM317_TO-220", extra=()):
    """VIN -> U1 -> /VOUT, with R1 from /VOUT to /ADJ and R2 from /ADJ to GND."""
    return netlist_of(
        ("+12V", [("U1", "3", "VI")]),
        ("/VOUT", [("U1", "2", "VO"), ("R1", "1"), ("C1", "1")]),
        ("/ADJ", [("U1", "1", "ADJ"), ("R1", "2"), ("R2", "1"), *extra]),
        ("GND", [("R2", "2"), ("C1", "2")]),
        components={
            "U1": FileNetComponent("U1", lib_id=lib),
            "R1": FileNetComponent("R1", value=top),
            "R2": FileNetComponent("R2", value=bottom),
            "C1": FileNetComponent("C1", value="10u"),
        },
    )


class TestRegulatorOutput:
    def test_divider(self):
        result = regulator_output(ldo(top="240", bottom="720"), "U1")

        # LM317: Vout = 1.25 * (1 + R2/R1), with R1 on top here: 1.25 * (1 + 240/720).
        assert result.volts == pytest.approx(1.25 * (1 + 240 / 720), rel=1e-5)
        assert result.parts == ("R1", "R2")
        assert "divider" in result.method

    def test_series_and_parallel_resistors_are_solved(self):
        netlist = netlist_of(
            ("/VOUT", [("U1", "2", "VO"), ("R1", "1"), ("R4", "1")]),
            ("/MID", [("R1", "2"), ("R3", "1")]),
            (
                "/ADJ",
                [
                    ("U1", "1", "ADJ"),
                    ("R3", "2"),
                    ("R2", "1"),
                    ("R5", "1"),
                    ("R4", "2"),
                    ("C2", "1"),
                ],
            ),
            ("GND", [("R2", "2"), ("R5", "2"), ("C2", "2")]),
            components={
                "U1": "Regulator_Linear:LM317_TO-220",
                "R1": FileNetComponent("R1", value="100"),
                "R3": FileNetComponent("R3", value="100"),
                "R4": FileNetComponent("R4", value="200"),
                "R2": FileNetComponent("R2", value="1k"),
                "R5": FileNetComponent("R5", value="1k"),
                "C2": FileNetComponent("C2", value="10n"),
            },
        )
        # Top: (100 + 100) || 200 = 100. Bottom: 1k || 1k = 500.
        assert regulator_output(netlist, "U1").volts == pytest.approx(1.25 * (1 + 100 / 500))

    def test_fixed(self):
        netlist = netlist_of(
            ("/VOUT", [("U2", "2", "VO")]),
            components={"U2": FileNetComponent("U2", lib_id="Regulator_Linear:AMS1117-3.3")},
        )
        result = regulator_output(netlist, "U2")

        assert result.volts == 3.3
        assert result.method.startswith("fixed")

    def test_current_reference(self):
        netlist = netlist_of(
            ("/VOUT", [("U3", "2", "OUT")]),
            ("/SET", [("U3", "3", "SET"), ("R1", "1")]),
            ("GND", [("R1", "2")]),
            components={
                "U3": "Regulator_Linear:LT3080xST",
                "R1": FileNetComponent("R1", value="330k"),
            },
        )

        assert regulator_output(netlist, "U3").volts == pytest.approx(10e-6 * 330e3)

    def test_passed_model_wins(self):
        result = regulator_output(ldo(), "U1", model=RegulatorModel("divider", vref=0.8))

        assert result.volts == pytest.approx(0.8 * (1 + 10e3 / 3.3e3))

    @pytest.mark.parametrize(
        ("netlist", "reason"),
        [
            (ldo(lib="MyLib:Mystery"), "no regulator model"),
            (ldo(top="DNP"), "cannot read R1"),
            (ldo(extra=[("U9", "4")]), "U9 on net /ADJ is not a resistor"),
            (netlist_of(("/X", [("R1", "1")])), "U1 is not in the netlist"),
        ],
    )
    def test_unknown_says_why(self, netlist, reason):
        result = regulator_output(netlist, "U1")

        assert result.volts is None
        assert not result.known
        assert reason in result.reason

    def test_a_divider_into_another_rail_is_refused(self):
        netlist = netlist_of(
            ("/VOUT", [("U1", "2", "VO"), ("R1", "1")]),
            ("/ADJ", [("U1", "1", "ADJ"), ("R1", "2"), ("R2", "1"), ("R9", "1")]),
            ("GND", [("R2", "2")]),
            ("+5V", [("R9", "2")]),
            components={
                "U1": "Regulator_Linear:LM317_TO-220",
                "R1": FileNetComponent("R1", value="1k"),
                "R2": FileNetComponent("R2", value="1k"),
                "R9": FileNetComponent("R9", value="1k"),
            },
        )

        assert "rail +5V" in regulator_output(netlist, "U1").reason


class TestRegulatorOutputCheck:
    def ctx(self, netlist):
        return FakeContext(schematic=FakeSchematic(netlist=netlist))

    def test_pass_within_tolerance(self):
        # 1.25 * (1 + 390/240) = 3.28 V, within 3 % of 3.3 V.
        result = RegulatorOutputCheck(reference="U1", expected=3.3).run(
            self.ctx(ldo(top="390", bottom="240"))
        )

        assert result.is_pass, result.message
        assert result.details["parts"] == ["R1", "R2"]

    def test_fail_outside_tolerance(self):
        result = RegulatorOutputCheck(reference="U1", expected=5.0).run(
            self.ctx(ldo(top="390", bottom="240"))
        )

        assert result.is_fail
        assert "expected 5 V" in result.message

    def test_unknown_is_an_error_not_a_fail(self):
        result = RegulatorOutputCheck(reference="U1", expected=3.3).run(
            self.ctx(ldo(lib="MyLib:Mystery"))
        )

        assert result.status == TestStatus.ERROR
        assert "no regulator model" in result.message

    def test_rejects_a_bad_tolerance(self):
        with pytest.raises(ValueError):
            RegulatorOutputCheck(reference="U1", expected=3.3, tolerance=2)
