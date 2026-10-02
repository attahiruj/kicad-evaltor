# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/kicad-evaltor/kicad-evaltor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kicad-evaltor/kicad-evaltor/releases/tag/v0.1.0
