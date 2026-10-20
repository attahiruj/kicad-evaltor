"""Record KiCad's DRC reports and pcbnew geometry for the example board.

The board checks' unit tests run without KiCad by replaying what KiCad said
about a real board: ``examples/demo_circuit/esp_data_logger_board``. Failing
cases are the same board with one thing broken, made on a copy with
:mod:`kicad_evaltor.edit`, so every recording is of a file KiCad wrote:

* ``base``      the board as it is (DRC-clean)
* ``unrouted``  every /SD_CLK track deleted
* ``parity``    R13 removed from the board
* ``overlap``   C5 moved onto C4
* ``offboard``  TP3 moved off the board
* ``tuning``    a custom rule (``.kicad_dru``) capping /SD_CLK at 10 mm and the
                SD bus skew at 5 mm, so KiCad's own length rules fire

Re-record after changing the board or upgrading KiCad::

    python tools/record_pcb_fixtures.py

``tests/test_pcb_integration.py`` re-runs KiCad and fails if a recording has
drifted from what KiCad now says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from kicad_evaltor.edit import (
    load_document,
    move_footprint,
    remove_footprint,
    remove_net_tracks,
    save_document,
)
from kicad_evaltor.pcbnew_bridge import dump_board_json
from kicad_evaltor.toolchain import probe_toolchain, source_identity
from kicad_evaltor.utils.kicad_cli import find_kicad_cli

ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = ROOT / "examples" / "demo_circuit" / "esp_data_logger_board"
NAME = "esp_data_logger_board"
OUT = ROOT / "tests" / "fixtures" / "pcb"

VARIANTS: dict[str, Callable[[list], object]] = {
    "base": lambda _doc: None,
    "unrouted": lambda doc: remove_net_tracks(doc, "/SD_CLK"),
    "parity": lambda doc: remove_footprint(doc, "R13"),
    "overlap": lambda doc: move_footprint(doc, "C5", 109.2, 78.0),
    "offboard": lambda doc: move_footprint(doc, "TP3", 160.0, 92.85),
    "tuning": lambda _doc: None,
}

# Custom DRC rules written beside a variant's board, as a project's .kicad_dru.
RULES = {
    "tuning": """(version 1)
(rule sd_clk_length
  (condition "A.NetName == '/SD_CLK'")
  (constraint length (max 10mm)))
(rule sd_bus_skew
  (condition "A.hasNetclass('SD')")
  (constraint skew (max 5mm)))
""",
}
GEOMETRY_VARIANTS = ("base", "offboard")


def make_variant(name: str, folder: Path) -> Path:
    """Copy the example project into ``folder`` and break it as ``name`` says."""
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copy(BOARD_DIR / f"{NAME}{suffix}", folder / f"{NAME}{suffix}")
    if name in RULES:
        (folder / f"{NAME}.kicad_dru").write_text(RULES[name], encoding="utf-8")
    document = load_document(BOARD_DIR / f"{NAME}.kicad_pcb")
    VARIANTS[name](document)
    return save_document(document, folder / f"{NAME}.kicad_pcb")


def run_drc(cli: Path, board: Path) -> dict:
    report = board.with_name("drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--schematic-parity",
            "--refill-zones",
            "-o",
            str(report),
            str(board),
        ],
        check=True,
        capture_output=True,
        cwd=board.parent,
    )
    data: dict = json.loads(report.read_text(encoding="utf-8"))
    # Absolute temp paths and the date change on every run; they say nothing.
    data["source"] = f"{NAME}.kicad_pcb"
    data.pop("date", None)
    return data


def main() -> int:
    cli = find_kicad_cli()
    if cli is None:
        print("kicad-cli not found", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kicad-evaltor-record-") as scratch:
        for name in VARIANTS:
            board = make_variant(name, Path(scratch) / name)
            drc = run_drc(cli, board)
            (OUT / f"{name}.drc.json").write_text(json.dumps(drc, indent=1) + "\n", "utf-8")
            if name in GEOMETRY_VARIANTS:
                geometry = dump_board_json(board, fill_zones=True)
                (OUT / f"{name}.geometry.json").write_text(
                    json.dumps(geometry, sort_keys=True) + "\n", "utf-8"
                )
            counts = {
                k: len(drc.get(k, []))
                for k in ("violations", "unconnected_items", "schematic_parity")
            }
            print(f"{name}: {counts}")
    identities = []
    for path in sorted(BOARD_DIR.glob(f"{NAME}.kicad_*")):
        if path.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro"):
            identity = source_identity(path)
            identity["path"] = path.relative_to(ROOT).as_posix()
            identities.append(identity)
    manifest = {
        "toolchain": asdict(probe_toolchain(str(cli), include_pcbnew=True, timeout=60)),
        "sources": identities,
        "variants": list(VARIANTS),
        "artifacts": {
            p.name: source_identity(p)["sha256"]
            for p in sorted(OUT.glob("*.json"))
            if p.name != "manifest.json"
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
