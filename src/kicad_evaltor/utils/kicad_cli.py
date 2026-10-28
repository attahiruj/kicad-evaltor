from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kicad_evaltor.findings import finding_origin, structured_items


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0


DEFAULT_CLI_TIMEOUT = 600.0
"""Seconds a kicad-cli call may run before it is abandoned.

ERC, DRC and netlist export on a large but valid design can take minutes on a
busy host, and abandoning one early reports a tooling failure as if it were a
design failure, so the default errs long. Pass ``timeout`` to tighten it.
"""


def find_kicad_cli() -> Path | None:
    """kicad-cli on ``PATH``, else in KiCad's standard install folders, newest first.

    KiCad does not put itself on ``PATH`` on Windows or macOS, so the install
    folders are searched too.
    """
    found = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if found:
        return Path(found)
    candidates: list[Path] = []
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base and (Path(base) / "KiCad").is_dir():
                candidates.extend(
                    sorted(
                        (Path(base) / "KiCad").glob("*/bin/kicad-cli.exe"),
                        key=lambda p: _version_key(p.parent.parent.name),
                        reverse=True,
                    )
                )
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"))
    return next((c for c in candidates if c.is_file()), None)


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name)) or (0,)


CHILD_ENV_DROPPED = ("__GLX_VENDOR_LIBRARY_NAME",)
"""Variables removed from the environment kicad-cli and KiCad's Python start with.

``__GLX_VENDOR_LIBRARY_NAME`` picks the OpenGL vendor library (``nvidia`` on
hosts set up for GPU rendering). kicad-cli and the ``pcbnew`` module need no
OpenGL for reports or board reads, yet KiCad 10.0.7 loads the named vendor
library while it starts and segfaults when it is ``nvidia``, before writing
anything. The child is only ever a headless run, so the variable is dropped
rather than passed on.
"""


def kicad_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment to start kicad-cli or KiCad's Python with.

    A copy of ``base`` (default: this process's environment) without the
    variables in :data:`CHILD_ENV_DROPPED`. The host process is not changed.
    """
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if k not in CHILD_ENV_DROPPED}


def describe_failure(result: SubprocessResult) -> str:
    """Why a kicad-cli run failed, in words, never an empty string.

    A run killed by a signal (out of memory under load, a crash) writes nothing
    to stderr or stdout, so the exit status is often the only reason there is.
    It is named, then the last line of stderr and of stdout follow when present.
    """
    if result.timed_out:
        return result.stderr.strip() or "timed out"
    parts = [_exit_status(result.returncode)]
    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in stream.splitlines() if line.strip()]
        if lines:
            parts.append(lines[-1])
    return "; ".join(parts)


def failure_details(result: SubprocessResult) -> dict[str, Any]:
    """The facts behind a failed run, for a result's ``details``.

    ``returncode`` is negative when a signal ended the process (POSIX), and -1
    when it never ran or timed out. ``stderr`` and ``stdout`` are kept to their
    last 2000 characters.
    """
    details: dict[str, Any] = {
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "stderr": result.stderr[-2000:],
        "stdout": result.stdout[-2000:],
    }
    if result.returncode < -1:
        details["signal"] = _signal_name(-result.returncode)
    return details


def _exit_status(code: int) -> str:
    if code < -1:
        return f"killed by signal {-code} ({_signal_name(-code)})"
    if code > 0xFFFF:
        # Windows reports a crash as an NTSTATUS such as 0xC0000005.
        return f"exit code {code:#x}"
    return f"exit code {code}"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def run_kicad_cli(
    args: list[str], cwd: Path | str | None = None, timeout: float = DEFAULT_CLI_TIMEOUT
) -> SubprocessResult:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
            env=kicad_child_env(),
        )
        return SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired as e:
        return SubprocessResult(
            returncode=-1,
            stdout=(
                e.stdout.decode("utf-8", errors="replace")
                if isinstance(e.stdout, bytes)
                else e.stdout or ""
            ),
            stderr=f"Command timed out after {timeout:g}s",
            timed_out=True,
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


class InvalidReport(ValueError):
    """A tool report cannot establish whether a design passed."""


def _report_data(text: str, *, erc: bool) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidReport("Invalid report JSON") from exc
    if not isinstance(data, dict):
        raise InvalidReport("Report must be an object")
    keys: tuple[str, ...] = ("violations", "erc_violation", "drc_violation")
    if erc and "sheets" in data:
        sheets = data["sheets"]
        if not isinstance(sheets, list) or any(not isinstance(s, dict) for s in sheets):
            raise InvalidReport("Invalid report sheets")
        groups = [s.get("violations") for s in sheets]
    else:
        keys = keys if erc else (*DRC_SOURCES, "drc_violation")
        groups = [data[k] for k in keys if k in data]
        if not groups:
            raise InvalidReport("Report has no recognized findings list")
    for group in groups:
        if not isinstance(group, list) or any(not isinstance(v, dict) for v in group):
            raise InvalidReport("Invalid findings list")
        for finding in group:
            if finding.get("severity") not in ("error", "warning", "ignore", "info", "exclusion"):
                raise InvalidReport("Missing or unknown finding severity")
    return data


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
    Invalid or unrecognized input raises InvalidReport.
    """
    data = _report_data(json_output, erc=True)

    sheets = data.get("sheets")
    if isinstance(sheets, list):
        return _violations_from_sheets(sheets)
    return _violations_from(data)


DRC_SOURCES = ("violations", "unconnected_items", "schematic_parity")
"""The three lists a KiCad DRC report keeps its findings in.

``violations`` holds rule breaches. A connection that is not routed is not in
it: KiCad 10 lists those under ``unconnected_items``, and differences from the
schematic under ``schematic_parity``. A parser that reads only ``violations``
passes a board with nothing routed on it.
"""


def parse_drc_report(json_output: str) -> list[dict[str, Any]]:
    """Every finding in a KiCad DRC JSON report, each tagged with its ``source``.

    ``source`` names the list it came from (see :data:`DRC_SOURCES`). Reports
    from KiCad 8 and 9, which have only the one list, read the same way.
    Invalid or unrecognized input raises InvalidReport.
    """
    data = _report_data(json_output, erc=False)
    found: list[dict[str, Any]] = []
    for source in DRC_SOURCES:
        entries = data.get(source)
        if isinstance(entries, list):
            found.extend(
                {**entry, "source": source} for entry in entries if isinstance(entry, dict)
            )
    if not found and "violations" not in data:
        found = [{**v, "source": "violations"} for v in _violations_from(data)]
    return found


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


def violation_details(
    violation: Mapping[str, Any], lib_ids: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The reportable fields of one violation.

    KiCad calls the human text ``description`` and puts every position in
    ``items[*].pos``; there is no ``message`` key and no ``at`` key, so reading
    those names yields ``None`` for every violation in the report. Projecting
    here keeps one spelling for both checks. ``sheet`` is present only for ERC,
    which is the per-sheet report, and ``source`` only for DRC.

    Each item keeps the report's keys and gains the structured fields of
    :func:`kicad_evaltor.findings.parse_item` (``kind``, ``reference``,
    ``pin``, ``pin_name``, ``pin_type``...), plus the symbol's ``lib_id`` when
    ``lib_ids`` (reference to library id) is given. ``origin`` says whether
    the finding comes from the design or from the library configuration it
    was checked in (see :func:`kicad_evaltor.findings.finding_origin`).
    ``hidden_by`` and ``hidden_kind`` are copied when a check that re-enabled
    silenced rules set them.
    """
    details: dict[str, Any] = {
        "type": violation.get("type"),
        "severity": violation.get("severity"),
        "description": violation.get("description"),
        "items": structured_items(violation, lib_ids),
        "origin": finding_origin(violation),
    }
    if "sheet" in violation:
        details["sheet"] = violation["sheet"]
    if "source" in violation:
        details["source"] = violation["source"]
    for key in ("hidden_by", "hidden_kind", "project_severity"):
        if key in violation:
            details[key] = violation[key]
    return details


REPORT_CWD: ContextVar[Path | None] = ContextVar("report_cwd", default=None)


@contextmanager
def report_workspace(kind: str) -> Iterator[Path]:
    """Run a kicad-cli report command inside a throwaway directory.

    kicad-cli writes its JSON report, and sometimes ``*.kicad_prl`` or cache
    files, into the current working directory. Redirecting the CWD keeps those
    out of the user's project and repository.

    The directory is passed to subprocesses through a context-local value;
    the host process working directory is never changed.
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"kicad-evaltor-{kind}-"))
    token = REPORT_CWD.set(workdir)
    try:
        yield workdir
    finally:
        REPORT_CWD.reset(token)
        shutil.rmtree(workdir, ignore_errors=True)
