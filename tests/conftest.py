"""Shared test doubles for the kipy (KiCad IPC) API surface that checks consume.

Checks only touch a narrow slice of the kipy objects (reference/lib_id/value on
symbols, net/pads/tracks/zones on boards), so plain fakes are enough to drive
every branch without a running KiCad instance.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.utils.kicad_cli import SubprocessResult


def _version_key(path: Path) -> tuple[int, ...]:
    """Order candidate installs newest-first (KiCad 10.0 beats 8.0)."""
    key: list[int] = []
    for part in path.parts:
        digits = "".join(c for c in part if c.isdigit())
        if digits:
            key.append(int(digits))
    return tuple(key)


def find_kicad_cli() -> Path | None:
    """Locate kicad-cli on PATH or in a standard KiCad installation.

    KiCad is not normally on PATH, so integration tests would otherwise skip
    even on a machine with KiCad installed.
    """
    for name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)

    candidates: list[Path] = []
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base and (Path(base) / "KiCad").is_dir():
                candidates.extend((Path(base) / "KiCad").glob("*/bin/kicad-cli.exe"))
    else:
        candidates = [
            Path("/usr/bin/kicad-cli"),
            Path("/usr/local/bin/kicad-cli"),
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
        ]

    for candidate in sorted(candidates, key=_version_key, reverse=True):
        if candidate.exists():
            return candidate
    return None


def kicad_cli_supports(subcommand: str, cli: Path) -> bool:
    """Ask kicad-cli whether a subcommand exists, without raising."""
    import subprocess

    try:
        result = subprocess.run(
            [str(cli), subcommand, "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and subcommand in result.stdout


# The sheet the suite's expectations are written against. A second demo sheet,
# visual_test.kicad_sch, lives in the same folder, so this one is picked by name
# rather than by whichever file happens to sort first.
DEMO_SCHEMATIC_STEM = "simple_circuit_test"


def demo_schematic_path() -> Path:
    """Locate the bundled demo schematic.

    The project is renamed from inside KiCad, which renames the ``.kicad_sch``
    with it, so the file is discovered rather than hardcoded.
    """
    demo_dir = Path(__file__).resolve().parent.parent / "examples" / "demo_circuit"
    candidates = sorted(
        path for path in demo_dir.glob("*.kicad_sch") if not path.name.startswith("~")
    )
    named = [path for path in candidates if path.stem == DEMO_SCHEMATIC_STEM]
    if named:
        return named[0]
    if not candidates:
        raise FileNotFoundError(f"no demo schematic in {demo_dir}")
    return candidates[0]


@pytest.fixture(scope="session")
def demo_schematic() -> Path:
    return demo_schematic_path()


@pytest.fixture(scope="session")
def kicad_cli() -> Path | None:
    return find_kicad_cli()


@pytest.fixture
def kicad_cli_required(kicad_cli: Path | None) -> Path:
    if kicad_cli is None:
        pytest.skip("kicad-cli not found on PATH or in a standard KiCad install")
    return kicad_cli


@pytest.fixture(scope="session")
def api_server_available(kicad_cli) -> bool:
    if kicad_cli is None:
        return False
    return kicad_cli_supports("api-server", kicad_cli)


def mm(value: float) -> int:
    """Lift a millimeter measurement into the nanometer units kipy reports."""
    return int(round(value * 1_000_000))


def mm2(value: float) -> int:
    return int(round(value * 1_000_000_000_000))


class Text:
    def __init__(self, value: str) -> None:
        self.value = value


class FootprintRef:
    def __init__(self, value: str) -> None:
        self.text = Text(value)


class SimpleNS:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeSymbol:
    def __init__(
        self,
        reference: str,
        lib_id: str = "Device:R",
        value: str = "R",
        footprint: str | None = None,
        user_fields: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
        sheet: str | None = None,
    ) -> None:
        self.reference = reference
        self.lib_id = lib_id
        self.value = value
        self.footprint = FootprintRef(footprint) if footprint is not None else None
        self.user_fields = user_fields or {}
        self.fields = fields or {}
        self.sheet = sheet


class BareSymbol:
    """A symbol exposing nothing beyond reference/lib_id/value."""

    def __init__(self, reference: str, lib_id: str = "Device:R", value: str = "R") -> None:
        self.reference = reference
        self.lib_id = lib_id
        self.value = value


class FakeNetNode:
    def __init__(
        self, ref: str, pin: str = "1", net: str = "", x: float = 0.0, y: float = 0.0
    ) -> None:
        self.ref = ref
        self.pin = pin
        self.net = net
        self.x = x
        self.y = y


class FakeNetlistNet:
    def __init__(self, name: str, nodes: list[FakeNetNode] | None = None) -> None:
        self.name = name
        self.nodes = nodes or []


class FakeNetlist:
    def __init__(self, nets: list[FakeNetlistNet] | None = None) -> None:
        self.nets = nets or []


class FakeSchematic:
    def __init__(
        self,
        symbols: list[Any] | None = None,
        nets: list[FakeNetlistNet] | None = None,
    ) -> None:
        self._symbols = symbols if symbols is not None else []
        self._netlist = FakeNetlist(nets)

    def get_symbols(self) -> list[Any]:
        return self._symbols

    def get_netlist(self) -> FakeNetlist:
        return self._netlist


class FakePad:
    def __init__(self, pad_number: str = "1", net: int | None = None) -> None:
        self.pad_number = pad_number
        self.net = net


class FakeFootprint:
    def __init__(
        self,
        reference: str,
        lib_id: str = "Resistor_SMD:R_0805",
        value: str = "",
        pads: list[FakePad] | None = None,
    ) -> None:
        self.reference = reference
        self.lib_id = lib_id
        self.value = value
        self.pads = pads or []


class FakeTrack:
    """A track exposing length through get_length(), like kipy's Track."""

    def __init__(self, net: int, width: int, length: int = 0) -> None:
        self.net = net
        self.width = width
        self._length = length

    def get_length(self) -> int:
        return self._length


class LengthAttrItem:
    """A net item exposing length as a plain attribute instead of a method."""

    def __init__(self, length: int) -> None:
        self.length = length


class LengthlessItem:
    """A net item with neither a length attribute nor a get_length method."""


class FakeNet:
    def __init__(self, name: str, net_code: int) -> None:
        self.name = name
        self.net_code = net_code


class FakeZone:
    def __init__(self, net_name: str, areas: list[int] | None = None, raises: bool = False) -> None:
        self.net_name = net_name
        self._areas = areas or []
        self._raises = raises

    def get_filled_polygons(self) -> list[Any]:
        if self._raises:
            raise RuntimeError("zone fill failed")
        return [SimpleNS(area=a) for a in self._areas]


class BBox:
    def __init__(self, min_x: int, min_y: int, max_x: int, max_y: int) -> None:
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y

    @classmethod
    def from_mm(cls, min_x: float, min_y: float, max_x: float, max_y: float) -> BBox:
        return cls(mm(min_x), mm(min_y), mm(max_x), mm(max_y))


class FakeBoard:
    def __init__(
        self,
        footprints: list[FakeFootprint] | None = None,
        tracks: list[Any] | None = None,
        nets: list[FakeNet] | None = None,
        zones: list[FakeZone] | None = None,
        items_by_net: dict[int, list[Any]] | None = None,
        bboxes: dict[str, BBox] | None = None,
    ) -> None:
        self._footprints = footprints or []
        self._tracks = tracks or []
        self._nets = nets or []
        self._zones = zones or []
        self._items_by_net = items_by_net or {}
        self._bboxes = bboxes or {}

    def get_footprints(self) -> list[FakeFootprint]:
        return self._footprints

    def get_tracks(self) -> list[Any]:
        return self._tracks

    def get_zones(self) -> list[FakeZone]:
        return self._zones

    def get_net_by_name(self, name: str) -> FakeNet | None:
        return next((n for n in self._nets if n.name == name), None)

    def get_footprint_by_reference(self, reference: str) -> FakeFootprint | None:
        return next((f for f in self._footprints if f.reference == reference), None)

    def get_items_by_net(self, net_code: int) -> list[Any]:
        return self._items_by_net.get(net_code, [])

    def get_item_bounding_box(self, footprint: FakeFootprint) -> BBox:
        return self._bboxes[footprint.reference]


def cli_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SubprocessResult:
    return SubprocessResult(returncode=returncode, stdout=stdout, stderr=stderr)


def cli_json(payload: dict[str, Any]) -> SubprocessResult:
    import json

    return cli_result(stdout=json.dumps(payload))


class FakeContext:
    """Stands in for DesignContext without touching kicad-cli or kipy."""

    def __init__(
        self,
        schematic: FakeSchematic | None = None,
        board: FakeBoard | None = None,
        cli_results: SubprocessResult | list[SubprocessResult] | None = None,
        schematic_path: str = "design.kicad_sch",
        board_path: str = "design.kicad_pcb",
    ) -> None:
        self._schematic = schematic
        self._board = board
        self._schematic_path = Path(schematic_path)
        self._board_path = Path(board_path)
        if cli_results is None:
            cli_results = cli_result(returncode=-1, stderr="kicad-cli not found")
        if isinstance(cli_results, SubprocessResult):
            cli_results = [cli_results]
        self._cli_results = list(cli_results)
        self.cli_calls: list[list[str]] = []

    def has_schematic(self) -> bool:
        return self._schematic is not None

    def has_board(self) -> bool:
        return self._board is not None

    @property
    def schematic(self) -> FakeSchematic:
        assert self._schematic is not None
        return self._schematic

    @property
    def board(self) -> FakeBoard:
        assert self._board is not None
        return self._board

    @property
    def schematic_path(self) -> Path:
        return self._schematic_path

    @property
    def board_path(self) -> Path:
        return self._board_path

    def run_kicad_cli(self, args: list[str]) -> SubprocessResult:
        self.cli_calls.append(args)
        result = self._next_cli_result()
        self._write_report_file(args, result)
        return result

    def _next_cli_result(self) -> SubprocessResult:
        if not self._cli_results:
            return cli_result(returncode=-1, stderr="no canned result")
        if len(self._cli_results) == 1:
            return self._cli_results[0]
        return self._cli_results.pop(0)

    @staticmethod
    def _write_report_file(args: list[str], result: SubprocessResult) -> None:
        """Mimic kicad-cli writing its JSON report to the -o path.

        Real kicad-cli prints only a human summary to stdout, so the checks read
        the file. Canned results keep carrying the JSON on stdout; move it.
        """
        if "-o" not in args or not result.success:
            return
        target = Path(args[args.index("-o") + 1])
        payload = result.stdout.strip()
        if not payload.startswith(("{", "[")):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")


def erc_report(*violations: dict[str, Any]) -> SubprocessResult:
    return cli_json({"erc_violation": list(violations)})


def drc_report(*violations: dict[str, Any]) -> SubprocessResult:
    return cli_json({"drc_violation": list(violations)})


def violation(
    severity: str = "error", message: str = "problem", kind: str = "unconnected"
) -> dict[str, Any]:
    return {"type": kind, "severity": severity, "message": message, "at": {"x": 1, "y": 2}}


@pytest.fixture(autouse=True)
def restore_check_registry():
    """Keep CheckRegistry mutations in one test from leaking into the next.

    test_core.py calls CheckRegistry.clear() wholesale, which would otherwise
    leave registration order deciding which tests pass.
    """
    saved = dict(CheckRegistry._registry)
    yield
    CheckRegistry._registry.clear()
    CheckRegistry._registry.update(saved)


@pytest.fixture
def schematic_ctx() -> FakeContext:
    return FakeContext(schematic=FakeSchematic())


@pytest.fixture
def board_ctx() -> FakeContext:
    return FakeContext(board=FakeBoard())
