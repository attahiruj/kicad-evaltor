"""0.6.1 schematic fixes, each measured on a real KiCad project.

The design is the real imu-data-logger project under
``tests/fixtures/boards/imu``. A failing case is a copy of it changed one way
by ``tools/record_pcb_fixtures.py`` (``IMU_VARIANTS``), reproducing what
another agent's design of the same task drew, never a schematic written from
scratch, and every connectivity claim is checked against KiCad's own ERC or
netlist in the integration tests.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from kicad_evaltor.checks.base import TestStatus as Status
from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.checks.schematic.overlaps import OverlapParams
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.drawing_topology import LabelOnlyLink, label_only_links, wire_groups
from kicad_evaltor.schematic_file import load_schematic
from kicad_evaltor.schematic_items import extract

IMU = Path(__file__).parent / "fixtures" / "boards" / "imu"
EXAMPLES = Path(__file__).parent.parent / "examples" / "demo_circuit"
TOOLS = Path(__file__).parent.parent / "tools"

NO_CONNECT = "sch.noconnect.floating"
TEXT_TEXT = "sch.layout.text_text_overlap"
TEXT_SYMBOL = "sch.layout.text_symbol_overlap"
SYMBOL_SYMBOL = "sch.layout.symbol_symbol_overlap"

# The five flags the stubs variant draws at the far end of a 2.54 mm stub from
# U4's unused IO3-IO7, as nc_wire_stub drew them for its unused pins.
STUB_FLAGS = {
    (83.82, 198.12): "U4.6",
    (83.82, 200.66): "U4.9",
    (83.82, 203.2): "U4.10",
    (83.82, 205.74): "U4.15",
    (83.82, 208.28): "U4.16",
}
# Joined end to end in stubs_joined.
JOINED = {(83.82, 200.66), (83.82, 203.2)}


def recorder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "record_pcb_fixtures", TOOLS / "record_pcb_fixtures.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def board() -> Path:
    path = IMU / "imu.kicad_sch"
    if not path.exists():
        pytest.skip("the real imu design is not checked out")
    return path


def variant(tmp_path: Path, name: str) -> Path:
    """The imu schematic changed as ``IMU_VARIANTS[name]`` says."""
    board()
    built: Path = recorder().make_imu_variant(name, tmp_path / name)
    return built.with_suffix(".kicad_sch")


def t_with_junction(tmp_path: Path) -> Path:
    """The open T with a junction added where the stem meets the bar."""
    sch = variant(tmp_path, "t_open")
    junction = (
        f"(junction (at {T_POINT[0]} {T_POINT[1]}) (diameter 0) (color 0 0 0 0) "
        f'(uuid "{uuid.uuid4()}"))\n'
    )
    text = sch.read_text(encoding="utf-8")
    before = "(wire"
    assert before in text
    sch.write_text(text.replace(before, junction + before, 1), encoding="utf-8")
    return sch


def run(check_id: str, path: Path, **params):
    return CheckRegistry.create(check_id, sheet="all", **params).run(
        DesignContext(schematic_path=path)
    )


def erc(path: Path) -> list[dict]:
    result = CheckRegistry.create("sch.erc", severity="all").run(DesignContext(schematic_path=path))
    return result.details.get("violations", [])


# ------------------------------------------------- #10 no-connect on a wire stub


class TestNoConnectWireStub:
    def test_flag_at_the_end_of_a_stub_marks_its_pin(self, tmp_path):
        result = run(NO_CONNECT, variant(tmp_path, "stubs"))
        assert result.status is Status.PASS
        stubs = {tuple(s["at"]): s["pin"] for s in result.details["stub_flags"]}
        assert stubs == STUB_FLAGS

    def test_allow_wire_stub_false_keeps_the_old_rule(self, tmp_path):
        result = run(NO_CONNECT, variant(tmp_path, "stubs"), allow_wire_stub=False)
        assert result.status is Status.FAIL
        assert {tuple(f) for f in result.details["floating_flags"]} == set(STUB_FLAGS)
        assert result.details["stub_flags"] == []

    def test_flag_on_a_wire_joining_two_pins_is_still_reported(self, tmp_path):
        result = run(NO_CONNECT, variant(tmp_path, "stubs_joined"))
        assert result.status is Status.FAIL
        assert {tuple(f) for f in result.details["floating_flags"]} == JOINED
        assert {tuple(s["at"]) for s in result.details["stub_flags"]} == set(STUB_FLAGS) - JOINED

    def test_a_board_with_flags_on_pins_is_unchanged(self):
        result = run(NO_CONNECT, board())
        assert result.status is Status.PASS
        assert result.details["stub_flags"] == []

    def test_allow_wire_stub_must_be_a_bool(self):
        with pytest.raises(TypeError, match="allow_wire_stub"):
            CheckRegistry.create(NO_CONNECT, allow_wire_stub="yes")

    @pytest.mark.integration
    def test_erc_agrees_on_stub_flags(self, kicad_cli_required, tmp_path):
        # KiCad accepts the stub flags: no no-connect finding of any kind.
        assert not [v for v in erc(variant(tmp_path, "stubs")) if "no_connect" in v["type"]]
        # Joining two stubs puts two pins on a flagged net, which ERC rejects.
        joined = [v for v in erc(variant(tmp_path, "stubs_joined")) if "no_connect" in v["type"]]
        assert joined, "ERC should reject a flag on a wire joining two pins"


# ------------------------------------------------------- #11 overlap min depth


# The base design, the overlaps variant, and a bundled demo sheet.
SHEETS = ["base", "overlaps", "visual"]


def sheet(name: str, tmp_path: Path) -> Path:
    if name == "visual":
        return EXAMPLES / "visual.kicad_sch"
    return board() if name == "base" else variant(tmp_path, name)


def pairs(result) -> set[tuple[str, str]]:
    return {(o["first"], o["second"]) for o in result.details.get("overlaps", [])}


class TestOverlapMinimumDepth:
    """On the overlaps variant: text placed over symbols as two other agents drew it.

    usb_pad_route_b's "SD_MISO" label over a GND symbol (its boxes 2.69 x
    0.67 mm deep), silk_text_edge's "+3V3" label running up into a resistor,
    its D3 value against the LED and its C11 reference just clear of the
    capacitor (here on R13, D3 and C13).
    """

    @pytest.mark.parametrize("check_id", [TEXT_TEXT, TEXT_SYMBOL, SYMBOL_SYMBOL])
    @pytest.mark.parametrize("name", SHEETS)
    def test_min_overlap_is_a_negative_half_margin(self, tmp_path, check_id, name):
        # Without a clearance to add near misses back, the two spellings keep
        # exactly the same pairs.
        path = sheet(name, tmp_path)
        by_margin = run(check_id, path, margin=-0.3, clearance=0.0)
        by_depth = run(check_id, path, min_overlap_mm=0.6, clearance=0.0)
        assert pairs(by_margin) == pairs(by_depth)

    @pytest.mark.parametrize("check_id", [TEXT_TEXT, TEXT_SYMBOL, SYMBOL_SYMBOL])
    @pytest.mark.parametrize("name", SHEETS)
    def test_every_finding_is_at_least_that_deep(self, tmp_path, check_id, name):
        result = run(check_id, sheet(name, tmp_path), min_overlap_mm=0.6)
        assert result.details["min_overlap_mm"] == 0.6
        for o in result.details.get("overlaps", []):
            assert o["overlap_width_mm"] >= 0.6
            assert o["overlap_height_mm"] >= 0.6
            assert o["gap"] == 0.0

    def test_shallow_box_hits_drop_and_a_deep_one_stays(self, tmp_path):
        path = variant(tmp_path, "overlaps")
        everything = run(TEXT_SYMBOL, path)
        deep = run(TEXT_SYMBOL, path, min_overlap_mm=0.6)
        assert everything.details["count"] == 4
        assert pairs(deep) == {("SD_MISO", "#PWR60")}
        (finding,) = deep.details["overlaps"]
        # The same as on usb_pad_route_b, where the pair was SD_MISO / #PWR047.
        assert finding["overlap_width_mm"] == pytest.approx(2.6924, abs=1e-3)
        assert finding["overlap_height_mm"] == pytest.approx(0.6652, abs=1e-3)

    def test_box_findings_report_their_drawn_overlap(self, tmp_path):
        result = run(TEXT_SYMBOL, variant(tmp_path, "overlaps"))
        by_pair = {(o["first"], o["second"]): o for o in result.details["overlaps"]}
        # A real overlap reports its shared region; a near miss reports none.
        # Both measure as they did on silk_text_edge (+3V3 / R23, C11).
        assert by_pair[("+3V3", "R13")]["overlap_width_mm"] == pytest.approx(0.807, abs=1e-3)
        assert by_pair[("+3V3", "R13")]["overlap_height_mm"] == pytest.approx(0.364, abs=1e-3)
        assert by_pair[("C13.Reference", "C13")]["overlap_width_mm"] == 0.0
        assert by_pair[("C13.Reference", "C13")]["gap"] > 0

    def test_the_base_design_has_no_text_over_a_symbol(self):
        assert run(TEXT_SYMBOL, board()).details.get("count", 0) == 0

    def test_min_overlap_rejects_a_margin_render_and_negatives(self):
        with pytest.raises(ValueError, match="min_overlap_mm"):
            OverlapParams(min_overlap_mm=0.6, margin=-0.3).validate()
        with pytest.raises(ValueError, match="min_overlap_mm"):
            OverlapParams(min_overlap_mm=0.6, method="render").validate()
        with pytest.raises(ValueError, match="min_overlap_mm"):
            OverlapParams(min_overlap_mm=-0.1).validate()
        with pytest.raises(TypeError, match="min_overlap_mm"):
            OverlapParams(min_overlap_mm="0.6").validate()

    @pytest.mark.integration
    def test_box_depth_is_not_visibility(self, kicad_cli_required, tmp_path):
        """What the render says about the pairs a 0.6mm minimum keeps and drops.

        The kept pair really is drawn on top: its ink meets. Of the dropped ones,
        D3's value never touches D3's ink, but the +3V3 text does touch R13's
        pin ink though their boxes share only 0.36mm of height. A box depth is a
        heuristic; ``method="render"`` is the test of what is visible.
        """
        from kicad_evaltor.ink import measure_overlap

        def ink(ctx, finding):
            return measure_overlap(ctx, finding, include_own_pins=False, resolution_mm=0.01)

        ctx = DesignContext(schematic_path=variant(tmp_path, "overlaps"))
        everything = CheckRegistry.create(TEXT_SYMBOL).run(ctx)
        deep = CheckRegistry.create(TEXT_SYMBOL, min_overlap_mm=0.6).run(ctx)
        (kept,) = deep.details["overlaps"]
        assert ink(ctx, kept).intersects

        by_pair = {(o["first"], o["second"]): o for o in everything.details["overlaps"]}
        assert not ink(ctx, by_pair[("D3.Value", "D3")]).intersects
        assert ink(ctx, by_pair[("+3V3", "R13")]).intersects


# --------------------------------------------- S T-joins and label-only links


# The T the t_open variant leaves without a junction: the stem from U3.10
# ends on the middle of the bar from U3.1 to C8 (silk_text_edge drew its T at
# R8 pin 1 this way).
T_POINT = (337.82, 55.88)
T_STEM = ((337.82, 63.5), (337.82, 55.88))
T_BAR = ((332.74, 55.88), (345.44, 55.88))


def wire_index(scene, ends) -> int:
    return next(i for i, w in enumerate(scene.wires) if (w.start, w.end) == ends)


class TestTJoins:
    def test_wire_groups_joins_a_t_by_default(self, tmp_path):
        scene = extract(load_schematic(variant(tmp_path, "t_open")))
        groups = wire_groups(scene)
        assert groups.wires[wire_index(scene, T_STEM)] == groups.wires[wire_index(scene, T_BAR)]
        assert groups.physically_joined(("U3", "10"), ("U3", "1"))

    def test_kicad_rule_needs_a_junction_on_a_t(self, tmp_path):
        scene = extract(load_schematic(variant(tmp_path, "t_open")))
        groups = wire_groups(scene, join_mid_wire=False)
        assert groups.wires[wire_index(scene, T_STEM)] != groups.wires[wire_index(scene, T_BAR)]
        assert groups.physically_joined(("U3", "1"), ("C8", "1"))
        assert not groups.physically_joined(("U3", "10"), ("U3", "1"))

    def test_a_junction_joins_the_t_under_either_rule(self, tmp_path):
        scene = extract(load_schematic(t_with_junction(tmp_path)))
        for rule in (True, False):
            groups = wire_groups(scene, join_mid_wire=rule)
            assert groups.physically_joined(("U3", "10"), ("U3", "1"))

    @pytest.mark.integration
    def test_kicad_netlist_agrees(self, kicad_cli_required, tmp_path):
        netlist = load_schematic(variant(tmp_path / "open", "t_open")).get_netlist()
        assert netlist.net_of("U3", "10") != netlist.net_of("U3", "1")
        joined = load_schematic(t_with_junction(tmp_path / "joined")).get_netlist()
        assert joined.net_of("U3", "10") == joined.net_of("U3", "1")

    @pytest.mark.integration
    @pytest.mark.parametrize("name", ["base", "stubs", "t_open"])
    def test_kicad_rule_groups_never_span_two_nets(self, kicad_cli_required, tmp_path, name):
        schematic = load_schematic(board() if name == "base" else variant(tmp_path, name))
        netlist = schematic.get_netlist()
        groups = wire_groups(extract(schematic), join_mid_wire=False)
        nets: dict[int, set[str | None]] = {}
        for (ref, pin), root in groups.pins.items():
            nets.setdefault(root, set()).add(netlist.net_of(ref, pin))
        assert all(len(found) == 1 for found in nets.values())


class TestLabelOnlyLinks:
    def test_pairs_are_parts_no_drawn_group_holds_together(self):
        link = LabelOnlyLink("+BATT", (("C3.1", "J2.1", "U2.2"), ("R9.1",)))
        assert link.parts == (frozenset({"C3", "J2", "U2"}), frozenset({"R9"}))
        assert link.pairs == (("C3", "R9"), ("J2", "R9"), ("R9", "U2"))

    def test_a_part_wired_through_any_group_is_not_paired(self):
        link = LabelOnlyLink("X", (("R1.1", "U1.2"), ("R1.2", "U2.1")))
        assert link.pairs == (("U1", "U2"),)

    @pytest.mark.integration
    def test_imu_design(self, kicad_cli_required):
        # The same links nc_wire_stub had.
        schematic = load_schematic(board())
        links = {
            link.net: link for link in label_only_links(extract(schematic), schematic.get_netlist())
        }
        assert links["+BATT"].groups == (("C3.1", "J2.1", "U2.2"), ("R9.1",))
        assert links["/IMU_INT1"].pairs == (("U4", "U5"),)
        # The I2C pull-up is wired to the IMU, and reaches the MCU by label.
        assert links["/I2C_SDA"].pairs == (("R15", "U4"), ("U4", "U5"))
        # A net drawn as one group is not a link.
        for net in links.values():
            assert len(net.groups) >= 2

    @pytest.mark.integration
    def test_a_part_reached_only_by_label(self, kicad_cli_required):
        schematic = load_schematic(board())
        links = {
            link.net: link for link in label_only_links(extract(schematic), schematic.get_netlist())
        }
        # The MCU's D+ pin has no drawn wire to the connector or the ESD part
        # (silk_text_edge had its series resistor R16 drawn this way).
        assert ("U4.18",) in links["/USB_D+"].groups
        assert ("J1", "U4") in links["/USB_D+"].pairs
        # U1 is drawn to J1 through one of its pins, so the two are not paired.
        assert ("J1", "U1") not in links["/USB_D+"].pairs
