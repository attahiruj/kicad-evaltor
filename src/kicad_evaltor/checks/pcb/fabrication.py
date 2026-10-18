"""Fabrication outputs, made by kicad-cli exactly as a fab house would get them."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import ClassVar

from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult
from kicad_evaltor.checks.registry import register
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import report_workspace


@dataclass
class FabExportsParams(CheckParams):
    timeout: float | None = None


@register
class FabExportsCheck(Check[FabExportsParams]):
    """Gerbers, drill files and a position file can all be generated.

    Each copper layer and the board edge must come out as a Gerber, and a drill
    file must be written when the board has holes. The files are made in a
    scratch folder and thrown away.
    """

    id = "pcb.fab.exports"
    name = "Fabrication Exports"
    description = "Checks that Gerber, drill and position files can be generated"
    category = CheckCategory.PCB
    Params: ClassVar[type[FabExportsParams]] = FabExportsParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        board = ctx.board
        board_path = str(ctx.board_path.resolve())
        timeout = self.params.timeout
        issues: list[str] = []
        with report_workspace("fab") as workdir:
            gerbers = workdir / "gerbers"
            drill = workdir / "drill"
            gerbers.mkdir()
            drill.mkdir()
            steps = {
                "gerbers": [
                    "pcb",
                    "export",
                    "gerbers",
                    "--no-protel-ext",
                    "-o",
                    f"{gerbers}/",
                    board_path,
                ],
                "drill": ["pcb", "export", "drill", "-o", f"{drill}/", board_path],
                "position file": [
                    "pcb",
                    "export",
                    "pos",
                    "--format",
                    "csv",
                    "--units",
                    "mm",
                    "-o",
                    str(workdir / "pos.csv"),
                    board_path,
                ],
            }
            for step, args in steps.items():
                result = ctx.run_kicad_cli(args, timeout=timeout)
                if result.timed_out:
                    return CheckResult.error(self.id, f"{step} export timed out", timed_out=True)
                if not result.success:
                    issues.append(f"{step} export failed: {result.stderr.strip()}")

            written = sorted(p.name for p in gerbers.iterdir())
            expected = [*board.copper_layers, "Edge.Cuts"]
            missing = [
                layer
                for layer in expected
                if not any(layer.replace(".", "_") in name for name in written)
            ]
            if missing:
                issues.append(f"no Gerber for {', '.join(missing)}")
            has_holes = bool(board.vias) or any(
                pad.drill for fp in board.footprints for pad in fp.pads
            )
            drills = sorted(p.name for p in drill.iterdir())
            if has_holes and not any(name.endswith(".drl") for name in drills):
                issues.append("no drill file written")

        details = {"gerbers": written, "drill_files": drills}
        if issues:
            return CheckResult.fail(self.id, "; ".join(issues), **details)
        return CheckResult.pass_(
            self.id, f"Wrote {len(written)} Gerbers and {len(drills)} drill file(s)", **details
        )


@dataclass
class PositionFileParams(CheckParams):
    expected_references: list[str] | None = None
    """Exactly the parts the file must list. None means every SMD footprint not
    marked DNP must be listed, and anything else may be."""
    timeout: float | None = None


@register
class PositionFileCheck(Check[PositionFileParams]):
    """The pick-and-place file lists the parts that must be machine-placed.

    The file is KiCad's own (``kicad-cli pcb export pos``, DNP parts left out).
    By default every SMD footprint must appear in it: one that does not, usually
    because it was marked "exclude from position files", would be left off an
    assembled board. Given ``expected_references``, the file must list exactly
    those parts.
    """

    id = "pcb.fab.position_file"
    name = "Position File"
    description = "Checks that the pick-and-place file lists the parts to assemble"
    category = CheckCategory.PCB
    Params: ClassVar[type[PositionFileParams]] = PositionFileParams

    def run(self, ctx: DesignContext) -> CheckResult:
        if not ctx.has_board():
            return CheckResult.skip(self.id, "No board available")
        board_path = str(ctx.board_path.resolve())
        with report_workspace("pos") as workdir:
            out = workdir / "pos.csv"
            result = ctx.run_kicad_cli(
                [
                    "pcb",
                    "export",
                    "pos",
                    "--format",
                    "csv",
                    "--units",
                    "mm",
                    "--exclude-dnp",
                    "-o",
                    str(out),
                    board_path,
                ],
                timeout=self.params.timeout,
            )
            if result.timed_out:
                return CheckResult.error(self.id, "position export timed out", timed_out=True)
            if not result.success or not out.exists():
                return CheckResult.error(self.id, f"position export failed: {result.stderr}")
            rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
        listed = {row.get("Ref", "") for row in rows} - {""}
        exact = self.params.expected_references is not None
        if exact:
            expected = set(self.params.expected_references or ())
        else:
            expected = {fp.reference for fp in ctx.board.footprints if fp.smd and not fp.dnp}
        missing = sorted(expected - listed)
        extra = sorted(listed - expected) if exact else []
        details = {"listed": sorted(listed), "missing": missing, "unexpected": extra}
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {', '.join(missing)}")
            if extra:
                parts.append(f"unexpected {', '.join(extra)}")
            return CheckResult.fail(self.id, "Position file " + "; ".join(parts), **details)
        return CheckResult.pass_(self.id, f"Position file lists {len(listed)} parts", **details)
