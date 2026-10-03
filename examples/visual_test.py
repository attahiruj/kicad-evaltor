#!/usr/bin/env python3
"""Run the layout checks against a sheet that documents its own defects.

``examples/demo_circuit/visual_test.kicad_sch`` carries seven annotation texts
saying what is wrong with it. This runs all five layout checks and reports which
of those a check catches, which needs a margin to appear, and why the rest are
not reported.

Two of the seven turn out not to be true of the sheet, and the report says so
rather than staying quiet: J1's label clears its own body, and so does SW2's
value. Reporting a claim that does not hold would be the same mistake as missing
one that does.

No KiCad needed: the layout checks read the file directly.

Usage:

    python examples/visual_test.py            human report
    python examples/visual_test.py --json     failures as JSON

Exit code is 0 while the sheet still produces the findings it documents.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from kicad_evaltor import (
    Check,
    CheckRegistry,
    CheckResult,
    DesignContext,
    TestReport,
    TestRunner,
)
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import extract
from kicad_evaltor.sexpr import children, value_of

SCHEMATIC = Path(__file__).resolve().parent / "demo_circuit" / "visual_test.kicad_sch"

LAYOUT_CHECKS = (
    "sch.layout.text_text_overlap",
    "sch.layout.text_symbol_overlap",
    "sch.layout.text_wire_overlap",
    "sch.layout.symbol_symbol_overlap",
    "sch.layout.text_off_sheet",
)

# Finding counts at margin 0. Pins and body outlines are sampled rather than
# solved, so these are exact for this file.
EXPECTED_COUNTS = {
    "sch.layout.text_text_overlap": 1,
    "sch.layout.text_symbol_overlap": 7,
    "sch.layout.text_wire_overlap": 2,
    "sch.layout.symbol_symbol_overlap": 1,
    "sch.layout.text_off_sheet": 0,
}


@dataclass(frozen=True)
class Documented:
    """One caption from the sheet, and what the checks make of it.

    ``key`` identifies the caption by a distinctive fragment, so rewrapping the
    text does not break the match. ``captured`` pairs a check id with a prefix of
    the finding that check is expected to report. An empty ``captured`` with a
    ``reason`` means the claim is out of scope by design; an empty ``captured``
    with a ``note`` means the claim simply is not true of this sheet.
    """

    key: str
    captured: tuple[tuple[str, str], ...] = ()
    reason: str = ""
    note: str = ""


DOCUMENTED = (
    Documented(
        "+3.3V text on J2 pin 2",
        captured=(("sch.layout.text_wire_overlap", "+3.3V overlaps wire"),),
    ),
    Documented(
        "`RESET` net label overlaps",
        captured=(
            ("sch.layout.text_symbol_overlap", "RESET overlaps SW2"),
            ("sch.layout.text_symbol_overlap", "SW2.Value overlaps PWR012"),
        ),
        reason="its third claim, RESET on the SW_Push text, misses by 0.13 mm: margin territory",
    ),
    Documented(
        "J1 symbol label overlaps symbol shape",
        note="J1.Reference clears its own body by 0.58mm, so the claim does not hold",
    ),
    Documented(
        "No connect flag placed on",
        captured=(("sch.layout.text_symbol_overlap", "U2.Reference overlaps U2"),),
        reason="the no-connect flag is not extracted as an item at all",
    ),
    Documented(
        "SW_Push value overlaps own symbol",
        note="SW_Push clears its own body; it is the reference that straddles",
    ),
    Documented(
        "PWR_FLAG text and symbol overlaps",
        reason="a power symbol's name is artwork, not text",
    ),
    Documented(
        "R1 value (10K) intersects own symbol shape",
        captured=(
            ("sch.layout.text_symbol_overlap", "RESET overlaps R1"),
            ("sch.layout.text_symbol_overlap", "R1.Value overlaps R1"),
        ),
    ),
)


def build_checks() -> list[Check]:
    return [CheckRegistry.create(check_id) for check_id in LAYOUT_CHECKS]


def captions_in(path: Path) -> list[str]:
    schematic = FileSchematic(path)
    return [value_of(node, 1) for node in children(schematic.nodes(), "text")]


def findings(result: CheckResult) -> list[dict]:
    return list(result.details.get("overlaps", []))


def describe(finding: dict) -> str:
    return f"{finding['first']} overlaps {finding['second']}"


def block(text: str, indent: str, first: str | None = None) -> str:
    lines = text.strip("\n").splitlines()
    return "\n".join(
        f"{first if first is not None and n == 0 else indent}{line.rstrip()}"
        if line.strip()
        else ""
        for n, line in enumerate(lines)
    )


def named(label: str, properties: dict) -> str:
    """A label plus the properties it shows, minus whatever the label repeats."""
    shown = [
        (name, value)
        for name, value in properties.items()
        if value != label and not (name == "Reference" and label.startswith(value))
    ]
    if not shown:
        return label
    if len(shown) == 1:
        return f"{label} {quote(shown[0][1])}"
    return label + " " + " ".join(f"{name}={quote(value)}" for name, value in shown)


def quote(text: str) -> str:
    if "\n" in text:
        return f'"{text.splitlines()[0]}..." ({len(text.splitlines())} lines)'
    return f'"{text}"' if len(text) <= 40 else f'"{text[:37]}..."'


def print_header(captions: list[str]) -> None:
    schematic = FileSchematic(SCHEMATIC)
    scene = extract(schematic)
    width, height = schematic.sheet_size()
    print(SCHEMATIC)
    print(
        f"A4 {width:g}x{height:g}mm, {len(scene.components)} symbols, "
        f"{len(scene.texts)} texts, {len(scene.wires)} wires"
    )
    print(f"{len(captions)} captions documenting the defects, and what catches them")
    for index, caption in enumerate(captions, start=1):
        print(block(caption, "     ", first=f"  {index}. "))


def plural(count: int) -> str:
    return f"{count} overlap" if count == 1 else f"{count} overlaps"


def print_findings(report: TestReport) -> None:
    print()
    print("Findings at margin 0")
    print("-" * 78)
    for result in report.results:
        mark = "FAIL" if result.status.value == "fail" else "pass"
        print(f"  {mark}  {result.check_id}  ({plural(result.details.get('count', 0))})")
        for finding in findings(result):
            first = named(finding["first"], finding.get("first_properties", {}))
            second = named(finding["second"], finding.get("second_properties", {}))
            area = finding["area"]
            print(
                f"          {first} overlaps {second}"
                f"   {finding['width']:.2f}x{finding['height']:.2f}mm"
                f" at ({area[0]:.2f}, {area[1]:.2f})"
            )


def print_coverage(captions: list[str], report: TestReport) -> None:
    print()
    print("Coverage")
    print("-" * 78)
    reported = {r.check_id: [describe(f) for f in findings(r)] for r in report.results}
    for index, caption in enumerate(captions, start=1):
        documented = next((d for d in DOCUMENTED if d.key in caption), None)
        if documented is None:
            print(f"  {index}. ?? no DOCUMENTED entry describes this caption")
            continue
        if not documented.captured:
            detail = documented.note or f"out of scope: {documented.reason}"
            print(f"  {index}. {detail}")
            continue
        print(f"  {index}. caught")
        for check_id, prefix in documented.captured:
            hit = next((f for f in reported.get(check_id, []) if f.startswith(prefix)), None)
            print(f"       {hit or 'NOT CAUGHT':<44} {check_id}")
        if documented.reason:
            print(f"       {documented.reason}")


def run(checks: list[Check]) -> TestReport:
    with DesignContext(schematic_path=SCHEMATIC, headless=True, kicad_cli_path=None) as ctx:
        return TestRunner(checks).run(ctx)


def print_extras() -> None:
    """What a margin buys."""
    text_text = CheckRegistry.get("sch.layout.text_text_overlap")
    tight = run([text_text()])
    loose = run([text_text(margin=0.2)])
    before = {describe(f) for f in findings(tight.results[0])}
    gained = [f for f in findings(loose.results[0]) if describe(f) not in before]

    print()
    print(f"margin 0.2 finds {len(before) + len(gained)} text-text overlaps, not {len(before)}")
    for finding in gained:
        print(f"  gained {describe(finding)}  {finding['width']:.2f}x{finding['height']:.2f}mm")


def verify(report: TestReport) -> list[str]:
    problems = []
    for result in report.results:
        if result.status.value not in ("pass", "fail"):
            problems.append(f"{result.check_id} did not run: {result.status.value}")
            continue
        expected, actual = EXPECTED_COUNTS[result.check_id], result.details.get("count", 0)
        if actual != expected:
            problems.append(f"{result.check_id}: expected {expected}, got {actual}")
    reported = {r.check_id: [describe(f) for f in findings(r)] for r in report.results}
    for documented in DOCUMENTED:
        for check_id, prefix in documented.captured:
            if not any(f.startswith(prefix) for f in reported.get(check_id, [])):
                problems.append(f"{check_id}: no longer reports '{prefix}'")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print failures as JSON")
    args = parser.parse_args()

    if not SCHEMATIC.exists():
        print(f"Missing: {SCHEMATIC}", file=sys.stderr)
        return 2

    captions = captions_in(SCHEMATIC)
    report = run(build_checks())
    problems = verify(report)

    if args.json:
        # A format switch only: these checks are meant to fail, so the exit code
        # still reports whether the sheet matches its documented results.
        print(report.failures_json())
        return 1 if problems else 0

    print_header(captions)
    print()
    print(report.summary())
    print_findings(report)
    print_coverage(captions, report)
    print_extras()

    print()
    if problems:
        print("This sheet no longer matches its documented results:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("Every documented finding was caught, and nothing else changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
