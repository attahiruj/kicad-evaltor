"""Netlist helpers added for 0.6.1, tested on real KiCad designs.

The netlists under ``tests/fixtures/boards/<case>/netlist_<case>.xml`` were
exported with ``kicad-cli sch export netlist --format kicadxml`` from the imu
design (an ESP32-C6 data logger: BQ24074 charger, TPS63001, LSM6DSL, microSD,
USB-C, JST connectors) and from the two bundled demos. Where a test needs a
fact none of those designs has (a DNP part, a renamed pin in a copied symbol,
a sheet-local net beside a global one, another agent's part description), it
changes one field of the real parsed netlist and says so.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kicad_evaltor.authoring import CircuitQueries
from kicad_evaltor.circuits import DCNetwork, NetworkUnavailable
from kicad_evaltor.connectivity import Connectivity, any_of, never, power_rails, series_parts
from kicad_evaltor.netlist import AmbiguousNet, FileNet, FileNetlist, parse_netlist
from kicad_evaltor.parts import (
    PartNotFound,
    fields_matching,
    find_part,
    led_colors,
    pins_named,
    signal_pins,
    stock_symbols_for,
    text_of,
    wavelength_color,
)
from kicad_evaltor.pin_roles import match_pin_name, normalise_pin_name, pin_name_alternatives
from kicad_evaltor.symbol_library import SymbolLibraries, find_symbol_dirs
from kicad_evaltor.values import quantity_of

BOARDS = Path(__file__).resolve().parent / "fixtures" / "boards"
ROOT = Path(__file__).resolve().parent.parent

needs_stock = pytest.mark.skipif(
    not find_symbol_dirs(), reason="KiCad's stock symbol libraries are not installed"
)


def load(case: str) -> FileNetlist:
    path = BOARDS / case / f"netlist_{case}.xml"
    return parse_netlist(path.read_text(encoding="utf-8"), "kicadxml")


@pytest.fixture(scope="module")
def imu() -> FileNetlist:
    """Global rails (``GND``, ``+3V3``), sheet-local signals (``/USB_D+``), a
    100 ohm series resistor to an LED and to a regulator's EN, 10k pull-ups."""
    return load("imu")


@pytest.fixture(scope="module")
def hierarchy() -> FileNetlist:
    """The bundled hierarchical demo: nets on sheet ``/shared/``, an STM32 with
    multi-function pins and a TP4056 with an ``EPAD`` pin."""
    return load("demo_hierarchy")


@pytest.fixture(scope="module")
def esp_demo() -> FileNetlist:
    """The bundled ESP32 demo, whose TPS63020 comes from a project library."""
    return load("demo_esp")


@pytest.fixture(scope="module")
def stock() -> SymbolLibraries:
    return SymbolLibraries()


# ------------------------------------------------------------------ net_named


class TestNetNamed:
    def test_finds_a_net_with_or_without_a_sheet_prefix(self, imu):
        assert imu.net_named("USB_D+").name == "/USB_D+"
        assert imu.net_named("/USB_D+").name == "/USB_D+"
        assert imu.net_named("SD_CS").name == "/SD_CS"
        assert imu.net_named("GND").name == "GND"
        assert imu.net_named("/GND").name == "GND"

    def test_finds_a_net_on_a_sub_sheet(self, hierarchy):
        assert hierarchy.net_named("CC1").name == "/shared/CC1"
        assert hierarchy.net_named("PA8/PB0/PB1/PB2").name == "/shared/PA8/PB0/PB1/PB2"

    def test_a_label_containing_a_slash_is_not_a_sheet(self, hierarchy):
        # "/shared/PA8/PB0/PB1/" is not a sheet path any component sits on.
        assert hierarchy.net_named("PB2") is None

    def test_missing_net_is_none_and_any_sheet_off_is_exact(self, imu):
        assert imu.net_named("NO_SUCH_NET") is None
        assert imu.net_named("USB_D+", any_sheet=False) is None
        assert imu.net_named("/USB_D+", any_sheet=False).name == "/USB_D+"

    def test_a_sheet_local_net_beside_the_global_one_is_ambiguous(self, hierarchy):
        # The demo has one ground; add a sheet-local "/shared/GND" beside it.
        nets = [*hierarchy.nets, FileNet("/shared/GND", [])]
        both = FileNetlist(nets, hierarchy.components)
        with pytest.raises(AmbiguousNet) as raised:
            both.net_named("GND")
        assert raised.value.candidates == ("/shared/GND", "GND")
        assert "/shared/GND" in str(raised.value)
        assert both.net_named("GND", any_sheet=False).name == "GND"


# ------------------------------------------------------- value-bounded bridge


class TestValueBoundedBridge:
    def test_series_resistor_conducts_and_pull_up_does_not(self, imu):
        bounded = series_parts("R", max_ohms=1000)
        led = Connectivity(imu).reach("Net-(U4-IO2)", through=bounded, stop_at=never)
        assert led.nets == {"Net-(U4-IO2)", "Net-(D3-A)"}
        assert led.crossed == ["R14"]

        sd = Connectivity(imu).reach("/SD_CS", through=bounded, stop_at=never)
        assert sd.nets == {"/SD_CS"}
        unbounded = Connectivity(imu).reach("/SD_CS", through=series_parts("R"), stop_at=never)
        assert "+3V3" in unbounded.nets

    def test_unreadable_value_does_not_conduct(self, imu):
        bridge = series_parts("L", max_ohms=1000)
        inductor = imu.components["L1"]  # Value "2.2uH": not a resistance
        assert bridge(inductor, imu.pins_of("L1")) == []
        assert series_parts("L")(inductor, imu.pins_of("L1")) != []

    def test_bound_is_inclusive(self, imu):
        exactly = series_parts("R", max_ohms=100)  # R14 is "100R"
        assert exactly(imu.components["R14"], imu.pins_of("R14")) != []
        assert series_parts("R", max_ohms=99.9)(imu.components["R14"], imu.pins_of("R14")) == []

    def test_negative_bound_is_refused(self):
        with pytest.raises(ValueError):
            series_parts("R", max_ohms=-1)


# --------------------------------------------------------------------- merged


class TestMerged:
    def test_joins_nets_through_bridged_parts_and_maps_raw_names(self, imu):
        merged = imu.merged(series_parts("R", max_ohms=100))

        assert merged.raw_names["Net-(D3-A)"] == ("Net-(D3-A)", "Net-(U4-IO2)")
        assert merged.merged_name["Net-(U4-IO2)"] == "Net-(D3-A)"
        assert merged.joined_by["Net-(D3-A)"] == ("R14",)
        # R11 (100 ohm) ties the TPS63001's EN to VSYS.
        assert merged.raw_names["/VSYS"] == ("/VSYS", "Net-(U3-EN)")
        assert merged.joined_by["/VSYS"] == ("R11",)
        assert merged.net_of("U4", "5") == "Net-(D3-A)"
        assert {n.ref for n in merged.nodes_on("Net-(D3-A)")} == {"U4", "R14", "D3"}
        assert merged.net("Net-(U4-IO2)") is None
        assert len(merged.nets) == len(imu.nets) - 2

    def test_original_is_untouched(self, imu):
        imu.merged(series_parts("R", max_ohms=100))
        assert imu.net_of("U4", "5") == "Net-(U4-IO2)"

    def test_dnp_part_does_not_merge(self, imu):
        # The design marks no part DNP; mark R14 in a copy.
        components = dict(imu.components)
        components["R14"] = dataclasses.replace(components["R14"], dnp=True)
        merged = FileNetlist(imu.nets, components).merged(series_parts("R", max_ohms=100))
        assert merged.raw_names["Net-(D3-A)"] == ("Net-(D3-A)",)
        assert merged.raw_names["/VSYS"] == ("/VSYS", "Net-(U3-EN)")
        assert "Net-(D3-A)" not in merged.joined_by

    def test_zero_ohm_and_ferrite_bridge_merges_nothing_on_a_board_without_them(self, imu):
        merged = imu.merged(any_of(series_parts("R", max_ohms=0), series_parts("FB")))
        assert all(len(raw) == 1 for raw in merged.raw_names.values())
        assert merged.joined_by == {}


# --------------------------------------------------------- resistance & strap


def without_adc(netlist: FileNetlist) -> FileNetlist:
    """The design with the MCU's ADC pin (U4.12) taken off the battery divider's tap.

    The tap then joins only R9 and R10: a two-part series chain from +BATT to
    GND that the chain search may walk through.
    """
    nets = [
        FileNet(net.name, [n for n in net.nodes if (n.ref, n.pin) != ("U4", "12")], net.code)
        for net in netlist.nets
    ]
    return FileNetlist(nets, netlist.components)


class TestDCNetworkStops:
    def test_pull_ups_do_not_link_signals_through_the_rail(self, imu):
        refs = [r for r in imu.components if r.startswith("R")]
        free = DCNetwork.from_netlist(imu, refs)
        stopped = DCNetwork.from_netlist(imu, refs, stop_at=power_rails)

        # Two 10k pull-ups to +3V3: 20k apart when the rail is walked through.
        assert free.equivalent_resistance("/SD_CS", "/SD_MOSI") == pytest.approx(20e3)
        with pytest.raises(NetworkUnavailable):
            stopped.equivalent_resistance("/SD_CS", "/SD_MOSI")
        assert stopped.equivalent_resistance("/SD_CS", "+3V3") == pytest.approx(10e3)
        assert {"+3V3", "GND"} <= stopped.stops

    def test_component_stops_at_rails(self, imu):
        refs = [r for r in imu.components if r.startswith("R")]
        stopped = DCNetwork.from_netlist(imu, refs, stop_at=power_rails)
        assert {r.reference for r in stopped.component("/SD_CS").resistors} == {"R18"}
        walked = stopped.component("/SD_CS", stop_at=())
        assert {"R18", "R19", "R12"} <= {r.reference for r in walked.resistors}

    def test_series_chain_to_ground(self, imu):
        # The battery divider: 100k + 100k from +BATT through the tap to GND.
        network = DCNetwork.from_netlist(imu, ["R9", "R10"], stop_at=power_rails)
        assert network.equivalent_resistance("+BATT", "GND") == pytest.approx(200e3)
        assert network.equivalent_resistance("/VBAT_SENSE", "GND") == pytest.approx(100e3)


class TestResistanceBetween:
    def test_two_part_series_chain(self, imu):
        found = CircuitQueries(without_adc(imu)).resistance_between("+BATT", "GND")
        assert found.ohms == pytest.approx(200e3)
        assert found.b == "GND"
        (chain,) = found.chains
        assert chain.references == ("R9", "R10")
        assert chain.nets == ("+BATT", "/VBAT_SENSE", "GND")

    def test_a_joint_with_a_third_pin_is_not_a_chain(self, imu):
        # With the ADC pin on the tap, R9 and R10 are not in series.
        assert CircuitQueries(imu).resistance_between("+BATT", "GND") is None

    def test_max_parts_limits_the_chain(self, imu):
        queries = CircuitQueries(without_adc(imu))
        assert queries.resistance_between("+BATT", "GND", max_parts=1) is None
        assert queries.resistance_between("/VBAT_SENSE", "GND", max_parts=1).ohms == 100e3

    def test_by_pin_and_direct(self, imu):
        queries = CircuitQueries(imu)
        ilim = queries.resistance_between(("U2", "12"), "GND")
        assert ilim.ohms == 1240.0
        assert ilim.chains[0].references == ("R4",)

    def test_signals_are_not_joined_through_a_rail(self, imu):
        assert CircuitQueries(imu).resistance_between("/SD_CS", "/SD_MOSI") is None

    def test_same_net_is_zero(self, imu):
        found = CircuitQueries(imu).resistance_between(("U2", "4"), "GND")  # ~CE tied low
        assert found.ohms == 0.0
        assert found.chains == ()

    def test_unknown_net_raises(self, imu):
        with pytest.raises(NetworkUnavailable):
            CircuitQueries(imu).resistance_between("NO_SUCH", "GND")


class TestStrapped:
    def test_pull_up_names_the_rail(self, imu):
        strap = CircuitQueries(imu).strapped(("U4", "8"), ["GND", "+3V3"], 100e3)  # EN
        assert strap.rail == "+3V3"
        assert strap.ohms == 10e3
        assert strap.chains[0].references == ("R12",)
        assert strap.alternatives == ()

    def test_tied_directly(self, imu):
        strap = CircuitQueries(imu).strapped(("U5", "1"), ["GND", "+3V3"], 0)  # SA0
        assert (strap.rail, strap.ohms) == ("GND", 0.0)

    def test_bound_and_missing_rail(self, imu):
        queries = CircuitQueries(imu)
        assert queries.strapped(("U4", "8"), ["GND", "+3V3"], 4.7e3) is None
        assert queries.strapped(("U4", "8"), ["NO_RAIL", "+3V3"], 100e3).rail == "+3V3"
        assert queries.strapped("/IMU_INT1", ["GND", "+3V3"], 1e6) is None


# ----------------------------------------------------------------- quantity_of


# Descriptions usb_pad_route_b, another agent's design of the same logger,
# gave its parts; this design keeps KiCad's generic ones.
B_C1 = "Ceramic capacitor 1uF 25V X5R 0402"
B_L1 = "2.2uH 2.2A power inductor, 4x4mm (NRS4018T2R2MDGJ)"
B_D1 = "Red LED 0603 (charging)"


class TestQuantityOf:
    def test_value_first(self, imu):
        found = quantity_of(imu.components["R4"], "resistance")
        assert (found.value, found.field) == (1240.0, "Value")

    def test_description_when_value_is_not_the_quantity(self, imu):
        # C1's Value is 1uF; with usb_pad_route_b's description its rating
        # is only in the Description.
        c1 = dataclasses.replace(imu.components["C1"], description=B_C1)
        found = quantity_of(c1, "voltage")
        assert found.field == "Description"
        assert found.value == 25.0
        assert "25V" in found.text
        assert not quantity_of(imu.components["C1"], "voltage").known

    def test_rating_and_tolerance_text(self, imu):
        r1 = dataclasses.replace(imu.components["R1"], value="5.1k ±1%")
        assert quantity_of(r1, "resistance").value == 5100.0
        r1 = dataclasses.replace(imu.components["R1"], value="5.1k +/-1% 0402")
        assert quantity_of(r1, "resistance").value == 5100.0
        c1 = dataclasses.replace(imu.components["C1"], value="10uF 16V X5R")
        assert quantity_of(c1, "capacitance").value == pytest.approx(10e-6)

    def test_part_number_value_falls_back_to_description(self, imu):
        inductor = dataclasses.replace(
            imu.components["L1"], value="NRS4018T2R2MDGJ", description=B_L1
        )
        found = quantity_of(inductor, "inductance")
        assert found.field == "Description"
        assert found.value == pytest.approx(2.2e-6)

    def test_unitless_numbers_in_free_text_are_not_read(self, imu):
        # usb_pad_route_b's hole Value. This design's, "M3", reads as the
        # resistance code for 0.3 Mohm.
        hole = dataclasses.replace(imu.components["H1"], value="MountingHole_M2")
        found = quantity_of(hole, "resistance")
        assert not found.known
        assert found.field == ""
        assert "Value" in found.quantity.reason


# --------------------------------------------------------------- text search


class TestFieldsMatching:
    def test_reports_which_fields_matched(self, imu):
        found = fields_matching(imu.components["J2"], r"JST[-_ ]?PH")
        assert [m.field for m in found] == ["footprint"]
        assert found[0].match == "JST_PH"
        # silk_text_edge's J3 Value; this design's is "UART".
        j3 = dataclasses.replace(imu.components["J3"], value="UART0 / JST-PH")
        found = fields_matching(j3, r"JST[-_ ]?PH")
        assert [m.field for m in found] == ["footprint", "Value"]
        assert found[1].match == "JST-PH"

    def test_searches_custom_fields_and_keywords(self, imu):
        assert [m.field for m in fields_matching(imu.components["L1"], "C167869")] == ["LCSC"]
        assert "ki_keywords" in text_of(imu.components["J1"])
        assert fields_matching(imu.components["R1"], "nothing-like-this") == ()


class TestLedColors:
    def test_value_and_description(self, imu):
        assert [(c.color, c.field) for c in led_colors(imu.components["D3"])] == [("blue", "Value")]
        colors = led_colors(dataclasses.replace(imu.components["D1"], description=B_D1))
        assert {c.color for c in colors} == {"red"}
        assert {c.field for c in colors} == {"Value", "Description"}

    def test_wavelength_and_caller_codes(self, imu):
        d3 = dataclasses.replace(
            imu.components["D3"], value="XL-1608UBC", description="LED 470nm 0603"
        )
        assert [(c.color, c.evidence) for c in led_colors(d3)] == [("blue", "470nm")]
        d1 = dataclasses.replace(imu.components["D1"], value="KT-0603R", description="")
        assert led_colors(d1) == ()
        assert [c.color for c in led_colors(d1, extra={r"KT-0603R\b": "red"})] == ["red"]

    def test_bands(self):
        assert [wavelength_color(n) for n in (365, 405, 470, 525, 585, 605, 630, 940)] == [
            "ultraviolet",
            "violet",
            "blue",
            "green",
            "yellow",
            "orange",
            "red",
            "infrared",
        ]

    def test_a_non_led_has_none(self, imu):
        assert led_colors(imu.components["R1"]) == ()


# ------------------------------------------------------------------------ pins


class TestPinSpellings:
    @pytest.mark.parametrize("spelling", ["~{CHG}", "/CHG", "CHG", "nCHG", "CHG_N", "chg"])
    def test_active_low_spellings_normalise(self, spelling):
        assert normalise_pin_name(spelling) == "CHG"

    def test_alternates(self):
        assert pin_name_alternatives("GPIO16/U0TXD") == ("GPIO16", "U0TXD")
        assert pin_name_alternatives("DAT3_/_CS") == ("DAT3", "CS")
        assert pin_name_alternatives("/CHG") == ()
        assert match_pin_name("DAT3/CD", "CD") == "alternate"
        assert match_pin_name("V_{CC}", "VCC") == "normalised"
        assert match_pin_name("CHG", "PGOOD") is None


class TestPinsNamed:
    def test_overbar_pin(self, imu):
        (found,) = pins_named(imu, "U2", "CHG", stock=False)
        assert (found.pin, found.how, found.spelling) == ("9", "normalised", "~{CHG}")
        assert found.net == "Net-(D1-K)"

    def test_exact_beats_normalised_and_keeps_duplicates(self, imu):
        found = pins_named(imu, "U2", "VSS", stock=False)
        assert [(p.pin, p.how) for p in found] == [("8", "exact"), ("17", "exact")]

    def test_alternate_function(self, hierarchy, imu):
        (pb0,) = pins_named(hierarchy, "U2", "PB0", stock=False)
        assert (pb0.pin, pb0.how) == ("15", "alternate")
        (cs,) = pins_named(imu, "J4", "CD", stock=False)
        assert cs.pin == "2"

    def test_subscript_markup(self, hierarchy):
        (vcc,) = pins_named(hierarchy, "U3", "VCC", stock=False)
        assert (vcc.pin, vcc.spelling) == ("4", "V_{CC}")

    def test_nothing_found(self, imu):
        assert pins_named(imu, "U2", "STAT1", stock=False) == ()
        assert pins_named(imu, "NOPE", "CHG", stock=False) == ()

    @needs_stock
    def test_renamed_pin_found_through_the_stock_symbol(self, imu, stock):
        # A copied symbol whose CHG pin was renamed: Logger:BQ24074RGT, pin 9 "STAT1".
        nets = [
            FileNet(
                net.name,
                [
                    dataclasses.replace(n, pinfunction="STAT1")
                    if (n.ref, n.pin) == ("U2", "9")
                    else n
                    for n in net.nodes
                ],
                net.code,
            )
            for net in imu.nets
        ]
        components = dict(imu.components)
        components["U2"] = dataclasses.replace(components["U2"], lib_id="Logger:BQ24074RGT")
        copied = FileNetlist(nets, components)

        (found,) = pins_named(copied, "U2", "CHG", libraries=stock)
        assert found.pin == "9"
        assert found.how == "stock_symbol"
        assert found.spelling == "~{CHG}"
        assert found.source == "Battery_Management:BQ24074RGT"
        assert pins_named(copied, "U2", "CHG", stock=False) == ()


@needs_stock
class TestStockSymbolsFor:
    def test_stock_lib_id(self, imu, stock):
        assert stock_symbols_for(imu.components["U2"], stock) == ("Battery_Management:BQ24074RGT",)

    def test_copied_symbol_and_value_stem(self, imu, stock):
        copied = dataclasses.replace(imu.components["U2"], lib_id="Logger:BQ24074RGT")
        assert stock_symbols_for(copied, stock) == ("Battery_Management:BQ24074RGT",)
        drawn = dataclasses.replace(
            imu.components["U2"], lib_id="Logger:charger", value="BQ24074RGTR"
        )
        assert stock_symbols_for(drawn, stock) == ("Battery_Management:BQ24074RGT",)

    def test_project_symbol_without_a_stock_twin(self, esp_demo, stock):
        assert esp_demo.components["U2"].lib_id == "lora-remote-symbols:TPS63020"
        assert stock_symbols_for(esp_demo.components["U2"], stock) == ()

    def test_index_finds_copies_by_name(self, stock):
        assert stock.lib_ids_named("BQ24074RGT") == ("Battery_Management:BQ24074RGT",)
        assert "Connector_Generic_MountingPin" in stock.nicknames()


class TestSignalPins:
    def test_usb_c_shield_is_dropped(self, imu):
        found = signal_pins(imu, "J1")
        assert [p.pin for p, _ in found.dropped] == ["SH"]
        assert len(found.pins) == 16
        assert "GND" in found.nets

    def test_exposed_pad_named_epad_is_dropped(self, hierarchy):
        found = signal_pins(hierarchy, "U3")
        assert [(p.pin, p.pinfunction) for p, _ in found.dropped] == [("9", "EPAD")]
        assert "EPAD" in found.dropped[0][1]
        assert len(found.pins) == 8

    def test_thermal_pad_named_for_its_net_stays(self, imu):
        found = signal_pins(imu, "U2")
        assert found.dropped == ()
        assert "17" in [p.pin for p in found.pins]

    def test_two_pin_connector_unchanged(self, imu):
        assert [p.pin for p in signal_pins(imu, "J2").pins] == ["1", "2"]


# ------------------------------------------------------------------- find_part


class TestFindPart:
    def test_by_lib_id_stem(self, imu):
        assert find_part(imu, "BQ24074").ref == "U2"

    def test_by_value_stem(self, imu):
        found = find_part(imu, "USBLC6-2")
        assert found.ref == "U1"
        assert any("Value" in reason for reason in found.matched_by)

    def test_by_marker_with_required_pins(self, imu):
        found = find_part(
            imu, "", markers=("TYPE-C", "USB_C"), required_pins=("CC1", "CC2", "D+", "D-")
        )
        assert found.ref == "J1"
        assert [p.pin for p in found.pins["D+"]] == ["A6", "B6"]

    def test_microsd_needs_its_contacts(self, imu):
        contacts = ("DAT2", "CD", "CMD", "VDD", "CLK", "VSS", "DAT0", "DAT1")
        found = find_part(imu, "", markers=("DM3AT",), required_pins=contacts, stock=False)
        assert found.ref == "J4"

    def test_reference_letter_is_ignored(self, imu):
        # The ESD array is U1 here and U5 on another agent's design; found the
        # same way.
        assert find_part(imu, "USBLC6").ref == "U1"

    def test_several_matches_raise_with_candidates(self, imu):
        with pytest.raises(PartNotFound) as raised:
            find_part(imu, "", markers=("JST",))
        assert [c.ref for c in raised.value.candidates] == ["J2", "J3"]
        assert "2 candidates" in str(raised.value)

    def test_rejections_are_explained(self, imu):
        with pytest.raises(PartNotFound) as raised:
            find_part(imu, "LSM6DSL", required_pins=("CHG",), stock=False)
        (candidate,) = raised.value.candidates
        assert candidate.ref == "U5"
        assert candidate.rejected == "no pin named CHG"

    def test_dnp_is_skipped_when_fitted(self, imu):
        components = dict(imu.components)
        components["U2"] = dataclasses.replace(components["U2"], dnp=True)
        unfitted = FileNetlist(imu.nets, components)
        with pytest.raises(PartNotFound) as raised:
            find_part(unfitted, "BQ24074")
        assert raised.value.candidates[0].rejected == "marked DNP"
        assert find_part(unfitted, "BQ24074", fitted=False).ref == "U2"

    def test_nothing_matches(self, imu):
        with pytest.raises(PartNotFound, match="no component matches"):
            find_part(imu, "STM32")


# ------------------------------------------------------------- recorded exports


@pytest.mark.integration
@pytest.mark.parametrize("case", ["imu"])
def test_recorded_netlist_matches_a_fresh_export(kicad_cli_required, case):
    from kicad_evaltor.schematic_file import FileSchematic

    schematic = BOARDS / case / f"{case}.kicad_sch"
    if not schematic.exists():
        pytest.skip(f"board project {case} not present")
    fresh = FileSchematic(schematic, cli_path=str(kicad_cli_required)).get_netlist()
    recorded = load(case)
    assert {n.name: {(x.ref, x.pin) for x in n.nodes} for n in fresh.nets} == {
        n.name: {(x.ref, x.pin) for x in n.nodes} for n in recorded.nets
    }
    assert fresh.components == recorded.components
