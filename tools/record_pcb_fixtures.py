"""Record KiCad's DRC reports and pcbnew geometry for the example board.

The board checks' unit tests run without KiCad by replaying what KiCad said
about a real board: ``examples/demo_circuit/esp_data_logger_board``. Failing
cases are the same board with one thing broken, made on a copy with
:mod:`kicad_evaltor.edit`, so every recording is of a file KiCad wrote:

* ``base``      the board as it is (DRC-clean)
* ``unrouted``  every /SD_CLK track deleted
* ``parity``    R13 removed from the board
* ``overlap``   C5 moved onto C4
* ``offboard``  TP3 moved off the board
* ``tuning``    a custom rule (``.kicad_dru``) capping /SD_CLK at 10 mm and the
                SD bus skew at 5 mm, so KiCad's own length rules fire

It also records what the ``tests/test_release_*`` tests replay for the imu
design under ``tests/fixtures/boards/imu`` and its variants (``IMU_VARIANTS``,
again copies changed one way): pcbnew geometry (``geometry.json``, and
``texts_<variant>.json`` for variants that only add board text), kicad-cli
ERC/DRC reports (``report_*.json``) and the KiCad XML netlist.

Re-record after changing a board, the geometry dump, or upgrading KiCad::

    python tools/record_pcb_fixtures.py                  # everything
    python tools/record_pcb_fixtures.py --geometry-only  # no DRC/ERC reports
    python tools/record_pcb_fixtures.py --only imu       # the imu design only

``--geometry-only`` leaves the DRC recordings alone, for a change to the
geometry dump that cannot affect what DRC says.

``tests/test_pcb_integration.py`` re-runs KiCad and fails if a recording has
drifted from what KiCad now says.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

from kicad_evaltor.board_file import _net_name, net_codes
from kicad_evaltor.edit import (
    EditError,
    load_document,
    move_footprint,
    placed_symbols,
    remove_footprint,
    remove_net_tracks,
    save_document,
)
from kicad_evaltor.pcbnew_bridge import dump_board_json
from kicad_evaltor.sexpr import Quoted, SExpr, child, children, head, parse, value_of
from kicad_evaltor.toolchain import probe_toolchain, source_identity
from kicad_evaltor.utils.kicad_cli import find_kicad_cli, kicad_child_env

ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = ROOT / "examples" / "demo_circuit" / "esp_data_logger_board"
NAME = "esp_data_logger_board"
OUT = ROOT / "tests" / "fixtures" / "pcb"

VARIANTS: dict[str, Callable[[list], object]] = {
    "base": lambda _doc: None,
    "unrouted": lambda doc: remove_net_tracks(doc, "/SD_CLK"),
    "parity": lambda doc: remove_footprint(doc, "R13"),
    "overlap": lambda doc: move_footprint(doc, "C5", 109.2, 78.0),
    "offboard": lambda doc: move_footprint(doc, "TP3", 160.0, 92.85),
    "tuning": lambda _doc: None,
}

# Custom DRC rules written beside a variant's board, as a project's .kicad_dru.
RULES = {
    "tuning": """(version 1)
(rule sd_clk_length
  (condition "A.NetName == '/SD_CLK'")
  (constraint length (max 10mm)))
(rule sd_bus_skew
  (condition "A.hasNetclass('SD')")
  (constraint skew (max 5mm)))
""",
}
GEOMETRY_VARIANTS = ("base", "offboard")

BOARDS = ROOT / "tests" / "fixtures" / "boards"
IMU = BOARDS / "imu"
"""The imu-data-logger design every ``tests/test_release_*`` test runs on.

It is one agent's KiCad 10 output for the ESP32-C6 IMU data logger task
(USB-C, USBLC6 ESD array, BQ24074, TPS63001, LSM6DSL, microSD), with the zone
fills dropped (KiCad refills them) and the parts no test needs deleted from
board and schematic alike. The other field reports' cases are variants of it
(``IMU_VARIANTS``), each a copy changed one way."""

IMU_NAME = "imu"


def remove_track(
    document: list[SExpr], net: str, start: tuple[float, float], end: tuple[float, float]
) -> list[SExpr]:
    """Delete the one track segment of ``net`` between ``start`` and ``end`` (either way)."""
    codes = net_codes(document)

    def at(node: list[SExpr], key: str) -> tuple[float, float]:
        found = child(node, key)
        return (float(value_of(found, 1)), float(value_of(found, 2)))

    wanted = {start, end}
    doomed = [
        node
        for node in document
        if isinstance(node, list)
        and head(node) == "segment"
        and _net_name(child(node, "net"), codes) == net
        and {at(node, "start"), at(node, "end")} == wanted
    ]
    if len(doomed) != 1:
        raise EditError(f"{len(doomed)} segments of {net} run {start} to {end}")
    document.remove(doomed[0])
    return doomed[0]


def drop_to_back(
    document: list[SExpr], net: str, start: tuple[float, float], end: tuple[float, float]
) -> None:
    """Move one F.Cu segment of ``net`` to B.Cu, with a via at each end.

    The route keeps its path but changes layer twice, as a designer does to
    cross under another track.
    """
    segment = remove_track(document, net, start, end)
    back = deepcopy(segment)
    child(back, "layer")[1] = Quoted("B.Cu")
    child(back, "uuid")[1] = Quoted(str(uuid.uuid4()))
    document.append(back)
    for x, y in (start, end):
        document.append(
            [
                "via",
                ["at", _number(x), _number(y)],
                ["size", "0.6"],
                ["drill", "0.3"],
                ["layers", Quoted("F.Cu"), Quoted("B.Cu")],
                ["net", Quoted(net)],
                ["uuid", Quoted(str(uuid.uuid4()))],
            ]
        )


def move_board_text(document: list[SExpr], text: str, dx: float, dy: float) -> None:
    """Shift the board text that reads ``text`` by ``(dx, dy)`` mm."""
    found = [
        node
        for node in document
        if isinstance(node, list) and head(node) == "gr_text" and value_of(node, 1) == text
    ]
    if len(found) != 1:
        raise EditError(f"{len(found)} board texts read {text!r}")
    position = child(found[0], "at")
    if position is None:
        raise EditError(f"Board text {text!r} has no position")
    position[1] = _number(float(value_of(position, 1)) + dx)
    position[2] = _number(float(value_of(position, 2)) + dy)


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _node(text: str) -> list[SExpr]:
    found = parse(text)
    assert isinstance(found, list)
    return found


# Field report #9's text, copied from that board (``silk_text_edge``) with its
# size, stroke and uuid: there it sat at (65, 99.3) over the straight bottom
# edge at y 100. Moved by (46, 50) it sits over this board's bottom edge, y 150,
# in the gap between H3 and J1, so its ink ends the same 0.2672 mm inside.
LIPO_TEXT = """(gr_text "LiPo 1S" (at 111 149.3 0) (layer "F.SilkS")
  (uuid "0ed17b74-670a-4549-abf8-a53e895e62ae")
  (effects (font (size 0.8 0.8) (thickness 0.14))))"""

# The "LiPo 1S" ink ends 0.2672 mm above the board edge (pcbnew, and the
# kicad-cli SVG render): moved this far down, it ends 0.5 mm past the edge.
TEXT_PAST_EDGE_MM = 0.7672


def add_lipo_text(document: list[SExpr]) -> None:
    document.append(_node(LIPO_TEXT))


def strip_nickname(document: list[SExpr], prefix: str) -> None:
    """Save every footprint named ``prefix...`` without its library nickname."""
    for node in children(document, "footprint"):
        if value_of(node).startswith(prefix):
            node[1] = Quoted(value_of(node).split(":", 1)[1])


def count_via_height(project: dict) -> None:
    project["board"]["design_settings"]["rules"]["use_height_for_length_calcs"] = True


# --------------------------------------------------------- schematic edits

# U4's unused IO3-IO7, flagged on the pin in this design. Field report #10
# drew the flags 2.54 mm out, at the end of a wire stub from each pin.
STUB_PINS = {
    "6": (86.36, 198.12),
    "9": (86.36, 200.66),
    "10": (86.36, 203.2),
    "15": (86.36, 205.74),
    "16": (86.36, 208.28),
}
STUB_MM = 2.54


def _wire(a: tuple[float, float], b: tuple[float, float]) -> list[SExpr]:
    return [
        "wire",
        ["pts", ["xy", _number(a[0]), _number(a[1])], ["xy", _number(b[0]), _number(b[1])]],
        ["stroke", ["width", "0"], ["type", "default"]],
        ["uuid", Quoted(str(uuid.uuid4()))],
    ]


def _at(node: list[SExpr]) -> tuple[float, float]:
    found = child(node, "at")
    return (round(float(value_of(found, 1)), 4), round(float(value_of(found, 2)), 4))


def wire_stub_flags(document: list[SExpr]) -> None:
    """Move each flag on U4's unused IO pins to the end of a 2.54 mm stub."""
    flags = {_at(n): n for n in children(document, "no_connect")}
    for pin in STUB_PINS.values():
        if pin not in flags:
            raise EditError(f"No no-connect flag on the pin at {pin}")
        end = (round(pin[0] - STUB_MM, 4), pin[1])
        child(flags[pin], "at")[1:3] = [_number(end[0]), _number(end[1])]
        document.insert(document.index(flags[pin]), _wire(pin, end))


def join_stubs(document: list[SExpr]) -> None:
    """The stubs of U4.9 and U4.10 joined end to end: two pins, two flags, one net."""
    wire_stub_flags(document)
    a, b = STUB_PINS["9"], STUB_PINS["10"]
    document.append(_wire((a[0] - STUB_MM, a[1]), (b[0] - STUB_MM, b[1])))


# A T on U3's VOUT: the wire from U3.1 to C8 (the bar) and the wire down to
# U3.10 (the stem) meet at a junction, where the bar is split in two.
T_POINT = (337.82, 55.88)
T_BAR = ((332.74, 55.88), (345.44, 55.88))


def open_t(document: list[SExpr]) -> None:
    """Make the bar one wire through the T and drop its junction.

    The stem then ends on the middle of a wire with no junction, which KiCad
    does not connect (field report: ``silk_text_edge`` at R8 pin 1).
    """
    junctions = [n for n in children(document, "junction") if _at(n) == T_POINT]
    bar = []
    for node in children(document, "wire"):
        a, b = [
            (round(float(xy[1]), 4), round(float(xy[2]), 4))
            for xy in children(child(node, "pts"), "xy")
        ]
        if T_POINT in (a, b) and a[1] == b[1] == T_POINT[1]:
            bar.append(node)
    if len(junctions) != 1 or len(bar) != 2:
        raise EditError(f"T at {T_POINT}: {len(junctions)} junctions, {len(bar)} bar wires")
    document.remove(junctions[0])
    document.insert(document.index(bar[0]), _wire(*T_BAR))
    for node in bar:
        document.remove(node)


def _field(symbol: list[SExpr], name: str) -> list[SExpr]:
    for node in children(symbol, "property"):
        if value_of(node) == name:
            return node
    raise EditError(f"No {name} field")


def _symbol(document: list[SExpr], reference: str) -> list[SExpr]:
    (found,) = placed_symbols(document, reference)
    return found


def _place_field(
    document: list[SExpr],
    reference: str,
    name: str,
    offset: tuple[float, float],
    size: str,
    justify: str | None,
) -> None:
    """Put a field where another design drew it relative to its symbol."""
    symbol = _symbol(document, reference)
    x, y = _at(symbol)
    field = _field(symbol, name)
    child(field, "at")[1:3] = [_number(x + offset[0]), _number(y + offset[1])]
    effects = child(field, "effects")
    effects[:] = ["effects", ["font", ["size", size, size]]] + (
        [["justify", justify]] if justify else []
    )


# A GND symbol and an "SD_MISO" label as usb_pad_route_b drew them, the label
# 2.54 mm right of and below the symbol: their boxes overlap 2.69 x 0.67 mm.
# Moved by OVERLAP_SHIFT to an empty part of this sheet, touching no wire.
OVERLAP_SHIFT = (-60.96, 50.8)
B_GROUND_AT = (332.74, 200.66)
B_LABEL = """(label "SD_MISO" (at 335.28 203.2 180)
  (effects (font (size 1.27 1.27)) (justify right bottom))
  (uuid "0e0b36ea-f6b8-4423-b545-545008b60e4b"))"""
# silk_text_edge's "+3V3" label 6.35 mm below R23's origin, its text running up
# into the resistor; R13 is the same Device:R, unrotated, with that spot free.
SILK_LABEL = """(label "+3V3" (at 0 0 90)
  (effects (font (size 1.016 1.016)) (justify left bottom))
  (uuid "028f3fab-0e3f-50c2-8483-93b8c78e98df"))"""


def crowd_texts(document: list[SExpr]) -> None:
    """Text over symbols as two other agents drew it, for the overlap checks."""
    # silk_text_edge: D3's value 2.54 mm off its origin on an LED turned 180
    # degrees; this D3 is not turned, so the value goes 2.54 mm the other way.
    _place_field(document, "D3", "Value", (0.0, 2.54), "1.016", None)
    # silk_text_edge: C11's reference 3.81 right and 1.27 up, centred.
    _place_field(document, "C13", "Reference", (3.81, -1.27), "1.016", None)
    x, y = _at(_symbol(document, "R13"))
    label = _node(SILK_LABEL)
    child(label, "at")[1:3] = [_number(x), _number(y + 6.35)]
    child(label, "uuid")[1] = Quoted(str(uuid.uuid4()))
    document.append(label)
    # usb_pad_route_b's GND symbol (turned 270, fields hidden) and its label.
    ground = deepcopy(_symbol(document, "#PWR56"))
    gx, gy = B_GROUND_AT[0] + OVERLAP_SHIFT[0], B_GROUND_AT[1] + OVERLAP_SHIFT[1]
    child(ground, "at")[1:4] = [_number(gx), _number(gy), "270"]
    child(ground, "uuid")[1] = Quoted(str(uuid.uuid4()))
    for name in ("Reference", "Value"):
        field = _field(ground, name)
        if name == "Reference":
            field[2] = Quoted("#PWR60")
        child(field, "at")[1:3] = [_number(gx), _number(gy + (3.81 if name == "Value" else 0))]
        if child(field, "hide") is None:
            field.insert(4, ["hide", "yes"])
    for pin in children(ground, "pin"):
        child(pin, "uuid")[1] = Quoted(str(uuid.uuid4()))
    path = child(child(child(ground, "instances"), "project"), "path")
    child(path, "reference")[1] = Quoted("#PWR60")
    document.append(ground)
    label = _node(B_LABEL)
    lx, ly = _at(label)
    child(label, "at")[1:3] = [_number(lx + OVERLAP_SHIFT[0]), _number(ly + OVERLAP_SHIFT[1])]
    child(label, "uuid")[1] = Quoted(str(uuid.uuid4()))
    document.append(label)


def relink_schematic(document: list[SExpr]) -> None:
    """The library mix-ups of the field reports, made on the schematic.

    The LEDs' symbols and footprints moved to a ``Logger`` library the project
    does not configure, H1's symbol deleted (its footprint stays on the board),
    and C1's LCSC number changed on the schematic only.
    """
    library = child(document, "lib_symbols")
    stock = next(n for n in children(library, "symbol") if value_of(n) == "Device:LED")
    copied = deepcopy(stock)
    copied[1] = Quoted("Logger:LED")
    library.insert(library.index(stock) + 1, copied)
    for reference in ("D1", "D2", "D3"):
        symbol = _symbol(document, reference)
        child(symbol, "lib_id")[1] = Quoted("Logger:LED")
        _field(symbol, "Footprint")[2] = Quoted("Logger:LED_0603_1608Metric")
    document.remove(_symbol(document, "H1"))
    _field(_symbol(document, "C1"), "LCSC")[2] = Quoted("C999999")


@dataclass(frozen=True)
class Variant:
    """A copy of the imu design changed one way; ``None`` keeps a file as it is."""

    board: Callable[[list[SExpr]], object] | None = None
    schematic: Callable[[list[SExpr]], object] | None = None
    project: Callable[[dict], object] | None = None


IMU_VARIANTS: dict[str, Variant] = {
    "base": Variant(),
    # The track under U1 from pin 3 to pin 4: without it nothing joins them.
    "cut": Variant(
        board=lambda doc: remove_track(doc, "/USB_D+", (120.5375, 137.55), (120.5375, 139.25))
    ),
    # D+'s link from J1.A6 to J1.B6 dropped to B.Cu through two vias, with via
    # height counted in lengths (as usb_pad_route_b routed and set it).
    "via": Variant(
        board=lambda doc: drop_to_back(doc, "/USB_D+", (119.75, 142.9), (120.75, 142.9)),
        project=count_via_height,
    ),
    # Field report #9: "LiPo 1S" whose frame box crosses the edge, ink inside.
    "silk": Variant(board=add_lipo_text),
    "text_off": Variant(
        board=lambda doc: (
            add_lipo_text(doc),
            move_board_text(doc, "LiPo 1S", 0.0, TEXT_PAST_EDGE_MM),
        )
    ),
    # J2 pushed 0.8 mm onto the left edge: its silkscreen crosses it (DRC
    # silk_edge_clearance), its pads still hold their track ends.
    "j2_edge": Variant(board=lambda doc: move_footprint(doc, "J2", 102.8, 123.0)),
    # Field report #10: no-connect flags at the end of wire stubs.
    "stubs": Variant(schematic=wire_stub_flags),
    "stubs_joined": Variant(schematic=join_stubs),
    "t_open": Variant(schematic=open_t),
    "overlaps": Variant(schematic=crowd_texts),
    "relinked": Variant(
        board=lambda doc: strip_nickname(doc, "Resistor_SMD:"), schematic=relink_schematic
    ),
}

GEOMETRY_TEXTS = ("silk", "text_off")
"""Variants whose geometry recording keeps only the board texts: nothing else
differs from ``base``. Every other variant changes no pad, text or outline,
so its tests replay the base recording."""


def make_imu_variant(variant: str, folder: Path) -> Path:
    """Copy the imu design into ``folder``, change it as ``variant`` says; the board path."""
    change = IMU_VARIANTS[variant]
    folder.mkdir(parents=True, exist_ok=True)
    for suffix, edit in ((".kicad_pcb", change.board), (".kicad_sch", change.schematic)):
        source = IMU / f"{IMU_NAME}{suffix}"
        if edit is None:
            shutil.copy(source, folder / source.name)
        else:
            document = load_document(source)
            edit(document)
            save_document(document, folder / source.name)
    project = json.loads((IMU / f"{IMU_NAME}.kicad_pro").read_text("utf-8"))
    if change.project is not None:
        change.project(project)
    (folder / f"{IMU_NAME}.kicad_pro").write_text(json.dumps(project, indent=2) + "\n", "utf-8")
    return folder / f"{IMU_NAME}.kicad_pcb"


def _kicad_report(cli: Path, kind: str, design: Path) -> dict:
    report = design.with_name(f"{kind}.json")
    command = ["sch", "erc"] if kind == "erc" else ["pcb", "drc", "--schematic-parity"]
    subprocess.run(
        [str(cli), *command, "--format", "json", "--severity-all"]
        + (["--refill-zones"] if kind == "drc" else [])
        + ["-o", str(report), str(design)],
        check=True,
        capture_output=True,
        cwd=design.parent,
        env=kicad_child_env(),
    )
    data: dict = json.loads(report.read_text(encoding="utf-8"))
    data["source"] = design.name
    data.pop("date", None)
    return data


# The kicad-cli reports the findings tests read: (variant, tool, file name).
IMU_REPORTS = [
    ("base", "erc", "report_erc.json"),
    ("base", "drc", "report_drc.json"),
    ("j2_edge", "drc", "report_j2_edge_drc.json"),
    ("relinked", "erc", "report_relinked_erc.json"),
    ("relinked", "drc", "report_relinked_drc.json"),
]


def record_imu(cli: Path, scratch: Path, geometry_only: bool = False) -> None:
    base: dict = {}
    for variant in ("base", *GEOMETRY_TEXTS):
        board = make_imu_variant(variant, scratch / "imu" / variant)
        geometry = dump_board_json(board, fill_zones=True)
        if variant == "base":
            base = geometry
            out = IMU / "geometry.json"
            out.write_text(json.dumps(geometry, sort_keys=True) + "\n", "utf-8")
        else:
            others = {k: v for k, v in geometry.items() if k != "texts"}
            if others != {k: v for k, v in base.items() if k != "texts"}:
                raise SystemExit(f"{variant}: more than the board texts changed")
            out = IMU / f"texts_{variant}.json"
            out.write_text(json.dumps({"texts": geometry["texts"]}, sort_keys=True) + "\n", "utf-8")
        print(f"imu/{variant}: geometry, {geometry['unconnected']} unconnected")
    if geometry_only:
        return
    for variant, kind, name in IMU_REPORTS:
        board = make_imu_variant(variant, scratch / "imu" / variant)
        design = board if kind == "drc" else board.with_suffix(".kicad_sch")
        data = _kicad_report(cli, kind, design)
        (IMU / name).write_text(json.dumps(data, indent=1) + "\n", "utf-8")
        print(f"imu/{variant}: {name}")
    # Exported from a copy: kicad-cli writes a .kicad_prl beside the schematic.
    board = make_imu_variant("base", scratch / "imu" / "netlist")
    netlist = board.with_name("netlist.xml")
    subprocess.run(
        [
            str(cli),
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "-o",
            str(netlist),
            str(board.with_suffix(".kicad_sch")),
        ],
        check=True,
        capture_output=True,
        env=kicad_child_env(),
    )
    text = netlist.read_text("utf-8")
    source = (IMU / f"{IMU_NAME}.kicad_sch").relative_to(ROOT).as_posix()
    text = re.sub(r"<source>[^<]*\.kicad_sch</source>", f"<source>{source}</source>", text, count=1)
    (IMU / f"netlist_{IMU_NAME}.xml").write_text(text, "utf-8")
    print("imu: netlist")


def make_variant(name: str, folder: Path) -> Path:
    """Copy the example project into ``folder`` and break it as ``name`` says."""
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copy(BOARD_DIR / f"{NAME}{suffix}", folder / f"{NAME}{suffix}")
    if name in RULES:
        (folder / f"{NAME}.kicad_dru").write_text(RULES[name], encoding="utf-8")
    document = load_document(BOARD_DIR / f"{NAME}.kicad_pcb")
    VARIANTS[name](document)
    return save_document(document, folder / f"{NAME}.kicad_pcb")


def run_drc(cli: Path, board: Path) -> dict:
    report = board.with_name("drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--schematic-parity",
            "--refill-zones",
            "-o",
            str(report),
            str(board),
        ],
        check=True,
        capture_output=True,
        cwd=board.parent,
        env=kicad_child_env(),
    )
    data: dict = json.loads(report.read_text(encoding="utf-8"))
    # Absolute temp paths and the date change on every run; they say nothing.
    data["source"] = f"{NAME}.kicad_pcb"
    data.pop("date", None)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=("example", "imu"), default=None)
    parser.add_argument("--geometry-only", action="store_true")
    args = parser.parse_args(argv)
    cli = find_kicad_cli()
    if cli is None:
        print("kicad-cli not found", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kicad-evaltor-record-") as scratch:
        if args.only != "example":
            record_imu(cli, Path(scratch), geometry_only=args.geometry_only)
        if args.only == "imu":
            return 0
        for name in VARIANTS:
            if args.geometry_only and name not in GEOMETRY_VARIANTS:
                continue
            board = make_variant(name, Path(scratch) / name)
            if not args.geometry_only:
                drc = run_drc(cli, board)
                (OUT / f"{name}.drc.json").write_text(json.dumps(drc, indent=1) + "\n", "utf-8")
                counts = {
                    k: len(drc.get(k, []))
                    for k in ("violations", "unconnected_items", "schematic_parity")
                }
                print(f"{name}: {counts}")
            if name in GEOMETRY_VARIANTS:
                geometry = dump_board_json(board, fill_zones=True)
                (OUT / f"{name}.geometry.json").write_text(
                    json.dumps(geometry, sort_keys=True) + "\n", "utf-8"
                )
                print(f"{name}: geometry")
    if args.geometry_only:
        return 0
    identities = []
    for path in sorted(BOARD_DIR.glob(f"{NAME}.kicad_*")):
        if path.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro"):
            identity = source_identity(path)
            identity["path"] = path.relative_to(ROOT).as_posix()
            identities.append(identity)
    manifest = {
        "toolchain": asdict(probe_toolchain(str(cli), include_pcbnew=True, timeout=60)),
        "sources": identities,
        "variants": list(VARIANTS),
        "artifacts": {
            p.name: source_identity(p)["sha256"]
            for p in sorted(OUT.glob("*.json"))
            if p.name != "manifest.json"
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
