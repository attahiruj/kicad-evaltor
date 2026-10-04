#!/usr/bin/env python3
"""Report what the layout checks find on a sheet.

Runs the ``sch.layout.*`` and ``sch.noconnect.*`` checks against
``examples/demo_circuit/visual.kicad_sch``, or against any other sheet
named on the command line, and lists each finding with where it sits. The checks
read the sheet's geometry straight from the file, so no KiCad is needed.

Usage:

    python examples/visual.py [sheet.kicad_sch]     human report
    python examples/visual.py --json                failures as JSON

Findings are what this sheet is for, so the exit code is 0 whenever the script
itself ran.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kicad_evaltor import (
    Check,
    CheckRegistry,
    CheckResult,
    DesignContext,
    TestReport,
    TestRunner,
    TestStatus,
)
from kicad_evaltor.schematic_file import FileSchematic
from kicad_evaltor.schematic_items import extract
from kicad_evaltor.sexpr import child, value_of

DEMO_SHEET = Path(__file__).resolve().parent / "demo_circuit" / "visual.kicad_sch"

LAYOUT_PREFIXES = ("sch.layout.", "sch.noconnect.")

# Left out by default because it is opt-in: KiCad masks wires behind text, so a net
# label lying on the wire it labels is normal rendering rather than a defect, and this
# fires on those. See the note on the check.
OPT_IN = ("sch.layout.text_wire_overlap",)


def layout_check_ids() -> list[str]:
    """The layout checks the registry knows, so a new one is picked up here too."""
    return sorted(
        check_id
        for check_id, _ in CheckRegistry.list()
        if check_id.startswith(LAYOUT_PREFIXES) and check_id not in OPT_IN
    )


def build_checks() -> list[Check]:
    return [CheckRegistry.create(check_id) for check_id in layout_check_ids()]


def finding_lines(result: CheckResult) -> list[str]:
    """One line per finding, shaped by whichever payload the check reported."""
    if "floating_flags" in result.details:
        return [
            *(f"flag at [{x:g}, {y:g}] marks no pin" for x, y in result.details["floating_flags"]),
            *(f"unmarked pin {pin}" for pin in result.details.get("unmarked_pins", [])),
        ]
    if "overlaps" in result.details:
        return [_overlap_line(finding) for finding in result.details["overlaps"]]
    return [] if result.status == TestStatus.PASS else [result.message]


def finding_count(result: CheckResult) -> int:
    """How many findings there were. The check's own count wins over the lines."""
    count = result.details.get("count")
    if isinstance(count, int):
        return count
    return len(finding_lines(result)) if result.status == TestStatus.FAIL else 0


def _overlap_line(finding: dict) -> str:
    """One collision, with the properties its two items show on the sheet."""
    first, second = finding.get("first"), finding.get("second")
    if first is None or second is None:
        return named(finding.get("label", "?"), finding.get("properties", {})) + _placement(finding)
    return (
        f"{named(first, finding.get('first_properties', {}))} overlaps "
        f"{named(second, finding.get('second_properties', {}))}"
    ) + _placement(finding)


def _placement(finding: dict) -> str:
    """The box a finding sits in, and the gap when the two items only nearly touch."""
    box = finding.get("area") or finding.get("bbox")
    if not isinstance(box, list | tuple) or len(box) < 4:
        return ""
    min_x, min_y, max_x, max_y = (float(value) for value in box[:4])
    text = f"   {max_x - min_x:.2f}x{max_y - min_y:.2f}mm at ({min_x:.2f}, {min_y:.2f})"
    gap = finding.get("gap")
    return f"{text}, gap {gap:.2f}mm" if gap else text


def named(label: str, properties: dict) -> str:
    """A label plus the properties it shows, minus whatever the label repeats."""
    shown = [
        (name, value)
        for name, value in properties.items()
        if value != label and not (name == "Reference" and label.startswith(value))
    ]
    label = brief(label)
    if not shown:
        return label
    if len(shown) == 1:
        return f"{label} {quote(shown[0][1])}"
    return label + " " + " ".join(f"{name}={quote(value)}" for name, value in shown)


def brief(text: str) -> str:
    """The first line of a label, so a caption cannot break the line-per-finding layout."""
    lines = text.splitlines()
    if len(lines) > 1:
        return f"{lines[0]}... ({len(lines)} lines)"
    return lines[0] if lines else ""


def quote(text: str) -> str:
    if "\n" in text:
        return f'"{brief(text)}"'
    return f'"{text}"' if len(text) <= 40 else f'"{text[:37]}..."'


def print_sheet(path: Path) -> None:
    schematic = FileSchematic(path)
    scene = extract(schematic)
    width, height = schematic.sheet_size()
    paper = value_of(child(schematic.nodes(), "paper")) or "unknown paper"
    print(path)
    print(
        f"{paper} {width:g}x{height:g}mm, {len(scene.components)} symbols, "
        f"{len(scene.texts)} texts, {len(scene.wires)} wires"
    )


def print_findings(report: TestReport, checks: list[Check]) -> None:
    names = {check.id: check.name for check in checks}
    print()
    print("Findings")
    print("-" * 78)
    for result in report.results:
        mark = "FAIL" if result.status == TestStatus.FAIL else result.status.value
        lines = finding_lines(result)
        count = finding_count(result)
        name = names.get(result.check_id, "")
        print(f"  {mark:<5} {result.check_id}  {name}  ({count} findings)")
        for line in lines:
            print(f"          {line}")
        if count > len(lines):
            print(f"          ... {count - len(lines)} more not detailed")


def print_totals(report: TestReport) -> None:
    clean = sum(1 for result in report.results if result.status == TestStatus.PASS)
    total = sum(finding_count(result) for result in report.results)
    print()
    print(
        f"{len(report.results)} checks, {clean} of them with no findings, {total} findings in all"
    )


def run(path: Path, checks: list[Check]) -> TestReport:
    with DesignContext(schematic_path=path, headless=True, kicad_cli_path=None) as ctx:
        return TestRunner(checks).run(ctx)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sheet", nargs="?", type=Path, default=DEMO_SHEET, help="sheet to read")
    parser.add_argument("--json", action="store_true", help="print failures as JSON")
    args = parser.parse_args()

    if not args.sheet.exists():
        print(f"Missing: {args.sheet}", file=sys.stderr)
        return 2

    checks = build_checks()
    report = run(args.sheet, checks)

    if args.json:
        print(report.failures_json())
        return 0

    print_sheet(args.sheet)
    print()
    print(report.summary())
    print_findings(report, checks)
    print_totals(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
