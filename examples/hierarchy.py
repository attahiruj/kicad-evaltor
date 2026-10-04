#!/usr/bin/env python3
"""Run kicad-evaltor checks against a hierarchical schematic.

The demo in ``examples/demo_circuit/hierarchy/`` is a LoRa remote controller drawn
the way a real project is: a root sheet with the parts a user touches, and one
sheet below it for the board's power and MCU.

    hierarchy.kicad_sch   the root: the three buttons (SW1-SW3) with their
                          pull-downs and debounce capacitors, the status LED D3
                          driven by Q1, and one sheet block, ``shared``
    circuit.kicad_sch     the ``shared`` sheet: USB-C input (J2), the TP4056
                          charger (U3), the TPS63001 buck-boost (U1), the
                          STM32G031 (U2) and the reset button (SW4)

The two halves meet only through the sheet block's pins. ``/BTN1`` leaves SW1 on
the root, crosses into ``shared`` through one of those pins and lands on U2's pin
10, so a connectivity check between SW1 and U2 passes only if the whole tree is
read.

Every expectation below is KiCad's own. ``kicad-cli sch export netlist`` lists 41
parts across the sheets ``/`` and ``/shared/``, all with a footprint, and
``kicad-cli sch erc`` reports nothing. The 37 power symbols name nets rather than
parts, so the footprint check does not count them.

Every check passes. The layout is a fair test of that: both LEDs carry their
reference right beside the triangle, inside the box around the whole symbol
because the light arrows widen it lower down, and the ``CC2`` label sits between
two of J2's pins. All three are clear of everything KiCad actually draws, and the
layout checks measure a symbol by its drawn pieces, so none is reported. A
finding on the child sheet would carry its name, ``shared.``, so one report can
hold both sheets.

``TextWireOverlapCheck`` is left out for the reason given in ``overlaps.py``:
its 17 findings here are hierarchical labels whose flags sit on the wire each one
connects, which is what a flag is for.

Usage:

    python examples/hierarchy.py

Exit code is 0 when the results match this description, so this doubles as a
smoke test for the sheet tree.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from kicad_evaltor import (
    Check,
    ComponentExistsCheck,
    ComponentPropertyCheck,
    ComponentsConnectedCheck,
    ComponentValueCheck,
    DesignContext,
    ERCRunCheck,
    FootprintAssignedCheck,
    NoConnectFloatingCheck,
    SheetCycleCheck,
    SheetFileMissingCheck,
    SheetNameOrPageCollisionCheck,
    SheetPinMismatchCheck,
    SymbolSymbolOverlapCheck,
    SymbolWireOverlapCheck,
    TestReport,
    TestRunner,
    TextOffSheetCheck,
    TextSymbolOverlapCheck,
    TextTextOverlapCheck,
)

DEMO_SCHEMATIC = (
    Path(__file__).resolve().parent / "demo_circuit" / "hierarchy" / "hierarchy.kicad_sch"
)


def find_kicad_cli() -> str | None:
    """Locate kicad-cli, which KiCad does not put on PATH by default.

    Mirrors the lookup in ``DesignContext``: PATH first, then the standard
    install locations.
    """
    for name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(name)
        if found:
            return found

    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if not base:
                continue
            root = Path(base) / "KiCad"
            if not root.is_dir():
                continue
            for candidate in sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True):
                if candidate.exists():
                    return str(candidate)
    else:
        for candidate in (
            "/usr/bin/kicad-cli",
            "/usr/local/bin/kicad-cli",
            "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        ):
            if Path(candidate).exists():
                return candidate
    return None


def build_checks() -> list[Check]:
    """A suite for this design, reading across both sheets.

    The component checks take no ``sheet`` argument and find U2 although it is
    drawn on ``shared``: a reference means the same thing on any sheet. The
    layout checks default to the whole tree and name each finding's sheet.
    """
    return [
        # Parts on either sheet, found by reference alone.
        ComponentExistsCheck(reference="SW1"),
        ComponentExistsCheck(reference="U2"),
        ComponentExistsCheck(lib_id_pattern="power:*"),
        ComponentValueCheck(reference="R3", expected="180k"),
        ComponentPropertyCheck(
            reference="U2", field="Footprint", expected="Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm"
        ),
        # Nets that cross the sheet block: the buttons and the LED on the root
        # reach the MCU on shared only through its pins.
        ComponentsConnectedCheck(reference_a="SW1", reference_b="U2"),
        ComponentsConnectedCheck(reference_a="Q1", reference_b="U2"),
        FootprintAssignedCheck(),
        ERCRunCheck(severity="error"),
        # The tree itself: the link resolves, nothing loops, every sheet pin has
        # its label below, and no two sheets share a name or page.
        SheetFileMissingCheck(),
        SheetCycleCheck(),
        SheetPinMismatchCheck(),
        SheetNameOrPageCollisionCheck(),
        # Layout, sheet by sheet.
        TextTextOverlapCheck(),
        TextSymbolOverlapCheck(),
        SymbolSymbolOverlapCheck(),
        SymbolWireOverlapCheck(),
        NoConnectFloatingCheck(),
        TextOffSheetCheck(),
    ]


def print_report(report: TestReport) -> None:
    print()
    print(report.summary())

    print()
    print("Failures in detail:")
    failures = report.failures()
    if not failures:
        print("  none")
    for result in failures:
        print(f"  {result.check_id}: {result.message}")
        for item in result.details.get("overlaps", []):
            print(f"      {item['first']} -> {item['second']}")


def check_expectations(report: TestReport) -> bool:
    """Confirm the report says what this file's docstring claims: every check passes."""
    failures = sorted(r.check_id for r in report.failures())
    skipped = sorted(r.check_id for r in report.results if r.status.value == "skip")

    print()
    if skipped:
        print(f"Skipped, so not confirmed here: {', '.join(skipped)}")
    if failures:
        print(f"Unexpected failures: {', '.join(failures)}")
        return False
    print("Matched the documented expectations for this design.")
    return True


def main() -> int:
    if not DEMO_SCHEMATIC.exists():
        print(f"Demo schematic missing: {DEMO_SCHEMATIC}", file=sys.stderr)
        return 2

    cli = find_kicad_cli()
    print(f"Demo schematic: {DEMO_SCHEMATIC}")
    print(f"kicad-cli:      {cli or 'not found - connectivity and ERC will be skipped'}")

    with DesignContext(
        schematic_path=DEMO_SCHEMATIC,
        headless=True,
        kicad_cli_path=cli,
    ) as ctx:
        report = TestRunner(build_checks()).run(ctx)

    print_report(report)
    return 0 if check_expectations(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
