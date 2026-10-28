"""Online sourcing: exact catalogue lookups, record matching and the check.

Offline tests replay JLCPCB component-search replies recorded verbatim from
the public API (``tests/fixtures/sourcing/jlcpcb``, named
``<keyword>_p<page>_s<page size>.json``) for LCSC numbers the real imu design
in ``tests/fixtures/boards/imu`` carries, a few alternatives, and one number
that does not exist. Regressions mutate the real design's LCSC fields; no reply
is made up.

Digi-Key and Mouser publish request/response schemas but no full example
payload, so their tests cover request construction and error handling only.

The live tests are marked ``network`` and run only with
``KICAD_EVALTOR_LIVE_SOURCING=1`` (plus credentials for Digi-Key and Mouser).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from kicad_evaltor.checks.base import TestStatus as Status
from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.deliverables import BOMRow, read_bom
from kicad_evaltor.sourcing import (
    DigiKeyProvider,
    HttpResponse,
    JlcpcbProvider,
    MouserProvider,
    PartRecord,
    PartRule,
    SourcingCheck,
    SourcingLookupError,
    SourcingProvider,
    catalogue_mismatch,
    part_identity,
    same_part,
)

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "sourcing" / "jlcpcb"
IMU = Path(__file__).resolve().parent / "fixtures" / "boards" / "imu"

# Caller policy for the IMU data logger boards: which listings each part needs.
RULES = [
    PartRule(value="*BQ24074*", mpn_stem="BQ24074"),
    PartRule(value="*USBLC6*", mpn_stem="USBLC6-2"),
    PartRule(value="*TPS63001*", mpn_stem="TPS63001"),
    PartRule(value="*LSM6DSL*", mpn_stem="LSM6DSL"),
    PartRule(value="*ESP32-C6-MINI-1*", mpn_stem="ESP32-C6-MINI-1"),
    PartRule(lib_id="*DM3AT*", mpn_stem="DM3AT"),
    PartRule(lib_id="Connector:USB_C*", category=("USB",)),
    PartRule(lib_id="Device:LED*", category=("LED",), colour=True),
    PartRule(reference="R*", lib_id="Device:R*", category=("Resistor",), dimension="resistance"),
    PartRule(reference="C*", lib_id="Device:C*", category=("Capacitor",), dimension="capacitance"),
    PartRule(reference="L*", lib_id="Device:L*", category=("Inductor",), dimension="inductance"),
    PartRule(reference="SW*", category=("Switch",)),
]
# Mounting holes carry no part; the JST connectors are hand-soldered.
EXCLUDE = ["MountingHole*", "Connector_JST:*"]


def no_sleep(_seconds: float) -> None:
    pass


class Replay:
    """A transport that answers JLCPCB searches from the recordings."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.requests.append(body)
        name = f"{body['keyword']}_p{body['currentPage']}_s{body['pageSize']}.json"
        return HttpResponse(200, (RECORDINGS / name).read_bytes())


def lcsc(**kwargs) -> JlcpcbProvider:
    kwargs.setdefault("transport", Replay())
    kwargs.setdefault("sleep", no_sleep)
    return JlcpcbProvider(**kwargs)


def board() -> DesignContext:
    path = IMU / "imu.kicad_sch"
    if not path.exists():
        pytest.skip("the real imu design is not in tests/fixtures/boards")
    return DesignContext(schematic_path=path)


def symbol(ctx: DesignContext, reference: str):
    return next(s for s in ctx.sheet_symbols() if s.reference == reference)


def with_fields(ctx: DesignContext, changes: dict[str, dict[str, str]]) -> DesignContext:
    """The board with some symbols' fields changed, as a designer might."""
    symbols = []
    for original in ctx.sheet_symbols():
        changed = copy.copy(original)
        if original.reference in changes:
            changed.properties = {**original.properties, **changes[original.reference]}
            changed.user_fields = {**original.user_fields, **changes[original.reference]}
        symbols.append(changed)
    ctx.sheet_symbols = lambda: symbols  # type: ignore[method-assign]
    return ctx


def run(ctx: DesignContext, providers=None, **params):
    params.setdefault("rules", RULES)
    params.setdefault("exclude", EXCLUDE)
    check = SourcingCheck(providers=providers or {"lcsc": lcsc()}, **params)
    return check.run(ctx)


# --------------------------------------------------------------------------
# JLCPCB provider against recorded replies


def test_exact_lookup_normalises_a_real_resistor_record():
    record = lcsc().lookup("c25744")
    assert record is not None
    assert (record.number, record.platform, record.mpn) == ("C25744", "lcsc", "0402WGF1002TCE")
    assert record.manufacturer == "UNI-ROYAL(Uniroyal Elec)"
    assert record.category == "Chip Resistor - Surface Mount"
    assert record.package == "0402"
    assert record.attributes["Resistance"] == "10kΩ"
    assert record.url
    assert record.url.endswith("_C25744.html")
    assert record.part_type == "resistor"
    assert record.inferred["type"].evidence == {"category": "Chip Resistor - Surface Mount"}
    assert record.chip_size == "0402"
    assert record.inferred["chip_size"].evidence == {"package": "0402"}


def test_led_colour_comes_from_illumination_colour_not_lens_colour():
    record = lcsc().lookup("C2286")
    assert record is not None
    assert record.attributes["Lens Color"] == "Water Clear"
    assert record.led_colours == ("red",)
    assert record.inferred["led_colours"].evidence == {"Illumination Color": "Red"}
    # "Emerald Green" is green.
    green = lcsc().lookup("C2297")
    assert green is not None
    assert green.led_colours == ("green",)
    assert green.chip_size == "0805"


def test_chip_size_is_only_read_from_package_metadata():
    ic = lcsc().lookup("C54313")  # BQ24074RGTR, QFN-16-EP(3x3)
    assert ic is not None
    assert ic.part_type == "ic"
    assert ic.chip_size is None
    inductor = lcsc().lookup("C83423")  # package "SMD,4x4mm"
    assert inductor is not None
    assert inductor.part_type == "inductor"
    assert inductor.chip_size is None


def test_keyword_hits_that_are_not_the_number_are_never_taken():
    # The search for C15850 also returns C9900163687, whose MPN is "C15850".
    replay = Replay()
    record = lcsc(transport=replay).lookup("C15850")
    assert record is not None
    assert record.number == "C15850"
    assert record.mpn == "CL21A106KAYNNNE"
    # C999999999 returns 41 near misses and no exact match: absent, not a hit.
    assert lcsc().lookup("C999999999") is None


def test_lookup_pages_until_every_result_has_been_seen():
    replay = Replay()
    assert lcsc(transport=replay, page_size=20).lookup("C999999999") is None
    assert [r["currentPage"] for r in replay.requests] == [1, 2, 3]

    replay = Replay()
    record = lcsc(transport=replay, page_size=10).lookup("C2286")
    assert record is not None
    assert record.mpn == "KT-0603R"
    assert [r["currentPage"] for r in replay.requests] == [1]


def test_too_many_pages_without_a_match_is_an_incomplete_answer():
    with pytest.raises(SourcingLookupError, match="incomplete") as caught:
        lcsc(page_size=20, max_pages=2, attempts=1).lookup("C999999999")
    assert caught.value.platform == "lcsc"
    assert isinstance(caught.value, LookupError)


def test_malformed_numbers_are_rejected_before_any_request():
    replay = Replay()
    provider = lcsc(transport=replay)
    for bad in ("", "25744", "C0", "C12a", "LCSC"):
        with pytest.raises(ValueError):
            provider.lookup(bad)
    assert replay.requests == []


def test_transport_failure_is_a_lookup_error_after_retries():
    calls = []
    sleeps: list[float] = []

    def down(request, timeout):
        calls.append(request)
        raise OSError("connection refused")

    provider = JlcpcbProvider(transport=down, attempts=3, backoff=0.5, sleep=sleeps.append)
    with pytest.raises(SourcingLookupError, match="lcsc: request failed"):
        provider.lookup("C25744")
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_a_retry_recovers_from_one_failed_request():
    replay = Replay()
    failures = [OSError("reset")]

    def flaky(request, timeout):
        if failures:
            raise failures.pop()
        return replay(request, timeout)

    record = JlcpcbProvider(transport=flaky, sleep=no_sleep).lookup("C25744")
    assert record is not None
    assert record.number == "C25744"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (503, b"Service Unavailable", "HTTP 503"),
        (200, b"<html>maintenance</html>", "not JSON"),
        (200, (RECORDINGS / "C25744_p1_s100.json").read_bytes()[:500], "not JSON"),
        (200, b'{"code": 200, "data": {}}', "malformed reply"),
        (200, b'{"code": 500, "message": "busy"}', "rejected"),
    ],
)
def test_bad_replies_are_lookup_errors_not_missing_parts(status, body, reason):
    def reply(_request, _timeout):
        return HttpResponse(status, body)

    provider = JlcpcbProvider(transport=reply, attempts=1)
    with pytest.raises(SourcingLookupError, match=reason):
        provider.lookup("C25744")


# --------------------------------------------------------------------------
# Matching records to symbols, on the real board


def test_every_real_record_matches_its_symbol_on_the_board():
    ctx = board()
    provider = lcsc()
    for reference in ("R4", "C2", "D1", "D3", "U2", "J1", "SW1", "L1", "U4", "J4"):
        part = symbol(ctx, reference)
        record = provider.lookup(part.properties["LCSC"])
        assert record is not None
        assert catalogue_mismatch(part, record, RULES) is None, reference


def test_mismatch_reasons_for_wrong_value_package_ic_and_colour():
    ctx = board()
    provider = lcsc()

    def why(reference: str, number: str) -> str | None:
        record = provider.lookup(number)
        assert record is not None
        return catalogue_mismatch(symbol(ctx, reference), record, RULES)

    assert why("R4", "C25744") == "is 10kΩ, not 1.24k"  # wrong value
    assert why("R6", "C25804") is not None  # 10k, but 0603 on an 0402 footprint
    assert "0603" in why("R6", "C25804")
    assert "0402" in why("R6", "C25804")
    assert why("U2", "C15999") == "is USBLC6-2P6, not a BQ24074"  # wrong IC
    assert why("D1", "C2288") == "emits blue, not red"  # wrong colour
    assert why("D1", "C25744") == (
        "is a Chip Resistor - Surface Mount, not a LED part"
    )  # not an LED
    assert why("J1", "C318884") == "is a Tactile Switches, not a USB part"


def test_another_manufacturers_equivalent_matches():
    record = lcsc().lookup("C60490")  # YAGEO RC0402FR-0710KL, 10k 0402
    assert record is not None
    assert record.manufacturer == "YAGEO"
    assert catalogue_mismatch(symbol(board(), "R6"), record, RULES) is None


def test_rules_are_caller_data():
    record = lcsc().lookup("C54313")
    assert record is not None
    part = symbol(board(), "U2")
    assert catalogue_mismatch(part, record, []) is None
    assert catalogue_mismatch(part, record, [PartRule(value="BQ*", mpn_stem="BQ25895")])
    assert catalogue_mismatch(part, record, [PartRule(value="BQ*", category="Battery")]) is None


def test_part_identity_ignores_role_values_unless_asked():
    ctx = board()
    reset, boot = symbol(ctx, "SW1"), symbol(ctx, "SW2")
    assert (reset.value, boot.value) == ("RESET", "BOOT")
    assert part_identity(reset, RULES) == part_identity(boot, RULES)
    assert part_identity(reset, RULES, include_value=True) != part_identity(
        boot, RULES, include_value=True
    )
    # Passives differ by value; "100R" and "100" are the same value.
    assert part_identity(symbol(ctx, "R6"), RULES) != part_identity(symbol(ctx, "R7"), RULES)


def test_same_part_cross_check():
    provider = lcsc()
    bq, usblc, red, blue, yageo, uniroyal = (
        provider.lookup(n) for n in ("C54313", "C15999", "C2286", "C2288", "C60490", "C25744")
    )
    assert same_part([bq, usblc], mpn_stem="BQ24074") == (
        "records disagree on the BQ24074 stem: lcsc C15999 is USBLC6-2P6"
    )
    assert same_part([bq, bq], mpn_stem="BQ24074") is None
    assert "colour" in (same_part([red, blue]) or "")
    assert same_part([yageo, uniroyal]) is None
    assert same_part([bq]) is None


# --------------------------------------------------------------------------
# SourcingCheck on the real board


def test_check_passes_the_real_board_and_caches_lookups():
    replay = Replay()
    result = run(board(), providers={"lcsc": lcsc(transport=replay)})
    assert result.status == Status.PASS, result.message
    assert result.details["parts"] == {}
    # 41 fitted parts, 25 distinct numbers: each looked up once.
    assert len(result.details["checked"]) == 41
    assert len(replay.requests) == 25
    assert len({r["keyword"] for r in replay.requests}) == 25
    # Identical buttons with role Values RESET and BOOT share C318884.
    assert "SW1" in result.details["checked"]
    assert result.details["reused"] == {}


def test_check_is_registered():
    assert CheckRegistry.get("sch.sourcing") is SourcingCheck


def test_placeholder_number_on_every_part_fails():
    ctx = board()
    fitted = run(ctx).details["checked"]
    result = run(with_fields(ctx, {ref: {"LCSC": "C25744"} for ref in fitted}))
    assert result.status == Status.FAIL
    assert "LCSC C25744" in result.details["reused"]
    assert "U2" in result.details["parts"]
    assert any("not a BQ24074" in why for why in result.details["parts"]["U2"])
    # R6 is a 10k 0402 resistor: its listing matches, but it shares the number.
    assert result.details["parts"]["R6"] == [
        f"shares LCSC C25744 with different parts {result.details['reused']['LCSC C25744']}"
    ]


def test_wrong_value_wrong_package_and_invented_numbers_fail():
    ctx = with_fields(
        board(),
        {
            "R4": {"LCSC": "C25744"},  # 10k on the 1.24k ILIM resistor
            "R6": {"LCSC": "C25804"},  # 10k 0603 on an 0402 footprint
            "U3": {"LCSC": "C999999999"},  # not in the catalogue
            "C1": {"LCSC": "C1x"},  # not an LCSC number
            "R12": {"LCSC": ""},  # missing
        },
    )
    result = run(ctx)
    assert result.status == Status.FAIL
    parts = result.details["parts"]
    assert parts["R4"][0] == "LCSC C25744 is 10kΩ, not 1.24k"
    assert "0603" in parts["R6"][0]
    assert parts["U3"] == ["LCSC C999999999 was not found by exact number in the lcsc catalogue"]
    assert "malformed" in parts["C1"][0]
    assert parts["R12"] == ["has no LCSC number"]
    # R4 now shares C25744 with the 10k resistors; that is a different part.
    assert any("shares LCSC C25744" in why for why in parts["R4"])
    assert set(parts) >= {"R4", "R6", "U3", "C1", "R12"}


def test_identical_parts_may_share_only_when_allowed():
    result = run(board(), allow_shared_identical=False)
    assert result.status == Status.FAIL
    assert result.details["reused"] == {"LCSC C318884": ["SW1", "SW2"]}


def test_exclusions_dnp_and_power_symbols_are_skipped():
    # The mounting holes are excluded from the BOM in the schematic itself.
    assert run(board(), exclude=[]).details["parts"].keys() == {"J2", "J3"}
    result = run(board(), exclude=[], skip_not_in_bom=False)
    assert result.status == Status.FAIL
    assert set(result.details["parts"]) == {"H1", "H2", "H3", "H4", "J2", "J3"}
    assert not any(ref.startswith("#") for ref in result.details["checked"])

    ctx = with_fields(board(), {"R4": {"LCSC": "C999999999"}})
    symbol(ctx, "R4").dnp = True
    assert "R4" not in run(ctx).details["checked"]


def test_outage_is_an_error_naming_the_platform_never_a_missing_part():
    def down(request, timeout):
        raise TimeoutError("timed out")

    provider = JlcpcbProvider(transport=down, attempts=1)
    result = run(board(), providers={"lcsc": provider})
    assert result.status == Status.ERROR
    assert "lcsc" in result.message
    assert result.details["parts"] == {}
    assert set(result.details["lookup_errors"]) == {"lcsc"}


def test_missing_credentials_are_an_error_naming_the_platform(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    ctx = with_fields(board(), {"U2": {"Mouser": "595-BQ24074RGTR"}})
    result = run(
        ctx,
        fields={"LCSC": "lcsc", "Mouser": "mouser"},
        require_any=True,
        providers={"lcsc": lcsc(), "mouser": MouserProvider(transport=_never)},
    )
    assert result.status == Status.ERROR
    assert "mouser" in result.message
    assert "MOUSER_API_KEY" in result.message


def test_require_any_accepts_a_number_from_any_platform():
    alt = JlcpcbProvider(transport=Replay(), sleep=no_sleep)
    alt.platform = "lcsc_alt"
    ctx = with_fields(board(), {"R6": {"LCSC": "", "LCSC_ALT": "C60490"}})
    fields = {"LCSC": "lcsc", "LCSC_ALT": "lcsc_alt"}
    providers: dict[str, SourcingProvider] = {"lcsc": lcsc(), "lcsc_alt": alt}
    result = run(ctx, fields=fields, providers=providers, require_any=True)
    assert result.status == Status.PASS, result.message

    strict = run(ctx, fields=fields, providers=providers)
    assert strict.status == Status.FAIL
    assert "has no LCSC number" in strict.details["parts"]["R6"]
    assert "has no LCSC_ALT number" in strict.details["parts"]["R4"]


def test_numbers_from_two_platforms_must_be_the_same_part():
    alt = JlcpcbProvider(transport=Replay(), sleep=no_sleep)
    alt.platform = "lcsc_alt"
    providers: dict[str, SourcingProvider] = {"lcsc": lcsc(), "lcsc_alt": alt}
    fields = {"LCSC": "lcsc", "LCSC_ALT": "lcsc_alt"}
    # No colour rule: each record alone is an 0603 LED, but they disagree.
    rules = [r for r in RULES if not r.colour]
    ctx = with_fields(board(), {"D1": {"LCSC_ALT": "C2288"}, "R6": {"LCSC_ALT": "C60490"}})
    result = run(ctx, fields=fields, providers=providers, require_any=True, rules=rules)
    assert result.status == Status.FAIL
    assert list(result.details["parts"]) == ["D1"]
    assert "records disagree on colour" in result.details["parts"]["D1"][0]


def test_params_accept_rules_as_mappings():
    check = SourcingCheck(
        providers={"lcsc": lcsc()},
        rules=[{"reference": "R*", "category": ["Resistor"], "dimension": "resistance"}],
    )
    assert check.params.rules == [
        PartRule(reference="R*", category=("Resistor",), dimension="resistance")
    ]
    with pytest.raises(ValueError):
        SourcingCheck(fields={})
    with pytest.raises(ValueError):
        PartRule(dimension="voltage")
    with pytest.raises(ValueError):
        SourcingCheck(fields={"Farnell": "farnell"})


# --------------------------------------------------------------------------
# SourcingCheck scoped by a BOM
#
# ``bom_imu.csv`` is the real BOM kicad-cli exports from the imu schematic:
#   kicad-cli sch export bom --fields 'Reference,Value,Footprint,LCSC,${QUANTITY},${DNP}'
#     --labels 'Reference,Value,Footprint,LCSC Part,Qty,DNP'
#     --group-by 'Value,Footprint,LCSC' --exclude-dnp -o bom_imu.csv imu.kicad_sch
# It groups references ("R6,R12,R13,R17-R19,R21") and lists the through-hole
# JST connectors J2 and J3, which carry no LCSC number. Variants edit a copy
# of its rows in the test.

BOM = IMU / "bom_imu.csv"


def bom_rows() -> list[BOMRow]:
    return list(
        read_bom(
            BOM.read_text(encoding="utf-8"), reference_column="Reference", quantity_column="Qty"
        )
    )


def without(rows: list[BOMRow], *references: str) -> list[BOMRow]:
    """The BOM with some references taken off their rows (an edited copy)."""
    edited = []
    for row in rows:
        kept = tuple(ref for ref in row.references if ref not in references)
        if kept:
            edited.append(dataclasses.replace(row, references=kept, quantity=len(kept)))
    return edited


def test_the_real_bom_decides_which_parts_are_checked():
    # EXCLUDE skips the JST connectors, but the BOM lists them: they are checked.
    result = run(board(), bom_path=str(BOM))
    assert result.status == Status.FAIL
    assert result.details["parts"] == {"J2": ["has no LCSC number"], "J3": ["has no LCSC number"]}
    assert len(result.details["checked"]) == 43
    assert result.details["bom_overrides"] == ["J2", "J3"]
    assert result.details["bom_not_in_design"] == []
    assert result.details["bom_not_listed"] == []

    # Rows read by the caller give the same answer as the path.
    assert run(board(), bom=bom_rows()).details == result.details


def test_a_part_off_the_bom_is_not_checked_whatever_its_flags():
    # J3 is a through-hole connector; without EXCLUDE it would be checked.
    result = run(board(), bom=without(bom_rows(), "J2", "J3"), exclude=[])
    assert result.status == Status.PASS, result.message
    assert "J3" not in result.details["checked"]
    assert result.details["bom_not_listed"] == ["J2", "J3"]
    assert "not on the BOM: J2, J3" in result.message

    complete = run(board(), bom=without(bom_rows(), "J2", "J3"), exclude=[], bom_complete=True)
    assert complete.status == Status.FAIL
    assert complete.details["parts"] == {}
    assert "J2, J3 not on the BOM" in complete.message


def test_a_bom_reference_missing_from_the_design_fails():
    rows = without(bom_rows(), "J2", "J3")
    rows.append(BOMRow(("R99",), 1, {**rows[0].fields, "Reference": "R99"}))
    result = run(board(), bom=rows)
    assert result.status == Status.FAIL
    assert result.details["bom_not_in_design"] == ["R99"]
    assert result.details["parts"] == {}
    assert "BOM lists R99, not in the design" in result.message


def test_a_plain_reference_list_is_a_bom():
    references = [ref for row in without(bom_rows(), "J2", "J3", "R12") for ref in row.references]
    result = run(board(), bom=references)
    assert result.status == Status.PASS, result.message
    assert "R12" not in result.details["checked"]
    assert result.details["bom_not_listed"] == ["R12"]
    # Grouped cells are expanded, and a part listed twice fails.
    grouped = run(board(), bom=["R6,R12,R17-R19", "R12"])
    assert grouped.details["checked"] == ["R6", "R12", "R17", "R18", "R19"]
    assert grouped.details["parts"] == {"R12": ["is listed on 2 BOM rows"]}


def test_part_numbers_can_come_from_a_bom_column():
    # Symbol LCSC fields blanked: the numbers now come only from the BOM.
    ctx = with_fields(board(), {"R4": {"LCSC": ""}, "U2": {"LCSC": ""}})
    rows = without(bom_rows(), "J2", "J3")
    result = run(ctx, bom=rows, bom_fields={"LCSC Part": "lcsc"})
    assert result.status == Status.PASS, result.message

    # A wrong number in the BOM column is caught against the symbol.
    r4 = next(i for i, row in enumerate(rows) if row.references == ("R4",))
    rows[r4] = dataclasses.replace(rows[r4], fields={**rows[r4].fields, "LCSC Part": "C25744"})
    result = run(ctx, bom=rows, bom_fields={"LCSC Part": "lcsc"})
    assert result.status == Status.FAIL
    assert result.details["parts"]["R4"][0] == "LCSC Part C25744 is 10kΩ, not 1.24k"

    # From the file, J2 and J3 have an empty column.
    result = run(ctx, bom_path=BOM, bom_fields={"LCSC Part": "lcsc"})
    assert set(result.details["parts"]) == {"J2", "J3"}
    assert result.details["parts"]["J3"] == ["has no LCSC Part number"]


def test_bom_params_stay_plain_data_and_are_validated(tmp_path):
    check = SourcingCheck(providers={"lcsc": lcsc()}, bom=bom_rows()[:1])
    assert check.params.bom == [
        {
            "references": ["C1"],
            "fields": {
                "Reference": "C1",
                "Value": "1uF",
                "Footprint": "Capacitor_SMD:C_0402_1005Metric",
                "LCSC Part": "C52923",
                "Qty": "1",
                "DNP": "",
            },
        }
    ]
    json.dumps(check.params_dict)
    assert SourcingCheck(bom=check.params.bom).params.bom == check.params.bom
    with pytest.raises(ValueError, match="not both"):
        SourcingCheck(bom=["R1"], bom_path=str(BOM))
    with pytest.raises(ValueError, match="needs a bom"):
        SourcingCheck(bom_fields={"LCSC Part": "lcsc"})
    with pytest.raises(ValueError, match="plain reference"):
        SourcingCheck(bom=["R1"], bom_fields={"LCSC Part": "lcsc"})
    with pytest.raises(ValueError):
        SourcingCheck(bom=["R1-"])

    missing = run(board(), bom_path=str(tmp_path / "bom.csv"))
    assert missing.status == Status.ERROR
    assert "Could not read the BOM" in missing.message
    no_column = run(board(), bom_path=BOM, bom_fields={"LCSC#": "lcsc"})
    assert no_column.status == Status.ERROR
    assert "no column LCSC#" in no_column.message


def test_without_a_bom_no_bom_details_are_reported():
    details = run(board()).details
    assert not any(key.startswith("bom_") for key in details)


# --------------------------------------------------------------------------
# Digi-Key and Mouser: request construction and error handling only


def _never(request, timeout):
    raise AssertionError("no request should be sent")


def test_digikey_missing_credentials_is_a_lookup_error(monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    provider = DigiKeyProvider(transport=_never, sleep=no_sleep)
    with pytest.raises(SourcingLookupError, match="DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET"):
        provider.lookup("296-1234-1-ND")


class Scripted:
    """A transport answering each request with the next scripted reply."""

    def __init__(self, *replies: HttpResponse | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


TOKEN = HttpResponse(200, b'{"access_token": "tok", "expires_in": 599, "token_type": "Bearer"}')


def test_digikey_requests_follow_the_published_api():
    transport = Scripted(TOKEN, HttpResponse(404, b'{"title": "Not Found", "status": 404}'))
    provider = DigiKeyProvider(
        client_id="id", client_secret="secret", transport=transport, sleep=no_sleep
    )
    assert provider.lookup("296-1234-1-ND") is None  # 404: not in the catalogue

    token, details = transport.requests
    assert token.get_method() == "POST"
    assert token.full_url == "https://api.digikey.com/v1/oauth2/token"
    assert urllib.parse.parse_qs(token.data.decode()) == {
        "client_id": ["id"],
        "client_secret": ["secret"],
        "grant_type": ["client_credentials"],
    }
    assert details.get_method() == "GET"
    assert details.full_url == (
        "https://api.digikey.com/products/v4/search/296-1234-1-ND/productdetails"
    )
    assert details.get_header("Authorization") == "Bearer tok"
    assert details.get_header("X-digikey-client-id") == "id"
    assert details.get_header("X-digikey-locale-site") == "US"


def test_digikey_reuses_its_token_and_quotes_numbers():
    transport = Scripted(TOKEN, HttpResponse(404, b"{}"), HttpResponse(404, b"{}"))
    provider = DigiKeyProvider(client_id="id", client_secret="s", transport=transport)
    provider.lookup("A/B 1")
    provider.lookup("X")
    assert len(transport.requests) == 3
    assert transport.requests[1].full_url.endswith("/search/A%2FB%201/productdetails")


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        ((HttpResponse(401, b'{"detail": "Invalid clientId"}'),), "credentials rejected"),
        ((TOKEN, HttpResponse(403, b'{"detail": "No subscription"}')), "forbidden"),
        ((TOKEN, HttpResponse(429, b'{"detail": "Rate limit"}')), "HTTP 429"),
        ((TOKEN, HttpResponse(200, b"not json")), "not JSON"),
        ((TOKEN, HttpResponse(200, b'{"SearchLocaleUsed": {}}')), "malformed"),
        ((OSError("dns"),), "request failed"),
    ],
)
def test_digikey_failures_are_lookup_errors(replies, reason):
    provider = DigiKeyProvider(
        client_id="id", client_secret="s", transport=Scripted(*replies), attempts=1
    )
    with pytest.raises(SourcingLookupError, match=reason) as caught:
        provider.lookup("296-1234-1-ND")
    assert caught.value.platform == "digikey"


def test_digikey_refused_credentials_are_not_retried():
    transport = Scripted(HttpResponse(401, b"{}"), TOKEN)
    provider = DigiKeyProvider(
        client_id="id", client_secret="s", transport=transport, sleep=no_sleep
    )
    with pytest.raises(SourcingLookupError, match="credentials rejected"):
        provider.lookup("296-1234-1-ND")
    assert len(transport.requests) == 1


def test_mouser_request_follows_the_published_api():
    transport = Scripted(
        HttpResponse(200, b'{"Errors": [], "SearchResults": {"NumberOfResult": 0, "Parts": []}}')
    )
    provider = MouserProvider(api_key="key", transport=transport)
    assert provider.lookup("595-BQ24074RGTR") is None
    (request,) = transport.requests
    assert request.get_method() == "POST"
    url = urllib.parse.urlsplit(request.full_url)
    assert url.netloc == "api.mouser.com"
    assert url.path == "/api/v1/search/partnumber"
    assert urllib.parse.parse_qs(url.query) == {"apiKey": ["key"]}
    assert json.loads(request.data) == {
        "SearchByPartRequest": {
            "mouserPartNumber": "595-BQ24074RGTR",
            "partSearchOptions": "Exact",
        }
    }


def test_mouser_failures_are_lookup_errors(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    with pytest.raises(SourcingLookupError, match="MOUSER_API_KEY"):
        MouserProvider(transport=_never).lookup("595-BQ24074RGTR")

    rejected = HttpResponse(200, b'{"Errors": [{"Code": "Invalid", "Message": "Invalid key"}]}')
    transport = Scripted(rejected, rejected)
    with pytest.raises(SourcingLookupError, match="Invalid key"):
        MouserProvider(api_key="k", transport=transport, sleep=no_sleep).lookup("595-X1")
    assert len(transport.requests) == 1  # a refused key is not retried

    for reply in (HttpResponse(500, b""), HttpResponse(200, b"[1]"), HttpResponse(200, b"{")):
        with pytest.raises(SourcingLookupError):
            MouserProvider(api_key="k", transport=Scripted(reply), attempts=1).lookup("595-X1")

    with pytest.raises(ValueError):
        MouserProvider(api_key="k", transport=_never).lookup("595-A|595-B")


# --------------------------------------------------------------------------
# Live catalogues

live = pytest.mark.skipif(
    os.environ.get("KICAD_EVALTOR_LIVE_SOURCING") != "1",
    reason="set KICAD_EVALTOR_LIVE_SOURCING=1 to query live catalogues",
)


@pytest.mark.network
@live
def test_live_jlcpcb_matches_the_recordings():
    live_provider = JlcpcbProvider()
    for number in ("C25744", "C2286", "C54313"):
        record = live_provider.lookup(number)
        recorded = lcsc().lookup(number)
        assert record is not None
        assert recorded is not None
        assert (record.mpn, record.category, record.package) == (
            recorded.mpn,
            recorded.category,
            recorded.package,
        )
    assert live_provider.lookup("C999999999") is None


@pytest.mark.network
@live
@pytest.mark.skipif(
    not (os.environ.get("DIGIKEY_CLIENT_ID") and os.environ.get("DIGIKEY_CLIENT_SECRET")),
    reason="needs DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET",
)
def test_live_digikey_resolves_a_manufacturer_number():
    record = DigiKeyProvider().lookup("BQ24074RGTR")
    assert isinstance(record, PartRecord)
    assert "BQ24074" in record.mpn


@pytest.mark.network
@live
@pytest.mark.skipif(not os.environ.get("MOUSER_API_KEY"), reason="needs MOUSER_API_KEY")
def test_live_mouser_resolves_a_mouser_number():
    record = MouserProvider().lookup("595-BQ24074RGTR")
    assert isinstance(record, PartRecord)
    assert "BQ24074" in record.mpn
