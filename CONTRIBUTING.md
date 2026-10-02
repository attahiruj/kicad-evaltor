# Contributing

Thanks for considering a contribution. This project is young, so small focused
pull requests are much easier to review than large ones.

## Getting set up

You need [uv](https://docs.astral.sh/uv/). A KiCad install is optional: the
schematic reader parses `.kicad_sch` directly, so the test suite runs without
one.

```bash
git clone https://github.com/attahiruj/kicad-evaltor
cd kicad-evaltor
uv sync --extra dev
```

## Before you open a pull request

Run the same gates CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m "not integration"
```

All four must be clean. The `integration` marker is excluded because those
tests drive a live KiCad through `kipy`; run them locally with
`uv run pytest -m integration` if you have KiCad available.

mypy is gated on `src` only. `checks/pcb/` and `schematic/consistency.py` are
excluded because the PCB checks are not implemented, and `core/context.py` is
excluded because it calls into `kipy`, which targets a newer KiCad than we run
against. Both are annotated in `pyproject.toml` with the reason.

## Versioning

The version lives in exactly one place, `project.version` in `pyproject.toml`,
and the maintainer bumps it. Do not edit the version, add a `v*` tag, or publish
to PyPI from a pull request. `kicad_evaltor.__version__` is read from the
installed distribution, and a test fails if it ever disagrees with
`pyproject.toml`.

If your change needs a release note, add it under `Unreleased` in
`CHANGELOG.md`.

## Commit hooks

Install them once after cloning:

```bash
uvx pre-commit install            # commit stage
uvx pre-commit install --hook-type pre-push
```

The commit stage runs ruff-format, ruff-check with `--fix`, mypy, and the
housekeeping checks (trailing whitespace, end of file, YAML and TOML validity,
merge conflicts, large files, line endings). The pre-push stage adds pytest and
`uv build`.

Hooks and CI run the same commands against the same ruff version, pinned in
`.pre-commit-config.yaml` to match `uv.lock`. To run everything by hand:

```bash
uvx pre-commit run --all-files
uvx pre-commit run --hook-stage pre-push --all-files
```

Formatting is enforced. If CI rejects a pull request on `ruff format`, run
`uv run ruff format .` and commit the result rather than arguing with it.

## Where things live

| Path | Purpose |
|------|---------|
| `src/kicad_evaltor/schematic_file.py` | `.kicad_sch` and `.kicad_pro` parsing |
| `src/kicad_evaltor/schematic_items.py` | Symbols, wires, labels and text turned into geometry |
| `src/kicad_evaltor/font_metrics.py` | Stroke-font metrics and text box placement |
| `src/kicad_evaltor/geometry.py` | `Placement`, `BBox` and the transform conventions |
| `src/kicad_evaltor/checks/` | The checks themselves, one module each |
| `tools/calibrate_stroke_font.py` | Regenerates `_stroke_font_data.json` from a real KiCad |

## Conventions

- Geometry is millimetres, sheet coordinates, y increasing downward. All
  rotation goes through `Placement.apply_box`; do not open-code trigonometry.
- A check is a module in `checks/` exporting one class, registered with
  `@register` and given a stable dotted `id`. Never renumber an existing `id`.
- New checks need a test in `tests/test_checks_*.py` and a row in the README
  table.

## Text geometry is calibrated, not guessed

`font_metrics.py` reproduces KiCad's text placement from measured data. If you
change how a text box is computed, you must re-verify it against a real KiCad
rendering rather than adjusting a constant until a test passes. The calibration
tool is there for exactly this:

```bash
uv run python tools/calibrate_stroke_font.py
```

An overlap finding that cannot be reproduced by eye in KiCad's own viewer is a
geometry bug on our side, not a false positive in the design. Fix the geometry.

## Reporting a bug

Include the schematic (or a minimal extract), the check id, what KiCad's viewer
actually shows, and what we reported. A KiCad version is required: ERC and DRC
output and text rendering both vary by release.

## Commit and PR style

- One logical change per pull request.
- Describe the observable before-and-after behaviour, not just the code change.
- Add a `CHANGELOG.md` entry under `Unreleased` for anything user-visible.
- Note any check that you knowingly left failing.
