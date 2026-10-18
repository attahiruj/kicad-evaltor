"""Find and run the Python interpreter that KiCad ships with ``pcbnew``.

``pcbnew`` is a compiled extension built against KiCad's own CPython, so it
cannot be imported into the interpreter running this library. Board geometry is
read by running a script under KiCad's interpreter instead and reading back the
JSON it writes.

Scripts run with ``-I`` (isolated mode): the user's ``PYTHONPATH`` and
site-packages are ignored, and the script's folder is not put on ``sys.path``,
so nothing outside KiCad's install can shadow a module the script imports.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kicad_evaltor.utils.kicad_cli import (
    DEFAULT_CLI_TIMEOUT,
    SubprocessResult,
    _version_key,
    find_kicad_cli,
)

KICAD_PYTHON_ENV = "KICAD_PYTHON"
"""Environment variable naming the interpreter to use, overriding the search."""


def find_kicad_python(cli_path: str | Path | None = None) -> Path | None:
    """The interpreter that can import ``pcbnew``, or None if none is found.

    Looked for in order: ``$KICAD_PYTHON``; beside the given ``kicad-cli`` (on
    Windows and macOS KiCad installs both in one ``bin`` folder); then the
    standard install locations, newest KiCad first. On Linux, distributions put
    ``pcbnew`` into the system interpreter, so ``python3`` on ``PATH`` is the
    last resort.
    """
    override = os.environ.get(KICAD_PYTHON_ENV)
    if override:
        return Path(override) if Path(override).exists() else None

    candidates: list[Path] = []
    cli = Path(cli_path) if cli_path else find_kicad_cli()
    if cli is not None:
        candidates.extend(_beside_cli(cli))
    candidates.extend(_standard_locations())

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _beside_cli(cli: Path) -> list[Path]:
    folder = cli.resolve().parent
    return [folder / "python.exe", folder / "python3", folder / "python"]


def _standard_locations() -> list[Path]:
    if sys.platform == "win32":
        found: list[Path] = []
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base and (Path(base) / "KiCad").is_dir():
                found.extend((Path(base) / "KiCad").glob("*/bin/python.exe"))
        return sorted(found, key=lambda p: _version_key(p.parent.parent.name), reverse=True)
    if sys.platform == "darwin":
        return [
            Path(
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                "Python.framework/Versions/Current/bin/python3"
            )
        ]
    system = shutil.which("python3")
    return [Path("/usr/bin/python3"), *([Path(system)] if system else [])]


def run_kicad_python(
    python: Path,
    script: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_CLI_TIMEOUT,
) -> SubprocessResult:
    """Run ``script`` under KiCad's interpreter, in isolated mode."""
    try:
        result = subprocess.run(
            [str(python), "-I", str(script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SubprocessResult(
            returncode=-1,
            stdout="",
            stderr=f"KiCad's Python timed out after {timeout:g}s",
            timed_out=True,
        )
    except OSError as exc:
        return SubprocessResult(returncode=-1, stdout="", stderr=str(exc))
    return SubprocessResult(
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
    )
