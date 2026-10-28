# kicad-evaltor

A Python library for validating KiCad designs programmatically. Describe the
properties a design is supposed to have, reference designators, values,
connectivity, footprint assignment, ERC and DRC results, routing, placement,
copper and fabrication outputs, and whether the sheet is readable, and run them
as a test suite.

You define the tests, thresholds, and acceptance policy. Evaltor also exposes measurements and queries that work directly in your own pytest tests, without a runner.

Works against **KiCad 10 and later**. It does not need the KiCad IPC API server,
which KiCad 10 removed, or a running KiCad.

[![CI](https://github.com/attahiruj/kicad-evaltor/actions/workflows/ci.yml/badge.svg)](https://github.com/attahiruj/kicad-evaltor/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kicad-evaltor.svg)](https://pypi.org/project/kicad-evaltor/)
[![Python](https://img.shields.io/pypi/pyversions/kicad-evaltor.svg)](https://pypi.org/project/kicad-evaltor/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Installation

```bash
pip install kicad-evaltor
```

### What you need installed

Most checks need nothing but the file itself. The schematic reader parses
`.kicad_sch` directly, so symbols, fields, wires, labels and text geometry all
work with no KiCad present. The board reader does the same for `.kicad_pcb`:
footprints, pads, tracks, vias, net classes and the stackup.

Where KiCad already has the answer, the library asks KiCad instead of guessing:

| Check | Needs |
|-------|-------|
| `sch.components.connected`, `sch.pins.connected`, `sch.net.members`, `sch.regulator.output_voltage`, `sch.pair.polarity`, `pcb.footprint.matches_schematic` | `kicad-cli sch export netlist` |
| `sch.erc` | `kicad-cli sch erc` |
| `pcb.drc`, `pcb.routing.complete`, `pcb.schematic_parity`, `pcb.footprint.overlap`, `pcb.footprint.matches_library`, `pcb.silk.over_copper`, `pcb.silk.overlap`, `pcb.tuning.rules` | `kicad-cli pcb drc` (run once per context, shared) |
| `pcb.fab.exports`, `pcb.fab.position_file` | `kicad-cli pcb export` |
| `pcb.board.outline`, `pcb.footprint.on_board`, `pcb.footprint.near`, `pcb.power.pour`, `pcb.plane.coverage`, `pcb.thermal.vias`, `pcb.text.off_board`; also `pcb.trace.length` pad to pad, `pcb.trace.width` by net class, `pcb.diff_pair` with gap or width from the net class, `pcb.footprint.placement` with `at_edge`, `pcb.via.rules` with `allow_via_in_pad=False` | `pcbnew`, run under KiCad's bundled Python |
| `sch.symbol.matches_library` | KiCad's symbol libraries (found next to `kicad-cli`, or via `KICAD10_SYMBOL_DIR`) |

Without them those checks report an error and the rest of the suite still runs.
On Windows and macOS, KiCad is not put on `PATH`; `DesignContext` finds
`kicad-cli` and KiCad's Python in the standard install folders, or pass
`kicad_cli_path="C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"`. Set
`KICAD_PYTHON` to use a different interpreter that can import `pcbnew`.

## Quick start

```python
from kicad_evaltor import (
    ComponentValueCheck,
    DesignContext,
    ERCRunCheck,
    TestRunner,
    TextTextOverlapCheck,
)

with DesignContext(project_path="myproject.kicad_pro", headless=True) as ctx:
    report = TestRunner(
        [
            ComponentValueCheck(reference="R1", expected="10k"),
            ERCRunCheck(severity="error"),
            TextTextOverlapCheck(),
        ]
    ).run(ctx)

    print(report.summary())
    assert report.all_passed
```

A complete, runnable example lives in
[`examples/simple_circuit.py`](examples/simple_circuit.py), which runs
the schematic checks against
[`examples/demo_circuit/simple_circuit.kicad_sch`](examples/demo_circuit/simple_circuit.kicad_sch).
It exits non-zero if the design stops matching its documented expectations, so it
doubles as a smoke test:

```bash
python examples/simple_circuit.py
```

[`examples/hierarchy.py`](examples/hierarchy.py) does the same for a hierarchical
design, [`examples/demo_circuit/hierarchy/`](examples/demo_circuit/hierarchy/): a
root sheet with the buttons and status LED, and a `shared` sheet below it holding
the power stage and MCU. Its checks find parts on either sheet by reference, follow
nets across the sheet block's pins, and name each layout finding's sheet. KiCad
agrees on the design: 41 parts across `/` and `/shared/`, and a clean ERC.

```bash
python examples/hierarchy.py
```

[`examples/visual.py`](examples/visual.py) does the same for the
layout checks, against
[`examples/demo_circuit/visual.kicad_sch`](examples/demo_circuit/visual.kicad_sch).
It runs every `sch.layout.*` and `sch.noconnect.*` check the registry knows and
lists each finding with the properties and coordinates involved. It is a report,
not a test: it exits 0 even when the checks find defects. It needs no KiCad at
all, and takes any sheet as an argument:

```bash
python examples/visual.py                                 # human report
python examples/visual.py --json                          # the same failures as JSON
python examples/visual.py path/to/your.kicad_sch          # any other sheet
```

[`examples/pcb.py`](examples/pcb.py) runs the board checks against a real
KiCad 10 board,
[`examples/demo_circuit/esp_data_logger_board/`](examples/demo_circuit/esp_data_logger_board/):
an ESP32-C6 data logger whose DRC is clean. It also prints four things on that
board that DRC does not report: tracks narrower than their net class asks for, a
thermal pad with no vias, a power pin beyond the chosen decoupling distance,
and hidden reference designators.

```bash
python examples/pcb.py
```

## Checks

### Schematic

| Check ID | Class | Description |
|----------|-------|-------------|
| `sch.component.exists` | `ComponentExistsCheck` | A component exists, by reference or `lib_id` pattern |
| `sch.component.value` | `ComponentValueCheck` | A component's value matches, case-insensitively, or as a number within a tolerance (`numeric=True`: `4k7` equals `4.7k`) |
| `sch.component.property` | `ComponentPropertyCheck` | An arbitrary field on a component matches |
| `sch.components.connected` | `ComponentsConnectedCheck` | Two components share at least N nets, optionally counting through series passives |
| `sch.pins.connected` | `PinsConnectedCheck` | Two specific pins are connected (or must not be), crossing only the internal pin pairs you name |
| `sch.net.members` | `NetMembersCheck` | A net contains the pins you list, and with `exact=True` (the default) nothing else |
| `sch.footprint.assigned` | `FootprintAssignedCheck` | Footprints are assigned, optionally allowing `None` |
| `sch.symbol.in_library` | `SymbolInLibraryCheck` | A symbol exists in the project libraries |
| `sch.symbol.matches_library` | `SymbolMatchesLibraryCheck` | Placed symbols are unchanged copies of their library symbols (pins and body; fields ignored) |
| `sch.regulator.output_voltage` | `RegulatorOutputCheck` | A linear regulator's output, worked out from its part and divider, is the expected voltage |
| `sch.pair.polarity` | `PairPolarityCheck` | Each half of a differential pair (USB, CAN, `+`/`-`) reaches the same half at the far part, never the other |
| `sch.erc` | `ERCRunCheck` | Runs ERC via `kicad-cli` and filters by severity; `timeout` sets how long it may run |

### Board

Board checks read the `.kicad_pcb` for what it stores as values and ask KiCad
for the rest. DRC runs once per context, and every DRC-backed check reads its
own findings from that one report. Geometry (courtyards, pad shapes, the board
outline, zone fill) comes from `pcbnew`, also once per context.

| Check ID | Class | Description |
|----------|-------|-------------|
| `pcb.drc` | `DRCRunCheck` | KiCad's DRC: rule violations, **unrouted connections** and **schematic parity**, filtered by severity and `include_types` / `exclude_types` |
| `pcb.routing.complete` | `RoutingCompleteCheck` | Nothing is left unrouted, grouped by net |
| `pcb.schematic_parity` | `SchematicParityCheck` | The board matches its schematic: missing or extra footprints, mismatched values, net conflicts |
| `pcb.footprint.exists` | `FootprintExistsCheck` | A footprint exists, by reference or `lib_id` |
| `pcb.footprint.matches_schematic` | `FootprintMatchesSchematicCheck` | Each footprint is the one its symbol names, with a pad for every pin |
| `pcb.footprint.matches_library` | `FootprintMatchesLibraryCheck` | Footprints are unchanged copies of their library footprints |
| `pcb.footprint.overlap` | `FootprintOverlapCheck` | No courtyards overlap, optionally with a minimum gap |
| `pcb.footprint.on_board` | `FootprintOnBoardCheck` | Every courtyard lies inside the board outline |
| `pcb.footprint.placement` | `FootprintPlacementCheck` | A part's side, rotation, position, or closeness to the board edge |
| `pcb.footprint.near` | `FootprintNearCheck` | A part is within a distance of another part or pin; `decoupling="U1"` pairs each power-input pin with a capacitor itself |
| `pcb.board.outline` | `BoardOutlineCheck` | `Edge.Cuts` is closed and the right size |
| `pcb.mounting_holes` | `MountingHolesCheck` | Number, drill size and edge distance of mounting holes |
| `pcb.trace.width` | `TraceWidthCheck` | Track widths on a net, a part's nets or a net class; `match_netclass=True` holds each track to its own class |
| `pcb.trace.length` | `TraceLengthCheck` | A net's length, or the shortest route between two pads, within min/max or a target ± tolerance |
| `pcb.trace.current_capacity` | `CurrentCapacityCheck` | Tracks are wide enough for a current (IPC-2221, as in KiCad's calculator) |
| `pcb.net.length_match` | `LengthMatchCheck` | A pair or bus of nets is length matched (max skew, and/or a target length ± tolerance) |
| `pcb.diff_pair` | `DiffPairCheck` | Differential pairs, found by name as KiCad finds them (`+`/`-`, `P`/`N`, or your own suffixes): skew, track width, coupled gap, uncoupled length, via count |
| `pcb.tuning.rules` | `TuningRulesCheck` | KiCad's own length, skew and diff-pair DRC rules from a `.kicad_dru` file |
| `pcb.net.vias` | `NetViasCheck` | How many vias a net uses |
| `pcb.net.layers` | `NetLayersCheck` | Which copper layers a net is routed on |
| `pcb.via.rules` | `ViaRulesCheck` | Via drill, diameter and annular ring (defaulting to the board's own rules), and via-in-pad |
| `pcb.thermal.vias` | `ThermalViasCheck` | Vias inside a part's thermal pad |
| `pcb.power.pour` | `PowerPourCheck` | A net has a pour that is filled, big enough, and connected to its pads |
| `pcb.plane.coverage` | `PlaneCoverageCheck` | How much of a layer a net's pour covers |
| `pcb.silk.reference_visible` | `ReferenceVisibleCheck` | Every footprint prints a readable reference on silkscreen |
| `pcb.silk.over_copper` | `SilkOverCopperCheck` | No silkscreen is clipped by pads or the board edge |
| `pcb.silk.overlap` | `SilkOverlapCheck` | No silkscreen items overlap |
| `pcb.text.off_board` | `TextOffBoardCheck` | No silkscreen text hangs off the board |
| `pcb.fab.exports` | `FabExportsCheck` | Gerbers (every copper layer and the edge), drill and position files can be made |
| `pcb.fab.position_file` | `PositionFileCheck` | The pick-and-place file lists every SMD part |
| `project.schematic_pcb_consistency` | `ConsistencyCheck` | Every schematic part is on the board, and the reverse |

DRC-backed checks refill zones first by default (`refill_zones=True`), as
KiCad's DRC dialog does; the file is never rewritten.

#### Lengths and differential pairs

Lengths are counted the way KiCad's DRC and length tuning count them: track
length (arcs as arcs), plus each via's height when the project has "use via
height in length" on (the default), plus pad-to-die lengths. A via counts only
between the outermost layers its tracks use, as KiCad optimises it. On the
example board every net's length matches KiCad's own DRC to 0.0001 mm. Pass
`include_vias=False` for track length alone.

Differential pairs are found by name using KiCad's own rule
(`DRC_ENGINE::MatchDpSuffix`): trailing digits and underscores are skipped, then
the name must end in `+`/`-` or `P`/`N`. So `USB_D+`/`USB_D-`, `TX_P`/`TX_N` and
`LVDS_P0`/`LVDS_N0` are pairs. Give `suffixes=[["_DP", "_DM"], ["_p", "_n"]]`
for other conventions, `pattern="*USB*"` to pick some, or `pairs=[...]` to name
them outright.

```python
DiffPairCheck(pattern="*USB*", max_skew_mm=0.15, max_uncoupled_mm=3)  # gap/width from net class
DiffPairCheck(pairs=[["ETH_TX+", "ETH_TX-"]], gap_mm=0.15, width_mm=0.2, max_via_mismatch=0)
LengthMatchCheck(pattern="/DDR_DQ*", target_length_mm=42, tolerance_mm=0.5)
```

KiCad's DRC can check pairs, lengths and skew as well, but only for rules a
project writes in a `.kicad_dru` file; `pcb.tuning.rules` reports those.

### Building blocks

The checks above are thin layers over modules you can use directly:

| Module | What it gives you |
|--------|-------------------|
| `kicad_evaltor.netlist` | `FileSchematic.get_netlist()` as nets, pins (with pin names and types) and components (with DNP, BOM and board flags) |
| `kicad_evaltor.connectivity` | `Connectivity(netlist).reach(net, through=...)`: follow a signal across series resistors, flow-through arrays or any parts you choose, stopping at power rails |
| `kicad_evaltor.values` | `parse_value("4k7") == 4700.0`, with SI prefixes, RKM notation and units |
| `kicad_evaltor.regulators` | `regulator_output(netlist, "U3")`: a regulator's output voltage and how it was worked out; `register_regulator` for parts it does not know |
| `kicad_evaltor.pin_roles` | `usb_polarity("UD+") == "D+"`, and `PairRule` for other pairs |
| `kicad_evaltor.symbol_library` | Read KiCad libraries, flatten `extends`, compare symbols by signature |
| `kicad_evaltor.board_file` | `load_board("x.kicad_pcb")`: footprints, pads (with nets and pin types), tracks with lengths, vias, zones, stackup, and net classes resolved as KiCad resolves them |
| `kicad_evaltor.pcbnew_bridge` | `dump_board(path)`: courtyards, pad outlines, board outline, zone fill and connectivity from KiCad's own `pcbnew` |
| `kicad_evaltor.board_connectivity` | `pad_to_pad_length(...)`: the shortest copper route between two pads |
| `kicad_evaltor.diff_pairs` | `find_diff_pairs(board.nets)` by KiCad's naming rule, and `measure_pair` for lengths, widths, gap and coupling |
| `kicad_evaltor.edit` | Build fixtures: `swap_symbol`, `embed_symbol` and `set_symbol_property` on a reference design; `remove_net_tracks`, `remove_footprint` and `move_footprint` on a board; then `save_document` |

```python
from kicad_evaltor import Connectivity, regulator_output
from kicad_evaltor.schematic_file import load_schematic

netlist = load_schematic("board.kicad_sch").get_netlist()
print(regulator_output(netlist, "U3"))  # volts, method, or the reason it is unknown
print(Connectivity(netlist).reach("/USB_DP").crossed)  # series parts on the way
```

A slow host can need more than the default 600 s for kicad-cli. Raise it per
context with `DesignContext(cli_timeout=900)` or per check with
`ERCRunCheck(timeout=900)`.

### Hierarchical sheets

A KiCad schematic is a tree of files: the root draws `(sheet ...)` blocks, and
each block links to another file. Every check above works across that whole tree,
so `ComponentExistsCheck(reference="U1")` finds U1 whether it was drawn on the
root or four levels down. A reference means one thing in a design; which sheet it
happens to sit on is not part of the question.

Four checks are about the hierarchy itself. They read the files directly and skip
cleanly when the context has no file-backed schematic.

| Check ID | Class | Description |
|----------|-------|-------------|
| `sch.sheet.file_missing` | `SheetFileMissingCheck` | A `Sheetfile` resolves to a readable `.kicad_sch` |
| `sch.sheet.cycle` | `SheetCycleCheck` | The sheet links contain no cycle |
| `sch.sheet.pin_mismatch` | `SheetPinMismatchCheck` | Every sheet pin has a matching hierarchical label, and the reverse |
| `sch.sheet.name_or_page_collision` | `SheetNameOrPageCollisionCheck` | Two sibling sheets share a name, or two sheets share a page number |

`sch.sheet.file_missing` distinguishes a `Sheetfile` that names nothing on disk
from one that does not parse, because the two mean different things. Relative
paths resolve against the **project** folder first, the way KiCad resolves them,
then against the folder of the file holding the link — so a `Sheetfile` of
`../shared/shared.kicad_sch` works from a subdirectory.

`sch.sheet.cycle` is keyed on the path taken to reach a sheet, not on the files
seen so far: KiCad legitimately instantiates one file under several sheet
symbols, and only a path that re-enters its own ancestor is a cycle.

`sch.sheet.name_or_page_collision` reports a sheet with no page number only when
the root shows the design ever tracked pages at all. A hand-written or generated
schematic has no `(sheet_instances ...)`, and reporting every sheet in one would
be noise rather than a finding.

### Selecting sheets

The layout checks each take an optional `sheet` parameter naming which sheet to
look at: a sheet name, a `Sheetfile` stem, or a KiCad instance path when one file
is instantiated twice. `sheet="all"` and the default both mean every sheet.

```python
TextSymbolOverlapCheck(sheet="shared")  # one sheet
TextSymbolOverlapCheck(sheet="all")  # the default
```

Omitting it covers the whole tree. A flat design *is* a one-sheet tree, so
nothing changes for one, while a hierarchical design would otherwise be checked
only on its root — which typically holds nothing but sheet symbols, and would
report a clean bill of health. Findings from a non-root sheet are qualified with
its name the same way a symbol's own text is, so `D1.Reference` on the
hierarchical demo's `shared` sheet reads as `shared.D1.Reference`. Root-sheet
findings are unqualified, exactly as before.

A selector that matches nothing is reported as a skip listing the valid names,
never as a pass: a typo that quietly checked nothing would look identical to a
clean design.

### Layout

These read each sheet's own geometry rather than the netlist. Text boxes are
computed from a stroke font calibrated against a real KiCad renderer, so the
verdicts match what you see in the viewer.

| Check ID | Class | Description |
|----------|-------|-------------|
| `sch.layout.text_text_overlap` | `TextTextOverlapCheck` | Two text items overlap |
| `sch.layout.text_symbol_overlap` | `TextSymbolOverlapCheck` | Text sits on a symbol |
| `sch.layout.symbol_symbol_overlap` | `SymbolSymbolOverlapCheck` | Two symbol bodies share space |
| `sch.layout.text_off_sheet` | `TextOffSheetCheck` | Text falls outside the sheet boundary |
| `sch.layout.text_wire_overlap` | `TextWireOverlapCheck` | Text sits on a wire. Opt-in: KiCad masks wires behind text, so this is usually benign |
| `sch.layout.symbol_wire_overlap` | `SymbolWireOverlapCheck` | A wire runs across a symbol's artwork, or into a pin from the body side |
| `sch.noconnect.floating` | `NoConnectFloatingCheck` | A no-connect flag marks no pin, or a drawn pin is reached by nothing at all |

Hidden items are never reported. A field marked `(hide yes)` is skipped, a pin
marked `(hide yes)` is not counted as a pin left open, and a power symbol's name
is treated as symbol artwork rather than as text.

A component's own text is measured against its own body rather than the full
body-and-pins box, and only crossing the outline counts. KiCad places a
reference directly above a body, which is where the topmost pin stub is too, so
measuring pins would report the default placement of every part. A small symbol
is mostly empty space, so a value that fits wholly inside one is left alone. Text
straddling the outline, half in and half out, is reported — and so is text over
*another* symbol's pins.

Every finding carries the properties behind each label, so a report says what is
on the sheet rather than only which field collided. Value is one property among
many: user-defined ones such as an LCSC part number are reported the same way.

The two text-on-anything checks answer a legibility question and leave a 0.2mm
clearance between the pair: text that comes within it is unreadable even though
nothing is drawn on top of anything. Set `clearance=0` to report only what
genuinely overlaps. The remaining checks answer "is this drawn on that", so they
take no clearance, and take a `margin` if the measurement noise of sampled
outlines and bare pin lines needs hiding.

To ignore shallow overlaps, set `min_overlap_mm`: a pair counts only when the
boxes as drawn share at least that much width and height. It replaces a negative
`margin` rather than adding to it (setting both is an error). A suite that used
`margin=m` and then dropped overlaps narrower than `w` converts to
`min_overlap_mm = w - 2*m`; `margin=-0.2` with a 0.6mm filter is
`min_overlap_mm=1.0`.

```json
{
  "first": "SW2.Value", "first_kind": "text", "first_properties": {"Value": "SW_Push"},
  "second": "PWR012", "second_kind": "symbol", "second_properties": {"Value": "GND"},
  "area": [256.54, 44.78, 259.08, 46.17], "width": 2.54, "height": 1.39
}
```

Hidden properties are left out, on both sides: a field marked `(hide yes)` is not
drawn, so it is not part of what the sheet shows.

`report.to_json()` serialises the whole run, `report.to_dict()` and
`result.to_dict()` give the same data as Python objects, and
`report.failures_json()` returns just the failures, which is usually what a CI
job wants to read.

## Authoring precise rules

The caller owns evaluation policy. Component connectivity means shared nets
(including ground); use pin checks when the signal or pin assignment matters:

```python
from kicad_evaltor import PinsConnectedCheck, NetMembersCheck, TraceWidthCheck

signal = PinsConnectedCheck(
    requirement_id="connector-data",
    pin_a="U1.3",
    pin_b="J1.2",
    through={"R1": [("1", "2")]},
)
no_short = PinsConnectedCheck(pin_a="U1.3", pin_b="J1.1", connected=False)
exact_net = NetMembersCheck(net="/DATA", pins=["U1.3", "R1.1"])
width = TraceWidthCheck(net_name="/DATA", min_width_mm=0.2, on_empty="fail")
```

Pin checks cross only explicitly configured pin pairs, without guessing which
parts conduct. `through` permits traversal; it does not require that a path
traverse every named part. Missing endpoints in the exported netlist return
ERROR, including for forbidden connections: absence cannot prove isolation.
`NetMembersCheck(exact=False)` permits additional net members.

`TraceWidthCheck` and `FootprintMatchesSchematicCheck` accept
`on_empty="skip" | "fail" | "pass"`. The default is `"skip"`: a selection
that matches nothing (a mistyped net class, an unrouted net) is never a quiet
pass. Empty results carry `selected_count=0`. A reference you name in
`FootprintMatchesSchematicCheck(references=[...])` that is not in the design
fails outright. Reusable selectors are available for
custom checks:

```python
from kicad_evaltor.selection import select

resistors = select(ctx.sheet_symbols(), "R*", key=lambda s: s.reference)
resistors.require_count(minimum=1, maximum=20)
```

Runner results include `requirement_id` and a parameter snapshot. Give explicit
IDs to track requirements across reordered suites; generated IDs include the
suite index. Duplicate IDs are rejected. `report.evaluated_count` counts PASS
and FAIL; `report.not_passed()` includes errors and skips as well as failures.
`failures_json()` retains its narrower FAIL/ERROR behavior. Empty suites still
have `all_passed=True`; callers can additionally require a nonzero evaluated
count when appropriate.

Contexts cache a design until `ctx.invalidate()` or `ctx.close()`. Call
`invalidate()` after editing inputs in a generate/evaluate/repair loop. Use a
separate context for each concurrent design. Temporary report directories are
context-local and never change the host process working directory.

Malformed or structurally unrecognized ERC/DRC reports raise `InvalidReport`
when parsed directly and produce ERROR through the checks. Numeric SI case is
preserved even during case-insensitive text comparison (`1m` differs from
`1M`). Numeric value parsing compares magnitudes; it is not dimensional analysis.
`ConsistencyCheck(require_both=False)` allows extra PCB references but still
requires every schematic component intended for the board.

Decoupling proximity is evaluated per power pin. It measures planar placement,
not actual routed loop length, capacitor electrical suitability, or inductance.
Pad-to-pad route length supports straight-track endpoint and via landings, and
crosses the copper of pads on the net (a route that flows through an ESD part's
pads), costing the straight distance between landings. Interior arc joins and
filled-zone paths are not modeled; `measure_route` says why a route could not be
measured. Use KiCad routing completeness checks for general copper connectivity.

### Querying parts, nets and findings

These helpers return facts with the evidence behind them; the pass/fail rule
stays in your test.

```python
from kicad_evaltor import (
    CircuitQueries,
    DRCRunCheck,
    ERCRunCheck,
    any_of,
    find_part,
    led_colors,
    measure_path_length,
    pins_named,
    quantity_of,
    series_parts,
)

netlist = ctx.schematic.get_netlist()
usb = find_part(netlist, "", markers=("USB_C",), required_pins=("CC1", "CC2", "D+", "D-"))
chg = pins_named(netlist, find_part(netlist, "BQ24074").ref, "CHG")[0]  # ~{CHG}, /CHG, nCHG
gnd = netlist.net_named("GND")  # /GND, /power/GND

q = CircuitQueries(netlist)
q.resistance_between(("U3", "12"), "GND").ohms  # series chain, stops at rails
q.strapped(("U1", "8"), ["GND", "+3V3"], max_ohms=100e3)
merged = netlist.merged(any_of(series_parts("R", max_ohms=0), series_parts("FB")))

quantity_of(netlist.components["L1"], "inductance")  # Value, Description, then fields
{c.color for c in led_colors(netlist.components["D1"])}

# A signal through a series resistor, net by net, with KiCad's net totals
measure_path_length(board, geometry, "U4.5", "D3.2", through=["R14"])

# ERC/DRC findings carry pin types, lib ids, origin and owners
erc = ERCRunCheck(severity="all").run(ctx)
real = [f for f in erc.details.get("violations", []) if f["origin"] == "design"]
DRCRunCheck(severity="error", exclude_owned_by=["J1"])  # J1's own findings only
DRCRunCheck(severity="all", reset_severities=True, drop_exclusions=True)  # hidden_by
```

Part numbers can be checked against distributor catalogues with the optional
`kicad-evaltor[sourcing]` extra:

```python
from kicad_evaltor.sourcing import PartRule, SourcingCheck

SourcingCheck(
    fields={"LCSC": "lcsc"},
    rules=[PartRule(reference="R*", category=("Resistor",), dimension="resistance")],
)
```

To check exactly what a BOM lists, through-hole parts included, pass it in;
the symbol flags and `exclude` then only say which parts the BOM should list:

```python
SourcingCheck(
    bom_path="out/bom.csv",  # or bom=read_bom(...) rows, or bom=["R1", "C2-C4"]
    bom_fields={"LCSC Part": "lcsc"},  # read numbers from the BOM, not the symbols
    bom_complete=True,  # also fail fitted parts the BOM leaves out
)
```

## Custom checks

A check is a class with a stable dotted `id`, registered so it can be looked up
by name.

```python
from dataclasses import dataclass

from kicad_evaltor import Check, CheckResult, TestStatus, register


@dataclass
class MyParams:
    min_count: int
    prefix: str


@register
class MyComponentCountCheck(Check):
    id = "custom.component.count"
    name = "Component Count"
    description = "Ensures a minimum number of components exist"
    Params = MyParams

    def run(self, ctx):
        symbols = ctx.schematic.get_symbols()
        matching = [s for s in symbols if s.reference.startswith(self.params.prefix)]
        if len(matching) >= self.params.min_count:
            return CheckResult.pass_(self.id, f"Found {len(matching)} components")
        return CheckResult.fail(self.id, f"Only {len(matching)} components found")


result = MyComponentCountCheck(prefix="R", min_count=10)
```

`TestStatus` is one of `PASS`, `FAIL`, `ERROR`, or `SKIP`. Use `SKIP` for a check
that cannot run in the current environment, such as a missing `kicad-cli`, so it
is reported honestly rather than passing silently.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest -m "not integration"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions, and
[CHANGELOG.md](CHANGELOG.md) for what changed.

## Documentation

- [Architecture](docs/architecture.md) — how parsing, geometry and the check
  registry fit together.
- [Contributing](CONTRIBUTING.md) — setup, gates, and conventions.

## License

MIT — see [LICENSE](LICENSE).
