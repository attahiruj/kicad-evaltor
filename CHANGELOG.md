# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/attahiruj/kicad-evaltor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/attahiruj/kicad-evaltor/tree/v0.1.0
