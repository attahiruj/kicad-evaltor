# kicad-evaltor

A Python library for validating KiCad designs programmatically. Describe the
properties a design is supposed to have — reference designators, values,
connectivity, footprint assignment, ERC results, and whether the sheet is
readable — and run them as a test suite.

Works against **KiCad 10 and later**. It does not need the KiCad IPC API server,
which KiCad 10 removed.

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
work with no KiCad present.

A `kicad-cli` binary is needed only for the checks that shell out:

| Check | Needs |
|-------|-------|
| `sch.components.connected` | `kicad-cli sch export netlist` |
| `sch.erc` | `kicad-cli sch erc` |

Without it those checks report an error and the rest of the suite still runs.
On Windows, KiCad is not put on `PATH` by default; pass
`kicad_cli_path="C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"` or let
`DesignContext` find it.

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
[`examples/simple_circuit_test.py`](examples/simple_circuit_test.py), which runs
the schematic checks against
[`examples/demo_circuit/simple_circuit_test.kicad_sch`](examples/demo_circuit/simple_circuit_test.kicad_sch).
That demo is deliberately hierarchical — a root holding only sheet blocks, plus
`Main`, `Power`, and a `Shared` sheet reached through `../demo_shared/` — so the
tree is exercised for real rather than described in prose. Splitting a schematic
does not change its netlist: exporting the root gives the same 46 nets and 15 BOM
components as the flat file did, with identical net names.

It exits non-zero if the design stops matching its documented expectations, so it
doubles as a smoke test:

```bash
python examples/simple_circuit_test.py
```

[`examples/visual_test.py`](examples/visual_test.py) does the same for the
layout checks, against
[`examples/demo_circuit/visual_test.kicad_sch`](examples/demo_circuit/visual_test.kicad_sch).
It runs every `sch.layout.*` and `sch.noconnect.*` check the registry knows and
lists each finding with the properties and coordinates involved. It is a report,
not a test: it exits 0 even when the checks find defects. It needs no KiCad at
all, and takes any sheet as an argument:

```bash
python examples/visual_test.py                                 # human report
python examples/visual_test.py --json                          # the same failures as JSON
python examples/visual_test.py path/to/your.kicad_sch          # any other sheet
```

## Checks

### Schematic

| Check ID | Class | Description |
|----------|-------|-------------|
| `sch.component.exists` | `ComponentExistsCheck` | A component exists, by reference or `lib_id` pattern |
| `sch.component.value` | `ComponentValueCheck` | A component's value matches, case-insensitively |
| `sch.component.property` | `ComponentPropertyCheck` | An arbitrary field on a component matches |
| `sch.components.connected` | `ComponentsConnectedCheck` | Two components share at least N nets |
| `sch.footprint.assigned` | `FootprintAssignedCheck` | Footprints are assigned, optionally allowing `None` |
| `sch.symbol.in_library` | `SymbolInLibraryCheck` | A symbol exists in the project libraries |
| `sch.erc` | `ERCRunCheck` | Runs ERC via `kicad-cli` and filters by severity |

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
TextSymbolOverlapCheck(sheet="Power")  # one sheet
TextSymbolOverlapCheck(sheet="all")  # the default
```

Omitting it covers the whole tree. A flat design *is* a one-sheet tree, so
nothing changes for one, while a hierarchical design would otherwise be checked
only on its root — which typically holds nothing but sheet symbols, and would
report a clean bill of health. Findings from a non-root sheet are qualified with
its name the same way a symbol's own text is, so `R3.Reference` reads as
`Main.R3.Reference`. Root-sheet findings are unqualified, exactly as before.

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
