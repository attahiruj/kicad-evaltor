"""Explicit toolchain probes and immutable source identities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from kicad_evaltor.utils.kicad_cli import run_kicad_cli
from kicad_evaltor.utils.kicad_python import find_kicad_python


@dataclass(frozen=True)
class ToolchainInfo:
    evaltor_version: str
    cli_path: str | None
    cli_version: str | None
    python_path: str | None = None
    pcbnew_version: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def versions_match(self) -> bool | None:
        if self.cli_version is None or self.pcbnew_version is None:
            return None

        def version(s: str) -> tuple[str, ...]:
            return tuple(re.findall(r"\d+", s)[:3])

        return version(self.cli_version) == version(self.pcbnew_version)


def probe_toolchain(
    cli_path: str | None, *, include_pcbnew: bool = False, timeout: float
) -> ToolchainInfo:
    """Runs only when requested; does not enforce a compatibility or design policy."""
    try:
        version = metadata.version("kicad-evaltor")
    except metadata.PackageNotFoundError:
        version = "uninstalled"
    errors = []
    cli_version = None
    if cli_path:
        result = run_kicad_cli([cli_path, "version"], timeout=timeout)
        if result.success and result.stdout.strip():
            cli_version = result.stdout.strip()
        else:
            errors.append(result.stderr or "No CLI version returned")
    else:
        errors.append("kicad-cli unavailable")
    python_path = find_kicad_python(cli_path) if include_pcbnew else None
    pcb_version = None
    if include_pcbnew:
        if python_path is None:
            errors.append("KiCad Python unavailable")
        else:
            result = run_kicad_cli(
                [str(python_path), "-I", "-c", "import pcbnew; print(pcbnew.Version())"],
                timeout=timeout,
            )
            if result.success and result.stdout.strip():
                pcb_version = result.stdout.strip()
            else:
                errors.append(result.stderr or "No pcbnew version returned")
    return ToolchainInfo(
        version,
        cli_path,
        cli_version,
        str(python_path) if python_path else None,
        pcb_version,
        tuple(errors),
    )


def source_identity(path: str | Path) -> dict[str, str]:
    source = Path(path).resolve()
    content = source.read_bytes()
    return {"path": str(source), "sha256": hashlib.sha256(content).hexdigest()}
