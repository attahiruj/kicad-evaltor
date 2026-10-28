"""Regression and mutation cases for user-authored evaluation rules."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

import kicad_evaltor

from kicad_evaltor import (
    ComponentValueCheck,
    DesignContext,
    DRCRunCheck,
    ERCRunCheck,
    NetMembersCheck,
    PinsConnectedCheck,
    TraceWidthCheck,
)
from kicad_evaltor.board_connectivity import net_graph
from kicad_evaltor.board_file import BoardTrack, BoardVia
from kicad_evaltor.checks.pcb.matches_schematic import FootprintMatchesSchematicCheck
from kicad_evaltor.core.runner import TestRunner as Runner
from kicad_evaltor.netlist import FileNet, FileNetlist, FileNetNode
from kicad_evaltor.selection import select
from kicad_evaltor.utils.kicad_cli import (
    REPORT_CWD,
    InvalidReport,
    SubprocessResult,
    parse_erc_report,
    parse_drc_report,
    report_workspace,
)


def circuit():
    nets = [
        FileNet("A", [FileNetNode("U1", "1", "A"), FileNetNode("R1", "1", "A")]),
        FileNet("B", [FileNetNode("J1", "1", "B"), FileNetNode("R1", "2", "B")]),
    ]
    ctx = Mock()
    ctx.has_schematic.return_value = True
    ctx.schematic.get_netlist.return_value = FileNetlist(nets)
    return ctx


@pytest.mark.parametrize("parser", [parse_erc_report, parse_drc_report])
@pytest.mark.parametrize(
    "payload",
    ["{broken", "{}", "[]", '{"violations": {}}', '{"violations": [null]}', '{"violations": [{}]}'],
)
def test_bad_reports_are_not_empty_successes(parser, payload):
    with pytest.raises(InvalidReport):
        parser(payload)


@pytest.mark.parametrize("check", [ERCRunCheck(strict=True), DRCRunCheck(strict=True)])
def test_bad_report_is_error_even_on_successful_process(check, tmp_path):
    ctx = Mock()
    ctx.has_schematic.return_value = ctx.has_board.return_value = True
    ctx.schematic_path = tmp_path / "x.kicad_sch"
    ctx.board_path = tmp_path / "x.kicad_pcb"
    ctx.memo.side_effect = lambda _key, compute: compute()

    def run(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text("{broken")
        return SubprocessResult(0, "", "")

    ctx.run_kicad_cli.side_effect = run
    assert check.run(ctx).status.value == "error"


def test_value_case_does_not_erase_si_magnitude():
    ctx = Mock()
    ctx.sheet_symbols.return_value = [NS(reference="R1", value="1m")]
    assert ComponentValueCheck(reference="R1", expected="1M").run(ctx).is_fail


def test_mid_segment_via_connects_layers():
    tracks = [
        BoardTrack((0, 0), (10, 0), 0.2, "F.Cu", "N"),
        BoardTrack((5, 0), (5, 5), 0.2, "B.Cu", "N"),
    ]
    board = NS(
        tracks_on=lambda _net: tracks,
        vias_on=lambda _net: [BoardVia(5, 0, 0.6, 0.3, ("F.Cu", "B.Cu"), "N")],
        copper_layers=["F.Cu", "B.Cu"],
    )
    graph = net_graph(board, "N")
    assert graph.shortest([(0, 0, "F.Cu")], [(5, 5, "B.Cu")]) == 10
    board.vias_on = lambda _net: []
    assert net_graph(board, "N").shortest([(0, 0, "F.Cu")], [(5, 5, "B.Cu")]) is None


def test_connection_requires_explicit_bridge_and_forbids_shorts():
    ctx = circuit()
    assert PinsConnectedCheck(pin_a="U1.1", pin_b="J1.1").run(ctx).is_fail
    assert PinsConnectedCheck(pin_a="U1.1", pin_b="J1.1", connected=False).run(ctx).is_pass
    params = dict(pin_a="U1.1", pin_b="J1.1", through={"R1": [("1", "2")]})
    assert PinsConnectedCheck(**params).run(ctx).is_pass
    assert PinsConnectedCheck(**params, connected=False).run(ctx).is_fail


def test_missing_endpoint_cannot_prove_disconnection():
    result = PinsConnectedCheck(pin_a="U1.99", pin_b="J1.1", connected=False).run(circuit())
    assert result.status.value == "error"


def test_exact_topology_detects_extra_and_missing_pins():
    ctx = circuit()
    assert NetMembersCheck(net="A", pins=["U1.1", "R1.1"]).run(ctx).is_pass
    assert NetMembersCheck(net="A", pins=["U1.1"]).run(ctx).is_fail
    assert NetMembersCheck(net="A", pins=["U1.1"], exact=False).run(ctx).is_pass
    assert NetMembersCheck(net="A", pins=["U1.1", "J1.1"], exact=False).run(ctx).is_fail


@pytest.mark.parametrize("policy", ["pass", "fail", "skip"])
def test_empty_selection_is_caller_controlled(policy):
    ctx = Mock()
    ctx.board.tracks = []
    ctx.board.nets = {"N"}
    ctx.sheet_symbols.return_value = []
    checks = [
        TraceWidthCheck(net_name="N", min_width_mm=0.2, on_empty=policy),
        FootprintMatchesSchematicCheck(on_empty=policy),
    ]
    for check in checks:
        result = check.run(ctx)
        assert result.status.value == policy
        assert result.details["selected_count"] == 0


def test_result_instances_keep_identity_and_parameter_snapshot():
    check = PinsConnectedCheck(pin_a="U1.1", pin_b="R1.1", requirement_id="signal")
    report = Runner([check]).run(circuit())
    check.params.pin_b = "J1.1"
    assert report.results[0].parameters["pin_b"] == "R1.1"
    assert report.results[0].requirement_id == "signal"
    assert report.evaluated_count == 1
    assert not report.not_passed()
    assert Runner([check]).run(circuit()).failed_count == 1
    with pytest.raises(ValueError, match="Duplicate"):
        Runner([check, check]).run(circuit())


def test_selectors_expose_cardinality():
    selected = select(["U1", "R1", "R2"], "R*", key=str)
    assert selected.items == ("R1", "R2")
    assert selected.total == 3
    assert selected.require_count(2, 2) is selected
    with pytest.raises(ValueError):
        selected.require_count(3)


def test_old_selectors_path_still_imports():
    from kicad_evaltor.selectors import select as old_select

    assert old_select is select


def test_package_directory_does_not_shadow_stdlib_selectors():
    # Python puts the script's directory first on sys.path, so a module named
    # like a stdlib one inside the package would replace it there.
    package = Path(kicad_evaltor.__file__).parent
    assert not (package / "selectors.py").exists()
    run = subprocess.run(
        [sys.executable, "-c", "import subprocess, selectors; selectors.DefaultSelector"],
        cwd=package,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr


def test_workspaces_do_not_change_cwd_and_are_context_local():
    original = Path.cwd()

    def worker(i):
        with report_workspace(str(i)) as directory:
            assert Path.cwd() == original
            assert REPORT_CWD.get() == directory
            with report_workspace("nested") as nested:
                assert REPORT_CWD.get() == nested
            assert REPORT_CWD.get() == directory
            return directory

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(worker, range(4)))
    assert len(set(paths)) == 4
    assert REPORT_CWD.get() is None
    assert all(not path.exists() for path in paths)


def test_context_invalidation_reloads_file(tmp_path):
    path = tmp_path / "x.kicad_pcb"
    path.write_text("(kicad_pcb (version 1))")
    ctx = DesignContext(board_path=path)
    assert ctx.board.version == 1
    path.write_text("(kicad_pcb (version 2))")
    assert ctx.board.version == 1
    ctx.invalidate()
    assert ctx.board.version == 2
    ctx.close()
    assert ctx._board is None


def test_subprocess_receives_workspace_cwd(monkeypatch):
    import kicad_evaltor.core.context as context_module

    seen = []

    def run(args, cwd=None, timeout=600):
        seen.append(cwd)
        return SubprocessResult(0, "", "")

    monkeypatch.setattr(context_module, "run_kicad_cli", run)
    ctx = DesignContext(kicad_cli_path="test-cli")
    with report_workspace("test") as directory:
        ctx.run_kicad_cli(["sch", "erc"])
        assert seen == [directory]


def test_runner_copies_results_and_rejects_invalid_return():
    from kicad_evaltor.checks.base import CheckResult

    check = PinsConnectedCheck(pin_a="U1.1", pin_b="R1.1")
    original = CheckResult.pass_(check.id, evidence=[1])
    check.run = lambda _ctx: original
    report = Runner([check]).run(circuit())
    original.details["evidence"].append(2)
    assert report.results[0].details["evidence"] == [1]
    check.run = lambda _ctx: None
    assert Runner([check]).run(circuit()).error_count == 1


# --- Audit fixes -------------------------------------------------------------


def test_an_empty_selection_skips_by_default():
    # A mistyped net class selects nothing: never a quiet pass.
    ctx = Mock()
    ctx.board.tracks = []
    ctx.board.nets = {"N"}
    ctx.board_geometry.return_value = NS(netclass_of={})

    result = TraceWidthCheck(netclass="SDD", min_width_mm=0.2).run(ctx)

    assert result.status.value == "skip"


def test_a_named_reference_missing_from_the_design_fails():
    ctx = Mock()
    ctx.sheet_symbols.return_value = [
        NS(reference="R1", on_board=True, footprint=NS(text=NS(value="")))
    ]

    result = FootprintMatchesSchematicCheck(references=["U99"], check_pins=False).run(ctx)

    assert result.is_fail
    assert result.details["missing_references"] == ["U99"]


def test_plain_params_object_does_not_crash_the_runner():
    from kicad_evaltor.checks.base import Check, CheckCategory, CheckResult

    class Plain:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Custom(Check):
        id = "custom.plain"
        name = description = "plain"
        category = CheckCategory.PROJECT
        Params = Plain

        def run(self, ctx):
            return CheckResult.pass_(self.id)

    report = Runner([Custom(limit=3)]).run(circuit())

    assert report.results[0].is_pass
    assert report.results[0].parameters == {"limit": 3}


def test_uncopyable_details_keep_the_verdict():
    import threading

    from kicad_evaltor.checks.base import CheckResult

    check = PinsConnectedCheck(pin_a="U1.1", pin_b="R1.1")
    lock = threading.Lock()
    check.run = lambda _ctx: CheckResult.pass_(check.id, lock=lock)

    result = Runner([check]).run(circuit()).results[0]

    assert result.is_pass
    assert result.details["lock"] is lock


@pytest.mark.parametrize(
    "check",
    [
        NetMembersCheck(net="A", pins=["U1.1"]),
        PinsConnectedCheck(pin_a="U1.1", pin_b="J1.1"),
    ],
)
def test_missing_netlist_is_the_check_s_own_error(check):
    from kicad_evaltor.schematic_file import NetlistUnavailable

    ctx = Mock()
    ctx.has_schematic.return_value = True
    ctx.schematic.get_netlist.side_effect = NetlistUnavailable("kicad-cli not found")

    result = check.run(ctx)

    assert result.status.value == "error"
    assert "Cannot read the netlist" in result.message
