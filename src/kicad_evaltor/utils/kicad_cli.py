from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
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

    KiCad writes violations under ``violations``; the older ``erc_violation``
    key is still accepted. Unparseable input yields an empty list.
    """
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError:
        return []
    return _violations_from(data)


def parse_drc_report(json_output: str) -> list[dict[str, Any]]:
    """Extract DRC violations from a KiCad DRC JSON report.

    KiCad writes violations under ``violations``; the older ``drc_violation``
    key is still accepted. Unparseable input yields an empty list.
    """
    try:
        data = json.loads(json_output)
    except json.JSONDecodeError:
        return []
    return _violations_from(data)


def _violations_from(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for key in ("violations", "erc_violation", "drc_violation"):
        found = data.get(key)
        if isinstance(found, list):
            return found
    return []


def filter_violations_by_severity(
    violations: list[dict[str, Any]], severity: str
) -> list[dict[str, Any]]:
    if severity == "all":
        return violations
    return [v for v in violations if v.get("severity", "").lower() == severity.lower()]


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
