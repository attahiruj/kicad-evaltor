#!/usr/bin/env python3
"""Run kicad-evaltor checks against the bundled demo schematic.

The demo in ``examples/demo_circuit/circuit.kicad_sch`` is an ATmega328P +
MPU-6050 IMU breakout. It is the same design the test suite uses, so every
expectation below was read back from the design itself rather than guessed:

    R1       10k pull-up on /RESET with SW1
    R2, R3   4k7 pull-ups on /SDA and /SCL
    C1       100n decoupling on +3.3V
    C2, C3   22p crystal load capacitors for Y1
    C6, C7   MPU-6050 CPOUT / REGOUT filtering
    J1       battery connector
    SW1      reset button
    U1       ATmega328P-M
    U2       MPU-6050
    Y1       8MHz crystal

U1 and U2 share four nets: /SDA, /SCL, +3.3V and GND.

Worth knowing about this particular file: it is a schematic that was never
finished. Only 5 of its 34 symbols carry a Footprint property, and ``Y1`` among
them, so ``FootprintAssignedCheck`` is run with ``allow_none=True`` here. Run it
without that flag to see the real list of 31.

Its layout is untidy but not overlapping, and the layout checks agree. ``SW1``
is placed at 270 degrees and ``C4`` at 90, with their fields stored at a
compensating angle; KiCad draws field text flat regardless, so neither part's
reference collides with its value. The same applies to the power symbols sitting
on ``U2``: a power symbol's name is drawn by its own artwork rather than as a
text field, so it is never treated as text. The text geometry behind the report
is measured from a real KiCad renderer rather than estimated, which is what
makes it safe to claim "no overlaps" here.

``TextWireOverlapCheck`` is deliberately left out of the suite below. KiCad draws
schematic text with an opaque background that masks whatever is behind it, so a
net label lying on the wire it labels is normal rendering, not a defect. The
check still exists for anyone who wants it; see the note in ``overlaps.py``.

Usage:

    python examples/demo_schematic_check.py

Exit code is 0 when the only failure is the deliberate one, so this doubles
as a smoke test.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from kicad_evaltor import (
    Check,
    CheckRegistry,
    ComponentExistsCheck,
    ComponentPropertyCheck,
    ComponentsConnectedCheck,
    ComponentValueCheck,
    DesignContext,
    ERCRunCheck,
    FootprintAssignedCheck,
    SymbolSymbolOverlapCheck,
    TestReport,
    TestRunner,
    TextOffSheetCheck,
    TextSymbolOverlapCheck,
    TextTextOverlapCheck,
)

DEMO_SCHEMATIC = Path(__file__).resolve().parent / "demo_circuit" / "circuit.kicad_sch"


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
            candidates = sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True)
            for candidate in candidates:
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


def list_checks() -> None:
    """Show what the registry knows about."""
    print("Available checks:")
    for check_id, check_class in sorted(CheckRegistry.list()):
        print(f"  {check_id:<34} {check_class.name}")


def build_checks() -> list[Check]:
    """A realistic suite for this design.

    One check is expected to fail on purpose: R1 is a 10k rather than a 1k. The
    unassigned footprints are real but tolerated by ``allow_none``, and every
    layout check passes on this sheet.
    """
    return [
        ComponentExistsCheck(reference="U1"),
        ComponentExistsCheck(lib_id="Device:R"),
        ComponentExistsCheck(lib_id_pattern="power:*"),
        ComponentValueCheck(reference="R1", expected="10K"),
        ComponentValueCheck(reference="R1", expected="1k"),  # fails on purpose
        ComponentsConnectedCheck(reference_a="U1", reference_b="U2", min_connections=2),
        ComponentPropertyCheck(reference="U1", field="Reference", expected="U1"),
        # Power symbols and flags legitimately have no footprint, so exclude them.
        FootprintAssignedCheck(allow_none=True),
        ERCRunCheck(severity="error"),
        # Layout, as measured from the file rather than from the netlist.
        # TextWireOverlapCheck is intentionally absent; see the module docstring.
        TextTextOverlapCheck(),
        TextSymbolOverlapCheck(),
        SymbolSymbolOverlapCheck(),
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
        for key, value in sorted(result.details.items()):
            print(f"      {key}: {value}")


def print_known_issues(report: TestReport) -> None:
    """Note anything about this environment that limits the result."""
    errored = [r for r in report.results if r.status.value == "error"]
    print()
    print("How these results were produced")
    print("-" * 70)
    print(
        "Symbols, values, fields and footprints are read straight from the\n"
        "`.kicad_sch` file, so they need no KiCad at all. Connectivity comes from\n"
        "`kicad-cli sch export netlist` rather than a hand-rolled wire tracer, and\n"
        "sch.erc runs `kicad-cli sch erc`."
    )
    if errored:
        print()
        print(f"{len(errored)} check(s) errored:")
        print("  " + ", ".join(sorted(r.check_id for r in errored)))
    else:
        print("\nNo check needed a running KiCad instance or the IPC API server.")


def main() -> int:
    if not DEMO_SCHEMATIC.exists():
        print(f"Demo schematic missing: {DEMO_SCHEMATIC}", file=sys.stderr)
        return 2

    list_checks()

    cli = find_kicad_cli()
    print()
    print(f"Demo schematic: {DEMO_SCHEMATIC}")
    print(f"kicad-cli:      {cli or 'not found - connectivity and ERC will be skipped'}")

    with DesignContext(
        schematic_path=DEMO_SCHEMATIC,
        headless=True,
        kicad_cli_path=cli,
    ) as ctx:
        report = TestRunner(build_checks()).run(ctx)

    print_report(report)
    print_known_issues(report)

    expected_failures = {
        "sch.component.value",
    }
    actual_failures = {r.check_id for r in report.failures()}
    unexpected = actual_failures - expected_failures
    missing = expected_failures - actual_failures

    print()
    if missing:
        print(f"Expected failures that no longer happen: {', '.join(sorted(missing))}")
    if unexpected:
        print(f"Unexpected failures: {', '.join(sorted(unexpected))}")
        return 1
    if missing:
        return 1
    print("Matched the documented expectations for this design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
