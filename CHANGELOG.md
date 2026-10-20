# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Caller-owned test-authoring APIs: component/pin selection, channel mappings,
  bounded connectivity witnesses, DC resistor networks, structured quantities,
  scoped regulator models, BOM reconciliation, drawing groups, and PCB placement
  measurements. Raw queries work independently of a test runner.
- Optional measurement/composition checks, report aggregation policies,
  caller-declared prerequisites, evidence validation, and toolchain/source probes.
- Optional KiCad SVG ink confirmation for text/text, text/symbol, and
  symbol/symbol checks, with isolated scratch files and complete findings.
- Active-project schematic annotation and page resolution, stable symbol
  identities, explicit ambiguous-sheet selection, and optional scope completeness.
- Windows fast-test CI and a reusable integration workflow taking a pinned
  KiCad image digest and an explicit expected tool version.

### Fixed

- DC networks reject every floating component before numerical elimination;
  pivot scaling retains the original row scales.
- Rendered overlap checks report uncertain apparent intersections as unavailable
  measurements, with witnessed overlap depth in raw evidence.
- Output-reference regulator models accept grounded adjustment pins as a
  reference-voltage setpoint, while grounded divider feedback remains unresolved.
- Trailing `R` notation retains its resistance dimension and cannot be interpreted
  as capacitance or another requested dimension.
- Legacy bare `hide` tokens are recognized alongside current hidden-field syntax.
- Machine overlap evidence is no longer truncated after twenty findings.
- Non-finite check parameters are rejected before evaluation.
- PCB route graphs accept track/via landings inside arcs; an unresolved route
  measurement reports an error instead of claiming a routing defect.
- Subprocess output is decoded explicitly as UTF-8, including partial timeout
  output. Differential-pair measurements expose their sampling method and step.

## [0.5.0] - 2026-10-18

Board evaluation, built and tested against a real KiCad 10 board
(`examples/demo_circuit/esp_data_logger_board`).

### Added

- `kicad_evaltor.board_file`: `load_board` reads `.kicad_pcb` with no KiCad
  installed. It covers footprints (fields, attributes, schematic path,
  reference text style), pads (net, pin function and type, drill), tracks and
  arcs with their lengths, vias, zones, the stackup, and net classes and
  design rules from the project file. `NetClasses.effective` resolves KiCad
  10's composite classes such as `"VCC,Default"` by priority. Per-net track
  lengths match pcbnew's exactly.
- The board reader also reads files from KiCad 9 and earlier, which name nets
  by code on tracks, vias and zones (`(net 3)`).
- `kicad_evaltor.pcbnew_bridge`: `dump_board` runs KiCad's own `pcbnew` under
  KiCad's bundled Python. It returns courtyards, pad outlines, the board
  outline, zone fill after a refill, pour connectivity, text boxes and each
  net's class. `DesignContext.board_geometry()` runs it once per context.
- `kicad_evaltor.utils.kicad_python` finds KiCad's interpreter, or uses
  `$KICAD_PYTHON`. `kicad_evaltor.utils.kicad_cli.find_kicad_cli` finds
  `kicad-cli` in the standard install folders when it is not on `PATH`.
- `kicad_evaltor.board_connectivity`: `pad_to_pad_length` gives the shortest
  copper route between two pads.
- `kicad_evaltor.diff_pairs`: `find_diff_pairs` and `kicad_complement` (KiCad's
  pair naming rule), and `measure_pair`. The pair gap it measures matches
  KiCad's DRC clearance on the example board.
- `FileBoard.net_length` counts a net as KiCad's DRC and length tuning do:
  tracks, plus via height when the project's "use via height in length" is on,
  plus pad-to-die lengths. A via counts only between the outermost layers its
  tracks use. Every net on the example board matches KiCad's DRC to 0.0001 mm.
- `pcb.via.rules` holds microvias to KiCad's microvia minimums
  (`min_microvia_drill`, `min_microvia_diameter`), not the through-via ones.
- New board checks:
  - DRC-backed: `pcb.routing.complete`, `pcb.schematic_parity`,
    `pcb.footprint.matches_library`, `pcb.silk.over_copper`,
    `pcb.silk.overlap`.
  - Placement: `pcb.footprint.on_board`, `pcb.footprint.placement`,
    `pcb.footprint.near` (with a `decoupling` mode that pairs every
    power-input pin with a capacitor), `pcb.board.outline`,
    `pcb.mounting_holes`, `pcb.footprint.matches_schematic`.
  - Routing: `pcb.net.length_match` (max skew and/or a target length),
    `pcb.net.vias`, `pcb.net.layers`.
  - Differential pairs: `pcb.diff_pair` checks skew, track width, coupled
    gap, uncoupled length and via count. Gap and width default to the net
    class's diff-pair settings. Pairs are found by name with KiCad's own rule
    (`+`/`-` and `P`/`N`, trailing digits and underscores skipped), by custom
    `suffixes`, by `pattern`, or named outright.
  - `pcb.tuning.rules` reports KiCad's own `length_out_of_range`,
    `skew_out_of_range` and diff-pair DRC findings from custom
    `.kicad_dru` rules.
  - Copper: `pcb.trace.current_capacity` (IPC-2221), `pcb.via.rules`,
    `pcb.thermal.vias`, `pcb.plane.coverage`.
  - Silkscreen: `pcb.silk.reference_visible`, `pcb.text.off_board`.
  - Fabrication: `pcb.fab.exports`, `pcb.fab.position_file`.
- `DesignContext.memo(key, compute)` keeps one answer per context. DRC runs once
  and every DRC-backed check filters that one report.
- `kicad_evaltor.edit` edits boards too: `remove_net_tracks`, `remove_footprint`
  and `move_footprint`. They are for making failing cases from a real board.
- `sch.pins.connected` (`PinsConnectedCheck`): two specific pins are connected,
  or must not be, crossing only the internal pin pairs given in `through`. A pin
  missing from the netlist is an error: absence cannot prove isolation.
- `sch.net.members` (`NetMembersCheck`): a net holds the listed pins, and with
  `exact=True` nothing else.
- `on_empty="skip" | "fail" | "pass"` on `pcb.trace.width` and
  `pcb.footprint.matches_schematic`, for when a selection matches nothing. It
  defaults to `"skip"`, so a mistyped net class is never a quiet pass.
- Checks take a `requirement_id`, and runner results carry it with a snapshot of
  the check's parameters. `TestReport.evaluated_count` counts passes and
  failures; `TestReport.not_passed()` adds errors and skips.
- `DesignContext.invalidate()` reloads the files and drops cached tool results,
  for a generate, evaluate and repair loop.
- `kicad_evaltor.selectors.select` picks items by wildcard for custom checks and
  can require how many it picks.
- `examples/pcb.py` runs the board checks on the example board.

### Changed

- `pcb.drc` counts unrouted connections and schematic parity issues, not only
  rule violations. Each finding names its `source`. New options:
  `include_types`, `exclude_types` and `refill_zones` (on by default, as in
  KiCad's DRC dialog).
- `pcb.trace.length` measures a net (`net=`) or one pad-to-pad route
  (`from_pad=`/`to_pad=`), against min/max or `target_length_mm` ±
  `tolerance_mm`, with lengths counted as KiCad counts them (`include_vias`). `reference=` is rejected with a pointer to these.
  It used to add up every net the footprint touched, which measured nothing
  useful.
- `pcb.footprint.overlap` reads KiCad's courtyard overlaps from DRC. With
  `min_clearance_mm` it also measures gaps between real courtyard outlines,
  not bounding boxes that include silkscreen text.
- `pcb.power.pour` measures the filled area in mm² after a refill (it used to
  square a length), can be limited to one `layer`, and by default requires the
  pour to connect to a pad of its net.
- `pcb.trace.width` can select a `netclass`, or hold each track to its own net
  class (`match_netclass=True`). Findings give net, layer, ends and width.
- `pcb.footprint.exists`, `pcb.trace.width`, `pcb.power.pour` and
  `project.schematic_pcb_consistency` read the board file. They no longer need
  a running KiCad.
- `DesignContext` is file-backed throughout. `board` is a `FileBoard`.
  `headless` and `timeout_ms` are still accepted, and ignored.

- `pcb.footprint.near` in decoupling mode judges each power-input pin on its
  own. It used to take the nearest capacitor per net, which let one close pin
  hide a far one: on the example board, U2's VINA pin is 3.79 mm from its
  nearest capacitor.
- `project.schematic_pcb_consistency` with `require_both=False` still requires
  every schematic part on the board; it only allows extra board parts.
- `parse_erc_report` and `parse_drc_report` raise `InvalidReport` for malformed
  or unrecognised input instead of returning no findings, and the ERC and DRC
  checks report that as an error.
- kicad-cli runs in its scratch folder through the subprocess's working
  directory; the process's own working directory is never changed, so reports
  can run on several threads.

### Removed

- The `kicad-python` (kipy) dependency, and with it `DesignContext.kicad`,
  `DesignContext.project` and the `kicad-cli api-server` startup. KiCad 10 has
  no IPC API server, so that path could not run.

### Fixed

- A malformed ERC or DRC report no longer reads as a clean design.
- `sch.erc` resolves a relative schematic path before kicad-cli runs.
- A case-insensitive value match no longer treats `1m` and `1M` as equal.
- A pad-to-pad route through a via in the middle of a track now joins the
  layers the via spans.
- Net names KiCad stores escaped, like `Net-(U2-PS{slash}SYNC)`, are read as
  KiCad shows them (`Net-(U2-PS/SYNC)`), from both the board file and pcbnew.
  Such a net used to show no tracks.
- `parse_drc_report` reads KiCad 10's `unconnected_items` and
  `schematic_parity` lists. Reading only `violations` passed a board with
  nothing routed.
- DRC no longer passes the schematic path after `--schematic-parity`, which is a
  flag with no value. KiCad finds the schematic by project name.
- Board paths are made absolute before kicad-cli runs from its scratch folder.
  A relative path used to fail with "Failed to load board".
- `project.schematic_pcb_consistency` no longer reports power symbols, or parts
  marked "exclude from board", as missing from the board.

## [0.4.0] - 2026-10-17

### Added

- `DesignContext(cli_timeout=...)` sets how long a kicad-cli call may run, and
  `sch.erc` and `pcb.drc` take a per-check `timeout`. Netlist export uses the
  same setting.
- `FileSchematic.get_netlist(format=...)` reads either export format into a
  richer model. Each node carries its pin name (`pinfunction`) and electrical
  type (`pintype`). `FileNetlist.components` lists every component with its
  `lib_id`, fields, and `dnp`, `exclude_from_bom` and `exclude_from_board`
  flags. Lookups `net`, `node`, `net_of`, `pins_of` and `nodes_on` answer
  without walking the nets.
- `FileSymbol` reads each instance's `dnp`, `in_bom`, `on_board` and
  `exclude_from_sim` flags.
- `kicad_evaltor.connectivity`: `Connectivity(netlist).reach(net)` follows a
  signal across chosen parts, such as series resistors or a flow-through ESD
  array, and stops at power rails. Which parts conduct, and between which pins,
  is set by composable bridges (`series_parts`, `pin_pairs`, `same_pin_name`,
  `whole_part`, `any_of`).
- `sch.components.connected` takes `through_passives=True` to count connections
  made through series resistors, inductors and ferrite beads.
- `kicad_evaltor.symbol_library`: `SymbolLibraries` reads symbols by
  `Library:Symbol` from KiCad's stock folders (found via `KICADn_SYMBOL_DIR`,
  the `kicad-cli` install, or the standard locations), `.kicad_symdir` folders,
  and a project's `sym-lib-table`. `resolve` flattens `extends` the way KiCad
  does: the result matches what KiCad embeds in a schematic byte for byte.
  `symbol_signature` and `symbol_diff` compare two symbols by their pins and
  body, ignoring fields, names and uuids.
- `sch.symbol.matches_library` checks that placed symbols are unchanged copies
  of their library symbols, or of one named `expected_lib_id`, and reports each
  difference.
- `kicad_evaltor.edit`, for building fixtures from a reference design:
  `swap_symbol` changes a part to another library symbol, embedding the
  flattened copy so ERC sees no `lib_symbol_mismatch`; `embed_symbol` and
  `set_symbol_property` round it out, and `load_document`/`save_document`
  read and write the file.
- `sexpr.dumps` writes an s-expression tree back out. Quoted atoms parse as
  `sexpr.Quoted`, a `str` subclass, so the quoting survives a round trip.
- `SymbolPin` carries the pin's `electrical_type` and `shape`, and `FileSymbol`
  its `lib_name`.
- `kicad_evaltor.values.parse_value` reads component values as numbers: SI
  prefixes, RKM notation (`4k7`, `2R2`, `3V3`), units and trailing ratings
  (`100nF/50V`). `sch.component.value` takes `numeric=True` and a `tolerance`
  to compare that way.
- `kicad_evaltor.regulators.regulator_output` works out what a linear regulator
  outputs. A fixed part's voltage is read from its name or library
  description. An adjustable part's comes from its reference voltage and the
  resistor network on its feedback pin, solved by nodal analysis, so series
  and parallel resistors count. A current-reference part (LT3080, LT3042/45)
  uses its SET current and resistor. Reference voltages for common stock
  `Regulator_Linear` parts ship in `_regulators.json`, each citing its
  datasheet parameter; `register_regulator` adds others. When the output
  cannot be worked out, the result says why.
- `sch.regulator.output_voltage` checks a regulator's output against an
  expected voltage within a tolerance, and is an `ERROR`, not a `FAIL`, when
  the output cannot be determined.
- `kicad_evaltor.pin_roles` classifies differential pair pins by name: the USB
  rule covers `D+`, `UD+`, `USBDP`, `USBD+`, `USB_D+`, `DP` and the like,
  including MCU pins such as `PA12/USB_DP`, and there are rules for CAN and
  for generic `+`/`-` and `_P`/`_N` pairs. `usb_polarity(name)` is the short
  form.
- `sch.pair.polarity` checks that each half of a pair leaves one part and
  reaches the same half of another, never the opposite one, through series
  resistors and any flow-through parts named in `through` or `through_pins`.

### Changed

- kicad-cli calls default to a 600 s timeout instead of 60 s, so a large but
  valid design is no longer failed by a busy host. A run that times out is an
  `ERROR` naming the timeout, never a skip, even without `strict`.
- Netlists are exported as `kicadxml` by default. The `kicadsexpr` export
  leaves out every symbol marked "exclude from board", so a correctly wired
  off-board part had no pins on any net. Pass `format="kicadsexpr"` for the
  old behaviour.
- The netlist is exported once per `FileSchematic` and format, not on every
  call.
- Pin names in a netlist no longer carry the `_<number>` suffix KiCad 10
  appends (`FB_10` reads as `FB`).
- Net names read from a `kicadsexpr` export have KiCad's escapes undone
  (`{slash}` becomes `/`), so both formats spell a net the same way.

### Fixed

- A placed symbol that KiCad embeds under a `lib_name` alias is measured with
  that copy's body and pins, not with the copy under its `lib_id`.

## [0.3.0] - 2026-10-04

### Added

- Hierarchical schematics. A design is read as the tree of sheets KiCad draws:
  every `(sheet ...)` link is followed, relative `Sheetfile` paths resolve the
  way KiCad resolves them, and a broken link or a cycle becomes a finding rather
  than an error. Every check runs across the whole tree, so a reference is found
  on whichever sheet it is drawn, and connectivity follows nets through sheet
  pins.
- The layout and no-connect checks take a `sheet` parameter: a sheet name, a
  `Sheetfile` stem, a uuid instance path, or `"all"`, which is the default. A
  finding from a child sheet is prefixed with its name, such as
  `shared.D1.Reference`, and an unknown sheet is a skip that lists the valid
  names.
- Four checks on the tree itself: `sch.sheet.file_missing`, `sch.sheet.cycle`,
  `sch.sheet.pin_mismatch` and `sch.sheet.name_or_page_collision`.
- `examples/hierarchy.py`, running a suite against the hierarchical demo in
  `examples/demo_circuit/hierarchy/`.
- `tools/calibrate_text_placement.py`, which measures where kicad-cli draws
  each kind of text and writes `_text_placement_data.json`. It checks the fit
  against probe sets it did not fit on and refuses to write a table that misses
  by more than 0.02 mm.

### Changed

- Text boxes are the ink KiCad draws, stroke included, rather than the taller
  line cell it justifies by. Text that only came near other items was reported
  as overlapping them. A layout report can therefore differ from 0.2.0: findings
  that were artifacts of the larger boxes are gone.
- A symbol is measured by each graphic and pin line it is drawn from, not by
  the box around the whole symbol, so text in its empty space, beside an LED's
  triangle or between a connector's pins, is no longer reported as touching it.
  A global or hierarchical label's flag is likewise measured apart from its text.
- `sch.footprint.assigned` skips power symbols, which never have a footprint.
- `sch.noconnect.floating` counts a label as connected by its anchor, which is
  where KiCad connects it, rather than by any text covering the pin.
- The demo files drop their `_test` suffix, so every demo script carries the
  name of the sheet it runs: `examples/simple_circuit.py` runs
  `demo_circuit/simple_circuit.kicad_sch`, `examples/visual.py` runs
  `demo_circuit/visual.kicad_sch`, and the new `examples/hierarchy.py` runs
  `demo_circuit/hierarchy/hierarchy.kicad_sch`. KiCad's netlist and ERC for the
  renamed sheets are unchanged.

### Fixed

- Text rotation is applied. A label or sheet text follows its angle, and a
  symbol field follows its own angle and the symbol's rotation and mirror, drawn
  upright. Every field was boxed as horizontal, so side-by-side vertical fields
  were reported as overlapping.
- A symbol's `(mirror x)` or `(mirror y)` is applied after its rotation, as
  KiCad does. At 90 and 270 degrees the body and pins were mirrored the wrong way.
- Right-justified text ends at its anchor, and every word of a justification
  such as `(justify left bottom)` is read, not only the first.
- A library sub-symbol with body style 0 applies to every body style. Its pins
  were dropped, so symbols drawn that way, such as the TP4056, had none.
- KiCad's `{slash}`-style escapes are measured as the characters they draw,
  overbar, subscript and superscript markup is measured, and multi-line text is
  laid out line by line. Font thickness and bold are read.
- A no-connect flag on a sheet block's pin marks that pin, and a pin ending on
  another pin is connected without a wire, as KiCad's ERC treats both.
- Hidden text and labels are no longer treated as drawn geometry. KiCad records
  `(hide yes)` inside the `(effects ...)` block for `text`, the three label kinds
  and a sheet pin, rather than beside the node as it does for a symbol field, and
  only the second placement was read. A hidden note was therefore entered into the
  scene and could be reported as overlapping other text, sitting on a symbol body,
  or falling off the sheet. A hidden label over a pin could also mark that pin
  connected, suppressing a real no-connect finding.
- `sch.erc` reads a KiCad 10 report again. ERC moved its violations under
  `sheets[*].violations` and no longer writes a top-level `violations` key, so
  the parser found nothing and every schematic passed regardless of what KiCad
  reported. Each violation now names the sheet it was found on. `pcb.drc` is
  unaffected: DRC still writes one flat top-level list.
- ERC and DRC findings read KiCad's real field names. Both reports spell the
  human text `description` and keep positions in `items[*].pos`; reading `message`
  and `at` reported `None` for every violation in the detail payload.

## [0.2.0] - 2026-10-04

### Added

- `sch.layout.symbol_wire_overlap` (`SymbolWireOverlapCheck`), reporting a wire
  that runs across a symbol's artwork or into one of its pins from the body side.
  A wire leaving a pin away from the body is what a pin is for, and a wire that
  ends on a square pin side is a normal connection, so neither is reported.
- `sch.noconnect.floating` (`NoConnectFloatingCheck`), reporting a no-connect
  flag that marks no pin and a drawn pin that nothing reaches and no flag covers.
  Pins are named by number, since a symbol can have eight pins all called `NC`.
  `ignore` takes pins the design leaves open on purpose. A pin the symbol marks
  `(hide yes)` is not reported: it is not drawn, so no wire, label or flag can
  reach it on the sheet, and that is how symbols carry their reserved and NC pins.
- Symbol geometry is measured the way KiCad draws it: placement honours
  mirroring, negated rotation and `apply_direction`, box corners rotate with the
  symbol, and pin lengths, offsets and sub-symbol offsets are applied. Pin names
  and numbers are read from their own atoms rather than from an electrical type.
- `examples/visual_test.py`, a layout demo against
  `examples/demo_circuit/visual_test.kicad_sch`. `--json` prints the failures as
  JSON, and an optional argument points it at any other sheet. It reports rather
  than asserts: every finding is listed with its geometry, and it exits 0 even when
  the checks find defects.
- Layout findings report each item's visible properties, so a collision says what
  is on the sheet and not only which label it was.
- `CheckResult.to_dict()` and `TestReport.failures_json()`. `to_json()` takes an
  `indent`.

### Changed

- Renamed `circuit.kicad_sch` to `simple_circuit_test.kicad_sch` and
  `demo_schematic_check.py` to `simple_circuit_test.py`, so each demo script is
  named after the sheet it runs.
- `sch.layout.text_symbol_overlap` now reports a component's own text crossing
  its own body outline. Previously a part's text was exempt against its own
  symbol entirely. Pins stay out of it: KiCad places a reference above a body,
  which is where the topmost pin stub is, and a small symbol is mostly empty
  space, so text fitting wholly inside a body is left alone. Text over another
  symbol's pins is still reported. `J1`'s value was moved clear of its connector
  in the demo sheet, which is the one straddle it actually had.
- Text boxes are now the line cell KiCad reserves for a run of text, sized from a
  stroke font measured against a real KiCad renderer rather than fitted to the ink.
  Overlap is measured between boxes by nearest-corner distance instead of
  `min(gap_x, gap_y)`, which used to call two boxes on a diagonal clear.
- `clearance` (default 0.2mm) is applied to the two text-on-anything checks, which
  answer a legibility question; the remaining checks still take `margin` alone.

### Fixed

- `__version__` is read from the installed distribution instead of a literal, so
  it can no longer drift from `project.version`. A test fails on any mismatch.
- Repository links in the README, changelog, contributing guide and security
  policy pointed at a repository that does not exist.

### Changed

- The release workflow now refuses to run when the version being tagged is
  already on the index, instead of failing on PyPI's opaque upload error, and
  can publish to TestPyPI to rehearse a release.

## [0.1.0] - 2026-10-02

First public release.

### Added

- Schematic reader that parses `.kicad_sch` directly. KiCad 10 removed
  `kicad-cli api-server`, so schematic loading no longer depends on the KiCad
  IPC API; only `kicad-cli` subprocesses are used, for netlists, ERC and DRC.
- Stroke-font text metrics calibrated against a real KiCad renderer and cached
  in `_stroke_font_data.json`, so text geometry does not depend on a KiCad
  install at run time.
- Text layout geometry that reproduces KiCad's own placement: fields render flat
  regardless of the angle recorded on the field or the symbol, vertical
  placement is anchored on the baseline, and only the default (centred)
  horizontal justification differs from `left`/`right`.
- Layout checks for overlapping text, text on symbol bodies, text on wires, and
  text outside the sheet boundary, each with configurable margins and ignore
  lists.
- ERC and DRC wrappers that read KiCad 10's `"violations"` JSON while still
  accepting the older `"erc_violation"` / `"drc_violation"` keys.
- A demo design under `examples/demo_circuit/` and runnable scripts in
  `examples/`.
- Continuous integration covering ruff lint and format, mypy, the test matrix on
  Python 3.10 to 3.13, and distribution builds.
- Pre-commit and pre-push hooks running the same commands as CI, with ruff
  pinned to the version in `uv.lock`.

### Known limitations

- The seven PCB and cross-domain checks (`pcb.*` and
  `project.schematic_pcb_consistency`) are registered but **not implemented**.
  They reference a `FootprintInstance` that `kicad_evaltor.models` does not
  define, so they cannot run. They are excluded from the mypy gate. Schematic
  support is the current focus; see `docs/architecture.md`.
- `kicad_evaltor.core.context` interacts with `kipy`, which targets KiCad 11's
  socket API. Against KiCad 10 the call sites cannot type-check; the affected
  error codes are disabled explicitly in `pyproject.toml` rather than hidden.
- KiCad dependencies are not pinned per check version, so ERC and DRC output
  shape can shift between KiCad releases.

[Unreleased]: https://github.com/attahiruj/kicad-evaltor/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/attahiruj/kicad-evaltor/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/attahiruj/kicad-evaltor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/attahiruj/kicad-evaltor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/attahiruj/kicad-evaltor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attahiruj/kicad-evaltor/tree/v0.1.0
