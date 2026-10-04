from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


def run_kicad_cli(
    args: list[str], cwd: Path | str | None = None, timeout: int = 60
) -> SubprocessResult:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired as e:
        return SubprocessResult(
            returncode=-1,
            stdout=e.stdout.decode() if e.stdout else "",
            stderr=f"Command timed out after {timeout}s",
        )
    except FileNotFoundError:
        return SubprocessResult(
            returncode=-1,
            stdout="",
            stderr="kicad-cli not found. Ensure it's installed and in PATH.",
        )
    except Exception as e:
        return SubprocessResult(
            returncode=-1,
            stdout="",
            stderr=str(e),
        )


def parse_erc_report(json_output: str) -> list[dict[str, Any]]:
    """Extract ERC violations from a KiCad ERC JSON report.

    KiCad 10 reports per sheet: the top level carries only metadata
    (``$schema``, ``kicad_version``, ``ignored_checks`` and so on) and every
    violation lives in ``sheets[*].violations``. There is no top-level
    ``violations`` key at all, so a parser that looks for one finds nothing and
    reports a schematic with errors on it as clean. KiCad 8 and 9 wrote a single
    flat list instead, so both shapes are read.

    Each returned violation carries the ``sheet`` path it was found under, which
    is the only thing that says which sheet of a hierarchy the error is on.
    Unparseable input yields an empty list.
    """
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    sheets = data.get("sheets")
    if isinstance(sheets, list):
        return _violations_from_sheets(sheets)
    return _violations_from(data)


def parse_drc_report(json_output: str) -> list[dict[str, Any]]:
    """Extract DRC violations from a KiCad DRC JSON report.

    Unlike ERC, this one is not per sheet: KiCad 10 still writes one top-level
    ``violations`` list. Unparseable input yields an empty list.
    """
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError:
        return []
    return _violations_from(data)


def _violations_from_sheets(sheets: list[Any]) -> list[dict[str, Any]]:
    """Flatten ``sheets[*].violations`` into one list, tagging each with its sheet."""
    found: list[dict[str, Any]] = []
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        path = sheet.get("path", "")
        violations = sheet.get("violations")
        if not isinstance(violations, list):
            continue
        found.extend(
            {**violation, "sheet": path} for violation in violations if isinstance(violation, dict)
        )
    return found


def _violations_from(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("violations", "erc_violation", "drc_violation"):
        found = data.get(key)
        if isinstance(found, list):
            return [violation for violation in found if isinstance(violation, dict)]
    return []


def filter_violations_by_severity(
    violations: list[dict[str, Any]], severity: str
) -> list[dict[str, Any]]:
    if severity == "all":
        return violations
    return [v for v in violations if v.get("severity", "").lower() == severity.lower()]


def violation_details(violation: Mapping[str, Any]) -> dict[str, Any]:
    """The reportable fields of one violation.

    KiCad calls the human text ``description`` and puts every position in
    ``items[*].pos``; there is no ``message`` key and no ``at`` key, so reading
    those names yields ``None`` for every violation in the report. Projecting
    here keeps one spelling for both checks. ``sheet`` is present only for ERC,
    which is the per-sheet report.
    """
    details: dict[str, Any] = {
        "type": violation.get("type"),
        "severity": violation.get("severity"),
        "description": violation.get("description"),
        "items": violation.get("items", []),
    }
    if "sheet" in violation:
        details["sheet"] = violation["sheet"]
    return details


@contextmanager
def report_workspace(kind: str) -> Iterator[Path]:
    """Run a kicad-cli report command inside a throwaway directory.

    kicad-cli writes its JSON report, and sometimes ``*.kicad_prl`` or cache
    files, into the current working directory. Redirecting the CWD keeps those
    out of the user's project and repository.

    Note: this changes the process-wide CWD, so it is not safe to use from
    threads. ``DesignContext.run_kicad_cli`` is likewise process-global.
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"kicad-evaltor-{kind}-"))
    previous = Path.cwd()
    os.chdir(workdir)
    try:
        yield workdir
    finally:
        os.chdir(previous)
        shutil.rmtree(workdir, ignore_errors=True)
