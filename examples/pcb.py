#!/usr/bin/env python3
"""Run the board checks against a real KiCad 10 board.

``examples/demo_circuit/esp_data_logger_board`` is an ESP32-C6 data logger: a
50 mm square two-layer board with a TPS63020 buck-boost (U2), a microSD socket
(J3), a battery connector, four M3 mounting holes and a GND pour on both layers.
KiCad's own DRC is clean on it, with no violations, unrouted connections or
schematic parity issues. Every expectation below was read off the board.

The suite has two halves. The first holds the board to what is true of it and
must pass. The second runs checks that find real things on this board which
KiCad's DRC does not report, and prints them:

* 17 tracks on +3.7V are 0.3 mm, but the net's class (3.7V) asks for 0.5 mm.
  A net class width is a default in KiCad, not a rule, so DRC passes it.
* U2 has a power pin about 3.79 mm from its nearest capacitor, exceeding 1 mm.
* U2's exposed thermal pad (pad 15) has no net and no vias under it.
* 42 of the 43 footprints that are not fiducials hide their reference
  designator.

Needs KiCad 10 installed: kicad-cli runs DRC and the fabrication exports, and
KiCad's bundled Python (pcbnew) supplies courtyards, pad shapes, the outline
and zone fill.

Usage:

    python examples/pcb.py

Exit code is 0 when the first half passes, so this doubles as a smoke test.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kicad_evaltor import (
    BoardOutlineCheck,
    DesignContext,
    DRCRunCheck,
    FabExportsCheck,
    FootprintMatchesSchematicCheck,
    FootprintNearCheck,
    LengthMatchCheck,
    MountingHolesCheck,
    PlaneCoverageCheck,
    PowerPourCheck,
    ReferenceVisibleCheck,
    RoutingCompleteCheck,
    SchematicParityCheck,
    TestRunner,
    ThermalViasCheck,
    TraceLengthCheck,
    TraceWidthCheck,
    ViaRulesCheck,
)

PROJECT = (
    Path(__file__).resolve().parent
    / "demo_circuit"
    / "esp_data_logger_board"
    / "esp_data_logger_board.kicad_pro"
)

MUST_PASS = [
    DRCRunCheck(severity="all"),
    RoutingCompleteCheck(),
    SchematicParityCheck(),
    BoardOutlineCheck(width_mm=50, height_mm=50),
    MountingHolesCheck(min_count=4, diameter_mm=3.2, min_edge_distance_mm=3),
    PowerPourCheck(net_name="GND", min_area_mm2=3000),
    PlaneCoverageCheck(net="GND", layer="B.Cu", min_fraction=0.75),
    TraceLengthCheck(from_pad="U2.1", to_pad="C9.1", max_length_mm=5),
    LengthMatchCheck(pattern="/SD_*", max_skew_mm=25),
    ViaRulesCheck(),
    FootprintMatchesSchematicCheck(),
    FabExportsCheck(),
]

FINDINGS = [
    FootprintNearCheck(decoupling="U2", max_distance_mm=1),
    TraceWidthCheck(match_netclass=True),
    ThermalViasCheck(reference="U2", min_vias=4),
    ReferenceVisibleCheck(exclude=["FID*"]),
]


def main() -> int:
    with DesignContext(project_path=PROJECT) as ctx:
        report = TestRunner(MUST_PASS).run(ctx)
        print(report.summary())
        print("\nWhat DRC does not report:")
        for check in FINDINGS:
            result = check.run(ctx)
            print(f"  {check.id}: {result.status.value} - {result.message}")
    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
