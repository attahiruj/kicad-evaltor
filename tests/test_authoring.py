"""Regression coverage for public test-authoring primitives and caller ownership."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_evaltor import (
    CircuitQueries,
    CompositeCheck,
    Connectivity,
    DCNetwork,
    FileNet,
    FileNetComponent,
    FileNetNode,
    FileNetlist,
    MeasurementCheck,
    NetworkUnavailable,
    RegulatorModel,
    RegulatorModels,
    ReportPolicy,
    Resistor,
    TestReport as Report,
    TestRunner as Runner,
    TestStatus as Status,
    json_evidence,
    parse_quantity,
    read_bom,
    reconcile_bom,
    regulator_model,
)
from kicad_evaltor.checks.schematic.overlaps import TextTextOverlapCheck
from kicad_evaltor.connectivity import pin_pairs
from kicad_evaltor.geometry import BBox
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import SchematicScene, TextItem
from kicad_evaltor.sexpr import is_hidden, parse


def test_user_owns_tests_limits_and_aggregation():
    calls = []

    def measure(ctx):
        calls.append(ctx)
        return 3.4

    low = MeasurementCheck("user.low", measure, lambda value: value <= 3.3, requirement_id="low")
    high = MeasurementCheck("user.high", measure, lambda value: value <= 3.5, requirement_id="high")
    assert calls == []
    report = Runner([low, high]).run(None)
    assert [r.status for r in report.results] == [Status.FAIL, Status.PASS]
    assert len(calls) == 2
    assert report.accepts(ReportPolicy(required_ids=("high",)))
    assert not report.accepts(ReportPolicy(required_ids=("missing",)))
    assert Report().all_passed
    assert not Report().accepts(ReportPolicy(allow_empty=False))
    assert Runner().run(None).results == []


def test_composition_keeps_errors_and_each_branch_evidence():
    good = MeasurementCheck("good", lambda _ctx: 1, lambda _value: True)
    bad = MeasurementCheck("broken", lambda _ctx: 1 / 0, lambda _value: True)
    all_result = Runner([CompositeCheck("both", [good, bad], mode="all")]).run(None).results[0]
    assert all_result.status is Status.ERROR
    assert len(all_result.details["children"]) == 2
    either = Runner([CompositeCheck("either", [good, bad], mode="any")]).run(None).results[0]
    assert either.is_pass
    assert either.details["children"][1]["status"] == "error"


def test_evidence_conversion_and_invalid_custom_evidence():
    @dataclass
    class Data:
        file: Path
        status: Status

    converted = json_evidence({"object": Data(Path("board"), Status.PASS), "nets": {"B", "A"}})
    assert json.loads(json.dumps(converted))["nets"] == ["A", "B"]
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="Cyclic"):
        json_evidence(cyclic)
    with pytest.raises(TypeError, match="Unsupported"):
        json_evidence(object())
    check = MeasurementCheck("custom", lambda _ctx: object(), lambda _value: True)
    assert Runner([check], validate_evidence=True).run(None).results[0].status is Status.ERROR


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_parameters_are_not_measurements(value):
    with pytest.raises(ValueError, match="finite"):
        TextTextOverlapCheck(clearance=value)


@pytest.mark.parametrize(
    ("flag", "hidden"),
    [("hide", True), ("(hide)", True), ("(hide yes)", True), ("(hide no)", False)],
)
def test_legacy_and_current_hidden_field_syntax(flag, hidden):
    assert (
        is_hidden(parse(f'(property "Value" "100n" (effects (font (size 1 1)) {flag}))')) is hidden
    )


def test_quoted_hide_is_content_not_a_visibility_flag():
    assert not is_hidden(parse('(label "hide" (effects (font (size 1 1))))'))
    assert not is_hidden(parse('(property "Value" "hide" (effects (font (size 1 1))))'))


def test_all_findings_survive_beyond_display_sample():
    from types import SimpleNamespace

    scene = SchematicScene(texts=[TextItem(str(i), BBox(0, 0, 2, 2), 0, 1) for i in range(8)])
    target = SimpleNamespace(scene=scene, prefix="", sheet=SimpleNamespace(path="/"))
    findings = TextTextOverlapCheck(clearance=0)._run_sheet(target)
    from kicad_evaltor.checks.schematic.overlaps import _report

    result = _report("texts", findings.summaries, findings.details, findings.checked)
    assert result.details["count"] == 28
    assert len(result.details["overlaps"]) == 28


@pytest.mark.parametrize(
    ("text", "dimension", "expected"),
    [
        ("16 MHz / CL=9p", "frequency", 16e6),
        ("16 MHz / CL=9p", "capacitance", 9e-12),
        ("100n / 50V", "capacitance", 100e-9),
        ("4k7 1%", "resistance", 4700),
        ("0.1uF", "capacitance", 1e-7),
        ("1m", "resistance", 0.001),
        ("1M", "resistance", 1e6),
        ("10Ohms", "resistance", 10),
        ("4.7kΩ", "resistance", 4700),
    ],
)
def test_dimension_aware_values(text, dimension, expected):
    assert parse_quantity(text, dimension=dimension).value == pytest.approx(expected)


def test_quantity_never_uses_incompatible_unit_or_guesses_ambiguity():
    assert not parse_quantity("50V", dimension="capacitance").known
    assert not parse_quantity("10nF / 20nF", dimension="capacitance").known
    assert not parse_quantity("100n / 50V").known


@pytest.mark.parametrize("token", ["10R", "10R0", "0R", "R47", "4.7R", ".5R", "+10R", "-10R"])
def test_resistance_notation_cannot_take_another_dimension(token):
    from kicad_evaltor.values import parse_value

    inferred = parse_quantity(token)
    assert inferred.dimension == "resistance"
    assert inferred.value == parse_value(token)
    assert parse_quantity(token, dimension="resistance").value == inferred.value
    for dimension in ("capacitance", "inductance", "voltage", "current", "frequency"):
        assert not parse_quantity(token, dimension=dimension).known


@pytest.mark.parametrize("injected", [{}, {"a": 1}, {"a": 1, "b": -1}])
@pytest.mark.parametrize("with_anchored_load", [False, True])
def test_floating_triangle_never_establishes_node_voltages(injected, with_anchored_load):
    network = DCNetwork(
        [
            Resistor("R1", "a", "b", 1),
            Resistor("R2", "b", "c", 3),
            Resistor("R3", "c", "a", 7),
        ]
        + ([Resistor("load", "unrelated_ground", "other", 2)] if with_anchored_load else [])
    )
    with pytest.raises(NetworkUnavailable, match=r"Floating.*voltage boundary"):
        network.solve({"unrelated_ground": 0}, injected=injected)


def test_each_disconnected_network_can_have_its_own_voltage_boundary():
    network = DCNetwork(
        [Resistor("R1", "a", "b", 1), Resistor("R2", "c", "d", 3), Resistor("link", "d", "e", 0)]
    )
    result = network.solve({"a": 2, "c": 5}, injected={"b": -1, "e": -1})
    assert result.voltages == pytest.approx({"a": 2, "b": 1, "c": 5, "d": 2, "e": 2})
    with pytest.raises(NetworkUnavailable, match="Floating"):
        network.solve({"a": 2})
    with pytest.raises(NetworkUnavailable, match="Floating"):
        network.solve({"a": 2, "c": 5}, injected={"isolated": 1})


def test_dc_network_zero_link_load_and_corners():
    network = DCNetwork(
        [
            Resistor("R1", "in", "tap", 1000),
            Resistor("R2", "tap", "gnd", 1000),
            Resistor("link", "tap", "out", 0),
        ]
    )
    solved = network.solve({"in": 5, "gnd": 0}, injected={"out": -0.001})
    assert solved.voltages["out"] == pytest.approx(2)
    assert solved.currents["R1"] == pytest.approx(0.003)
    assert solved.currents["link"] is None
    assert solved.powers["R1"] == pytest.approx(0.009)
    assert network.equivalent_resistance("tap", "out") == 0
    results = list(network.corners({"in": 5, "gnd": 0}, {"R1": 0.1, "R2": 0.1}))
    assert len(results) == 4
    assert min(s.voltages["out"] for s in results) == pytest.approx(2.25)
    with pytest.raises(NetworkUnavailable, match="Conflicting"):
        network.solve({"tap": 0, "out": 1})
    with pytest.raises(NetworkUnavailable, match="Floating"):
        DCNetwork([Resistor("R", "a", "b", 100)]).solve({"gnd": 0})


def test_resistor_bridge_kcl_and_equivalent_resistance():
    network = DCNetwork(
        [
            Resistor("R1", "a", "left", 100),
            Resistor("R2", "left", "b", 100),
            Resistor("R3", "a", "right", 100),
            Resistor("R4", "right", "b", 100),
            Resistor("R5", "left", "right", 100),
        ]
    )
    assert network.equivalent_resistance("a", "b") == pytest.approx(100)
    result = network.solve({"a": 10, "b": 0})
    assert result.currents["R5"] == pytest.approx(0)
    assert result.currents["R1"] == pytest.approx(result.currents["R2"])


def path_netlist():
    return FileNetlist(
        [
            FileNet("a", [FileNetNode("U1", "1", "a"), FileNetNode("X1", "1", "a", "IO1")]),
            FileNet("b", [FileNetNode("X1", "2", "b", "IO1"), FileNetNode("U2", "1", "b")]),
            FileNet(
                "other", [FileNetNode("X1", "3", "other", "IO2"), FileNetNode("U2", "2", "other")]
            ),
        ],
        {"X1": FileNetComponent("X1", lib_id="Custom:Protection")},
    )


def test_paths_have_witnesses_and_do_not_join_channels():
    netlist = path_netlist()
    graph = Connectivity(netlist)
    bridge = pin_pairs("X1", ("1", "2"))
    assert graph.path(("U1", "1"), ("U2", "1")) is None
    path = graph.path(("U1", "1"), ("U2", "1"), through=bridge, max_crossings=1)
    assert path.nets == ("a", "b")
    assert (path.steps[0].reference, path.steps[0].from_pin, path.steps[0].to_pin) == (
        "X1",
        "1",
        "2",
    )
    assert graph.path(("U1", "1"), ("U2", "1"), through=bridge, max_crossings=0) is None
    assert graph.path(("U1", "1"), ("U2", "2"), through=bridge) is None
    assert graph.path(("U1", "1"), ("U2", "1"), through=bridge, forbidden={"b"}) is None
    with pytest.raises(ValueError, match="Endpoint"):
        graph.path(("missing", "1"), ("U2", "1"))
    assert len(netlist.pin_roles("X1")["IO1"]) == 2
    assert len(netlist.select_components(lib_id="Custom:*").items) == 1
    assert CircuitQueries(netlist).channel("X1", {"first": ["1", "2"]}) == {"first": ("a", "b")}


def test_models_are_scoped_without_global_registration():
    first, second = RegulatorModels(include_builtin=False), RegulatorModels(include_builtin=False)
    first.register("Mine:*", RegulatorModel("fixed", vout=2.8))
    assert regulator_model("Mine:LDO", models=first).vout == 2.8
    assert regulator_model("Mine:LDO", models=second) is None


def test_bom_reconciliation_is_reference_and_quantity_based():
    rows = read_bom(
        'refs,qty,LCSC\n"R1-R3",2,C123\n', reference_column="refs", quantity_column="qty"
    )
    differences = reconcile_bom(
        rows, {"R1": {"LCSC": "C123"}, "R2": {"LCSC": "C999"}, "R4": {"LCSC": "C123"}}
    )
    assert differences["missing"] == ["R4"]
    assert differences["extra"] == ["R3"]
    assert differences["quantity_mismatches"][0]["quantity"] == 2
    assert differences["field_mismatches"][0]["reference"] == "R2"


def test_project_specific_annotation_keeps_symbol_identity(tmp_path):
    path = tmp_path / "shared.kicad_sch"
    path.write_text(
        """(kicad_sch (uuid "child") (lib_symbols)
      (symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (uuid "placed")
       (property "Reference" "R1") (property "Value" "1k")
       (instances (project "tx" (path "/root/sheet" (reference "R2") (unit 1)))
                  (project "rx" (path "/other/sheet" (reference "R8") (unit 1))))))""",
        encoding="utf-8",
    )
    tx = FileSchematic(
        path, project_name="tx", instance_path="/sheet", root_uuid="root"
    ).get_symbols()[0]
    rx = FileSchematic(
        path, project_name="rx", instance_path="/sheet", root_uuid="other"
    ).get_symbols()[0]
    assert (tx.reference, rx.reference) == ("R2", "R8")
    assert tx.uuid == rx.uuid == "placed"
    assert path.read_text().count('"R1"') == 1


def test_prerequisites_are_only_the_ones_the_caller_declares():
    calls = []
    bad = MeasurementCheck("bad", lambda _ctx: False, bool, requirement_id="source")
    dependent = MeasurementCheck(
        "dependent", lambda _ctx: calls.append(1), lambda _value: True, requirement_id="dependent"
    )
    result = Runner([bad, dependent], prerequisites={"dependent": ("source",)}).run(None)
    assert result.results[1].status is Status.SKIP
    assert calls == []
    Runner([bad, dependent]).run(None)
    assert calls == [1]
    with pytest.raises(ValueError, match="earlier"):
        Runner([bad, dependent], prerequisites={"source": ("dependent",)}).run(None)


def test_active_project_page_claims_and_repeated_sheet_annotations(tmp_path):
    from kicad_evaltor import DesignContext
    from kicad_evaltor.checks.schematic.sheets import SheetNameOrPageCollisionCheck

    shared = tmp_path / "shared.kicad_sch"
    shared.write_text(
        """(kicad_sch (uuid "child") (lib_symbols)
      (symbol (lib_id "Device:R") (at 10 10 0) (unit 1) (uuid "placed")
       (property "Reference" "R1") (property "Value" "1k")
       (instances (project "tx" (path "/root/first" (reference "R2") (unit 1))
                                (path "/root/second" (reference "R3") (unit 1))))))""",
        encoding="utf-8",
    )
    sheets = []
    for name, number in (("first", 2), ("second", 3)):
        sheets.append(f"""(sheet (uuid "{name}") (at 1 1) (size 10 10)
          (property "Sheetname" "{name}") (property "Sheetfile" "shared.kicad_sch")
          (instances (project "tx" (path "/root" (page "{number}")))
                     (project "rx" (path "/other" (page "{number}")))))""")
    root = tmp_path / "tx.kicad_sch"
    root.write_text(
        '(kicad_sch (uuid "root") (paper "A4") (lib_symbols) '
        + "".join(sheets)
        + ' (sheet_instances (path "/" (page "1"))))',
        encoding="utf-8",
    )
    with DesignContext(project_path=tmp_path / "tx.kicad_pro") as ctx:
        assert [s.symbol.reference for s in ctx.index().symbols] == ["R2", "R3"]
        assert len({s.identity for s in ctx.index().symbols}) == 2
        assert SheetNameOrPageCollisionCheck().run(ctx).is_pass


def test_ambiguous_sheet_selection_and_optional_completeness(tmp_path):
    from kicad_evaltor import DesignContext, SchematicTree
    from kicad_evaltor.hierarchy import AmbiguousSheet

    root = tmp_path / "root.kicad_sch"
    root.write_text(
        """(kicad_sch (uuid "root") (paper "A4") (lib_symbols)
        (sheet (uuid "one") (property "Sheetname" "duplicate") (property "Sheetfile" "child.kicad_sch"))
        (sheet (uuid "two") (property "Sheetname" "duplicate") (property "Sheetfile" "child.kicad_sch"))
        (sheet (uuid "missing") (property "Sheetname" "missing") (property "Sheetfile" "absent.kicad_sch")))""",
        encoding="utf-8",
    )
    (tmp_path / "child.kicad_sch").write_text(
        '(kicad_sch (uuid "child") (paper "A4") (lib_symbols))', encoding="utf-8"
    )
    tree = SchematicTree.load(root)
    with pytest.raises(AmbiguousSheet, match="/one"):
        tree.find("duplicate")
    assert tree.find("/one").name == "duplicate"
    with DesignContext(schematic_path=root) as ctx:
        assert (
            TextTextOverlapCheck(require_complete=True).run(ctx).details["reason_code"]
            == "scope_incomplete"
        )
        assert TextTextOverlapCheck(sheet="/one", require_complete=True).run(ctx).is_pass
        assert TextTextOverlapCheck(sheet="/", require_complete=True).run(ctx).is_pass


def test_output_referenced_regulator_uses_explicit_numeric_pins():
    from kicad_evaltor import regulator_output

    nets = FileNetlist(
        [
            FileNet("out", [FileNetNode("U", "1", "out"), FileNetNode("R1", "1", "out")]),
            FileNet(
                "adj",
                [
                    FileNetNode("U", "2", "adj"),
                    FileNetNode("R1", "2", "adj"),
                    FileNetNode("R2", "1", "adj"),
                ],
            ),
            FileNet("return", [FileNetNode("U", "3", "return"), FileNetNode("R2", "2", "return")]),
        ],
        {
            "U": FileNetComponent("U"),
            "R1": FileNetComponent("R1", value="100"),
            "R2": FileNetComponent("R2", value="200"),
        },
    )
    model = RegulatorModel(
        "output_reference",
        vref=1.0,
        out_numbers=("1",),
        feedback_numbers=("2",),
        ground_numbers=("3",),
    )
    assert regulator_output(nets, "U", model=model).volts == pytest.approx(3)


@pytest.mark.parametrize("vref", [1.25, -1.25])
@pytest.mark.parametrize("kind", ["output_reference", "divider"])
def test_grounded_adjustment_pin_depends_on_reference_model(kind, vref):
    from kicad_evaltor import regulator_output

    nets = FileNetlist(
        [
            FileNet("out", [FileNetNode("U", "1", "out")]),
            FileNet("return", [FileNetNode("U", "2", "return"), FileNetNode("U", "3", "return")]),
        ],
        {"U": FileNetComponent("U")},
    )
    model = RegulatorModel(
        kind, vref=vref, out_numbers=("1",), feedback_numbers=("2",), ground_numbers=("3",)
    )
    result = regulator_output(nets, "U", model=model)
    if kind == "output_reference":
        assert result.volts == vref
        assert result.parts == ()
    else:
        assert not result.known
        assert "feedback pin is grounded" in result.reason


def test_grounded_output_cannot_satisfy_an_output_reference_model():
    from kicad_evaltor import regulator_output

    nets = FileNetlist(
        [FileNet("return", [FileNetNode("U", pin, "return") for pin in ("1", "2", "3")])],
        {"U": FileNetComponent("U")},
    )
    model = RegulatorModel(
        "output_reference",
        vref=1.25,
        out_numbers=("1",),
        feedback_numbers=("2",),
        ground_numbers=("3",),
    )
    result = regulator_output(nets, "U", model=model)
    assert not result.known
    assert "output pin is grounded" in result.reason


def test_drawing_contact_does_not_join_bare_crossings():
    from kicad_evaltor import wire_groups
    from kicad_evaltor.schematic_items import Junction, WireSegment

    scene = SchematicScene(wires=[WireSegment((-1, 0), (1, 0)), WireSegment((0, -1), (0, 1))])
    assert len(set(wire_groups(scene).wires)) == 2
    scene.junctions.append(Junction((0, 0)))
    assert len(set(wire_groups(scene).wires)) == 1
