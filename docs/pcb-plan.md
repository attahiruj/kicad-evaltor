# PCB evaluation: plan and status

Board checks were scaffolding. All of them read the board through kipy and the
IPC API server, which KiCad 10 removed, so none could run. This records what
replaced them, the decisions behind it, and what is still open. How the pieces
fit is in [architecture.md](architecture.md).

## Decisions

- **KiCad does the work wherever it can.**
  - Rule checks come from `kicad-cli pcb drc`.
  - Geometry (courtyards, pad shapes, outline, zone fill, connectivity) comes
    from `pcbnew` under KiCad's bundled Python.
  - Fabrication outputs come from `kicad-cli pcb export`.
  - Plain values (footprints, pads, tracks, vias, stackup) are parsed from the
    file.
  - Our own code covers only what KiCad has no API for: IPC-2221 width,
    decoupling pairing, proximity, and pad-to-pad length.
- **kipy is dropped.** KiCad 10 has no IPC server, so that path could not run.
- **Check ids keep their names, and their meaning is corrected in place.** For
  example, `pcb.trace.length` now measures a net or a pad-to-pad route.
- **Tested on a real board, not invented ones.** The example board is
  `examples/demo_circuit/esp_data_logger_board`. Failing cases are copies of it
  with one thing broken (`tools/record_pcb_fixtures.py`). Unit tests replay
  KiCad's recorded output, and integration tests prove the recordings still
  match.
- **One release at the end, not one per phase.**

## Status

| Phase | Done |
|-------|------|
| 0. DRC report | Reads all three lists; `pcb.routing.complete`, `pcb.schematic_parity`; parity flag and relative path fixed |
| 1. Board access | `board_file.py`, `pcbnew_bridge.py`, `utils/kicad_python.py`; `DesignContext` file-backed; kipy removed; mypy covers all PCB code |
| 2. Routing | Net length and pad-to-pad length counted as KiCad counts them (via height, pad-to-die), target-length modes, `pcb.net.vias`, `pcb.net.layers`, `pcb.net.length_match`, net-class widths |
| 2b. Differential pairs | `pcb.diff_pair` (KiCad's naming rule plus custom suffixes; skew, width, gap, uncoupled length, via count), `pcb.tuning.rules` (KiCad's DRC length, skew and pair rules) |
| 3. Placement | `pcb.footprint.overlap` (DRC plus courtyard gaps), `pcb.footprint.on_board`, `pcb.footprint.placement`, `pcb.footprint.near` with decoupling mode, `pcb.board.outline`, `pcb.mounting_holes`, `pcb.footprint.matches_schematic`, `pcb.footprint.matches_library` |
| 4. Copper | `pcb.power.pour` (fill area, layer, connected), `pcb.plane.coverage`, `pcb.trace.current_capacity`, `pcb.via.rules`, `pcb.thermal.vias` |
| 5. Silkscreen | `pcb.silk.reference_visible`, `pcb.silk.over_copper`, `pcb.silk.overlap`, `pcb.text.off_board` |
| 6. Fabrication | `pcb.fab.exports`, `pcb.fab.position_file` |

## What the example board shows

KiCad's DRC is clean on it: no violations, no unrouted connections, no parity
issues. The new checks still find four real things:

- 17 tracks on `+3.7V` are 0.3 mm, but the net's class (3.7V) asks for 0.5 mm.
  KiCad treats a class width as a default, not a rule.
- U2's (TPS63020) exposed thermal pad, pad 15, is on no net and has no vias.
- 42 of the 43 footprints that are not fiducials hide their reference
  designators.
- U1's courtyard (the ESP32 module plus its antenna clearance) runs about
  14 mm past the board edge. This is deliberate for a module antenna, so
  callers exclude it with `exclude_refs`.

## Open

- **A real differential pair to test on.** The example board has none, so
  `pcb.diff_pair`'s measuring is proven on its UART lines (/RX0 and /TX0),
  whose gap matches KiCad's DRC clearance. A board with a routed USB or
  Ethernet pair would test naming and coupling end to end.
- **Impedance.** Needs a field solver or a fab's stackup tables; KiCad's
  calculator has no API.
- **Signals crossing a split in their reference plane.**
- **Fab profiles.** Minimum feature sizes and layer count checked against a
  named fab house's rules.
- **Other custom `.kicad_dru` rules.** Length, skew and pair rules are surfaced
  by `pcb.tuning.rules`; the rest are only in `pcb.drc`.
