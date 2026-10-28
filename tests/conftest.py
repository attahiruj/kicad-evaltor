"""Shared test doubles and fixtures.

Schematic checks only touch a narrow slice of a symbol (reference, lib_id,
value, fields), so plain fakes drive every branch. Board checks are tested
against the real example board instead: its file, and the pcbnew geometry and
DRC reports recorded from it under ``tests/fixtures/pcb``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from kicad_evaltor.checks.registry import CheckRegistry
from kicad_evaltor.netlist import FileNet, FileNetComponent, FileNetlist, FileNetNode
from kicad_evaltor.utils.kicad_cli import SubprocessResult

# The library starts KiCad without this variable (``kicad_child_env``): set to
# ``nvidia`` it makes kicad-cli 10.0.7 segfault. Tests that call kicad-cli or
# KiCad's Python directly inherit this process's environment, so drop it here
# too; the tests of ``kicad_child_env`` set it back explicitly.
os.environ.pop("__GLX_VENDOR_LIBRARY_NAME", None)


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
# visual.kicad_sch, lives in the same folder, so this one is picked by name
# rather than by whichever file happens to sort first.
ESP_BOARD = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "demo_circuit"
    / "esp_data_logger_board"
    / "esp_data_logger_board.kicad_pcb"
)
"""A real KiCad 10 board (ESP32-C6 data logger), DRC-clean, with its schematic."""

PCB_RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "pcb"
"""KiCad's DRC reports and pcbnew geometry for that board and broken copies of
it, recorded by ``tools/record_pcb_fixtures.py``."""


def recorded_drc(variant: str = "base") -> SubprocessResult:
    """A canned kicad-cli result carrying the DRC report recorded for ``variant``."""
    return cli_result(stdout=(PCB_RECORDINGS / f"{variant}.drc.json").read_text("utf-8"))


def recorded_geometry(variant: str = "base") -> Any:
    from kicad_evaltor.pcbnew_bridge import geometry_from_dict

    return geometry_from_dict(
        json.loads((PCB_RECORDINGS / f"{variant}.geometry.json").read_text("utf-8"))
    )


DEMO_SCHEMATIC_STEM = "simple_circuit"


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


def hierarchy_schematic_path() -> Path:
    """The root of the bundled hierarchical demo.

    ``hierarchy.kicad_sch`` draws the controls and holds one sheet, ``shared``,
    linking to ``circuit.kicad_sch`` with the power stage and MCU. The flat demo
    covers the design checks; this one is what the sheet tree is tested against.
    """
    path = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "demo_circuit"
        / "hierarchy"
        / "hierarchy.kicad_sch"
    )
    if not path.exists():
        raise FileNotFoundError(f"no hierarchical demo at {path}")
    return path


@pytest.fixture(scope="session")
def hierarchy_schematic() -> Path:
    return hierarchy_schematic_path()


@pytest.fixture(scope="session")
def kicad_cli() -> Path | None:
    return find_kicad_cli()


@pytest.fixture
def kicad_cli_required(kicad_cli: Path | None) -> Path:
    if kicad_cli is None:
        pytest.skip("kicad-cli not found on PATH or in a standard KiCad install")
    return kicad_cli


# ---------------------------------------------------------------- sheet trees
#
# Hierarchical tests need designs with shapes nobody would draw on purpose: a
# link that points nowhere, a directory where a file should be, a sheet that links
# back to its own ancestor, one file instantiated twice. They are built here
# rather than in each test so no test has to hand-write an s-expression, and so a
# change to the file format is fixed in one place.


def _uuid(tag: str) -> str:
    """A stable uuid for a tag, so a tree's shape is readable in a failure."""
    digits = f"{abs(hash(tag)) % (16**12):012x}"
    return f"{tag[:8]}-{digits[:4]}-{digits[4:8]}-{digits[8:12]}-{digits[:12]}"


def sheet_symbol(
    name: str,
    filename: str,
    *,
    uuid: str,
    pins: Sequence[str] = (),
    page: str | None = None,
    project: str = "proj",
    parent_uuid: str | None = None,
) -> str:
    """One ``(sheet ...)`` block linking to a child file."""
    at_x, at_y, width, height = 50.8, 25.4, 38.1, 25.4
    pin_text = "".join(
        f'\t\t(pin "{pin}" input\n'
        f"\t\t\t(at {at_x + 8 + 16 * i:g} {at_y + height:g} 270)\n"
        f'\t\t\t(uuid "{_uuid(f"{name}-pin-{pin}")}")\n'
        "\t\t\t(effects\n\t\t\t\t(font (size 1.27 1.27))\n\t\t\t\t(justify right)\n\t\t\t)\n\t\t)\n"
        for i, pin in enumerate(pins)
    )
    page_text = f'\n\t\t\t\t\t(page "{page}")' if page else ""
    instance = (
        f'\t\t(instances\n\t\t\t(project "{project}"\n'
        f'\t\t\t\t(path "{"/" if parent_uuid is None else "/" + parent_uuid}'
        f'/{uuid}"{page_text}\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
        if project
        else ""
    )
    return (
        "\t(sheet\n"
        f"\t\t(at {at_x:g} {at_y:g})\n\t\t(size {width:g} {height:g})\n"
        "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n\t\t(dnp no)\n"
        "\t\t(fields_autoplaced yes)\n"
        "\t\t(stroke\n\t\t\t(width 0.1524)\n\t\t\t(type solid)\n\t\t)\n"
        "\t\t(fill\n\t\t\t(color 0 0 0 0.0000)\n\t\t)\n"
        f'\t\t(uuid "{uuid}")\n'
        f'\t\t(property "Sheetname" "{name}"\n\t\t\t(at {at_x + width / 2:g} {at_y + 2.54:g} 0)\n'
        "\t\t\t(effects\n\t\t\t\t(font (size 1.27 1.27))\n\t\t\t\t(justify bottom)\n\t\t\t)\n\t\t)\n"
        f'\t\t(property "Sheetfile" "{filename}"\n\t\t\t(at {at_x + width / 2:g} {at_y + height - 2.54:g} 0)\n'
        "\t\t\t(hide yes)\n"
        "\t\t\t(effects\n\t\t\t\t(font (size 1.27 1.27))\n\t\t\t\t(justify top)\n\t\t\t)\n\t\t)\n"
        f"{pin_text}{instance}\t)\n"
    )


def hierarchical_label(name: str, x: float = 76.2, y: float = 50.8) -> str:
    """One ``(hierarchical_label ...)`` inside a child file."""
    return (
        f'\t(hierarchical_label "{name}"\n\t\t(shape input)\n\t\t(at {x:g} {y:g} 0)\n'
        "\t\t(effects\n\t\t\t(font (size 1.27 1.27))\n\t\t\t(justify)\n\t\t)\n"
        f'\t\t(uuid "{_uuid(f"label-{name}")}")\n\t)\n'
    )


def schematic_file(
    path: Path,
    *,
    uuid: str,
    body: str = "",
    paper: str = "A4",
    sheet_instances: bool = True,
) -> Path:
    """Write a minimal, valid ``.kicad_sch`` and return its path.

    The document is genuinely empty apart from ``body``, so a test can say exactly
    which nodes it is about and nothing else perturbs the result.
    """
    pages = (
        '\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n'
        if sheet_instances
        else ""
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'(kicad_sch\n\t(version 20250114)\n\t(generator "evaltor")\n\t(generator_version "1")\n'
        f'\t(uuid "{uuid}")\n\t(paper "{paper}")\n\t(lib_symbols)\n{body}{pages}'
        "\t(embedded_fonts no)\n)\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hierarchical_design(tmp_path):
    """Build sheet trees on disk, one call per shape.

    Returns a callable taking the child files to write and returning the root path,
    so a test reads as the tree it means. Each key names both the child file and
    the sheet symbol pointing at it. A value is the child's body; ``None`` means a
    directory sitting where a file should be, and a dict of ``body`` / ``pins``
    overrides the shape of the link:

        hierarchical_design({"main": ""})
        hierarchical_design({"plain": {"body": "", "pins": ()}})
        hierarchical_design({"missing": None})

    Pairs are given distinct page numbers, because a page number shared by two
    sheets is a defect under test rather than a thing every test should trip.
    """
    created: list[Path] = []

    def build(
        children: dict[str, object], *, root_body: str | None = None, root_uuid: str | None = None
    ):
        root = tmp_path / "root.kicad_sch"
        created.append(root)
        for name, target in children.items():
            if isinstance(target, Path):
                continue
            if target is None:
                (tmp_path / f"{name}.kicad_sch").mkdir(parents=True, exist_ok=True)
                continue
            shape = target if isinstance(target, dict) else {"body": target}
            schematic_file(
                tmp_path / f"{name}.kicad_sch",
                uuid=_uuid(name),
                body=str(shape.get("body", "")),
            )
        root_text = root_body
        if root_text is None:
            root_text = "".join(
                sheet_symbol(
                    name,
                    f"{name}.kicad_sch",
                    uuid=_uuid(f"sheet-{name}"),
                    pins=_pins(children[name]),
                    page=str(index + 2),
                )
                for index, name in enumerate(children)
            )
        return schematic_file(root, uuid=root_uuid or _uuid("root"), body=root_text)

    return build


def _pins(target: object) -> tuple[str, ...]:
    """The pins a generated sheet symbol carries, defaulting to a power pair."""
    if isinstance(target, dict):
        return tuple(target.get("pins", ("+3.3V", "GND")))
    return ("+3.3V", "GND")


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
        netlist: Any = None,
    ) -> None:
        self._symbols = symbols if symbols is not None else []
        # A real FileNetlist when the check under test needs its lookups.
        self._netlist = netlist if netlist is not None else FakeNetlist(nets)
        self.netlist_formats: list[str] = []

    def get_symbols(self) -> list[Any]:
        return self._symbols

    def get_netlist(self, format: str = "kicadxml") -> Any:
        self.netlist_formats.append(format)
        return self._netlist


class FakeFootprint:
    """Just enough of a board footprint for the schematic/board consistency check."""

    def __init__(self, reference: str, lib_id: str = "Resistor_SMD:R_0805") -> None:
        self.reference = reference
        self.lib_id = lib_id


class FakeBoard:
    """A board that is only a list of footprints. Board checks use the real
    ``FileBoard`` read from the example board instead."""

    def __init__(self, footprints: list[FakeFootprint] | None = None) -> None:
        self.footprints = footprints or []

    def get_footprints(self) -> list[FakeFootprint]:
        return list(self.footprints)


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
        board: Any = None,
        geometry: Any = None,
        cli_results: SubprocessResult | list[SubprocessResult] | None = None,
        schematic_path: str = "design.kicad_sch",
        board_path: str = "design.kicad_pcb",
    ) -> None:
        self._schematic = schematic
        self._board = board
        self._geometry = geometry
        self._memo: dict[Any, Any] = {}
        self._schematic_path = Path(schematic_path)
        self._board_path = Path(board_path)
        if cli_results is None:
            cli_results = cli_result(returncode=-1, stderr="kicad-cli not found")
        if isinstance(cli_results, SubprocessResult):
            cli_results = [cli_results]
        self._cli_results = list(cli_results)
        self.cli_calls: list[list[str]] = []
        self.cli_timeouts: list[float | None] = []

    def has_schematic(self) -> bool:
        return self._schematic is not None

    def has_board(self) -> bool:
        return self._board is not None

    @property
    def schematic(self) -> FakeSchematic:
        assert self._schematic is not None
        return self._schematic

    def sheet_symbols(self) -> list[object]:
        """The design's symbols, flattened across sheets.

        A fake schematic stands for a single flat sheet, so the whole design is
        one file's symbols and the tree traversal has nothing to add.
        """
        return self.schematic.get_symbols()

    @property
    def board(self) -> Any:
        assert self._board is not None
        return self._board

    def board_geometry(self, *, fill_zones: bool = True) -> Any:
        """Recorded pcbnew geometry, or GeometryUnavailable as with no KiCad."""
        if self._geometry is None:
            from kicad_evaltor.pcbnew_bridge import GeometryUnavailable

            raise GeometryUnavailable("KiCad's Python (with pcbnew) was not found")
        return self._geometry

    def memo(self, key: Any, compute: Any) -> Any:
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def forget(self, key: Any) -> None:
        self._memo.pop(key, None)

    @property
    def schematic_path(self) -> Path:
        return self._schematic_path

    @property
    def board_path(self) -> Path:
        return self._board_path

    def run_kicad_cli(self, args: list[str], *, timeout: float | None = None) -> SubprocessResult:
        self.cli_calls.append(args)
        self.cli_timeouts.append(timeout)
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


def erc_report(*violations: dict[str, Any], sheet: str = "/") -> SubprocessResult:
    """A canned ERC report in the shape KiCad 10 writes.

    Per sheet, under ``sheets[*].violations``: there is no top-level
    ``violations`` key in a real report, so a fixture that writes one would test
    a format KiCad never produces.
    """
    return cli_json(
        {
            "$schema": "https://schemas.kicad.org/erc.v1.json",
            "kicad_version": "10.0.1",
            "sheets": [{"path": sheet, "uuid_path": sheet, "violations": list(violations)}],
        }
    )


def drc_report(*violations: dict[str, Any]) -> SubprocessResult:
    """A canned DRC report, which unlike ERC stays one flat top-level list."""
    return cli_json({"violations": list(violations)})


def violation(
    severity: str = "error", description: str = "problem", kind: str = "unconnected"
) -> dict[str, Any]:
    """One violation as KiCad writes it: a description plus per-item positions.

    There is no ``message`` and no ``at``; a fixture using those names would let
    a report that reads the wrong keys pass unnoticed.
    """
    return {
        "type": kind,
        "severity": severity,
        "description": description,
        "items": [{"description": "a thing", "pos": {"x": 1.0, "y": 2.0}, "uuid": "u-1"}],
    }


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


# ------------------------------------------------------------------- netlists


def netlist_of(
    *nets: tuple[str, list[tuple[str, str] | tuple[str, str, str]]],
    components: dict[str, str | FileNetComponent] | None = None,
) -> FileNetlist:
    """A real netlist from ``(net, [(ref, pin[, pin_name]), ...])`` tuples.

    ``components`` maps a reference to its ``lib_id``, or to a whole
    ``FileNetComponent`` when the test needs a value or description; any
    reference on a net and not listed gets an empty one. The connectivity code
    reads real ``FileNetlist`` lookups, so these tests build the real thing.
    """
    built = [
        FileNet(
            name,
            [FileNetNode(n[0], n[1], name, pinfunction=n[2] if len(n) > 2 else "") for n in nodes],
        )
        for name, nodes in nets
    ]
    comps = {node.ref: FileNetComponent(node.ref) for net in built for node in net.nodes}
    for ref, spec in (components or {}).items():
        comps[ref] = (
            spec if isinstance(spec, FileNetComponent) else FileNetComponent(ref, lib_id=spec)
        )
    return FileNetlist(built, comps)
