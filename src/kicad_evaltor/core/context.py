from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kipy import KiCad
    from kipy.board import Board
    from kipy.project import Project
    from kipy.schematic import Schematic

from kicad_evaltor.hierarchy import SchematicTree, tree_for
from kicad_evaltor.schematic_file import FileSchematic, FileSymbol, load_schematic
from kicad_evaltor.utils.kicad_cli import SubprocessResult, run_kicad_cli


@dataclass
class KiCadInstance:
    process: subprocess.Popen | None = None
    port: int = 5051
    started: bool = False


class DesignContext:
    def __init__(
        self,
        project_path: str | Path | None = None,
        schematic_path: str | Path | None = None,
        board_path: str | Path | None = None,
        headless: bool = True,
        kicad_cli_path: str | None = None,
        timeout_ms: int = 5000,
    ) -> None:
        self._project_path = Path(project_path) if project_path else None
        self._schematic_path = Path(schematic_path) if schematic_path else None
        self._board_path = Path(board_path) if board_path else None
        self._headless = headless
        self._kicad_cli_path = kicad_cli_path
        self._timeout_ms = timeout_ms

        self._kicad_instance = KiCadInstance()
        self._kicad: KiCad | None = None
        self._project: Project | None = None
        # Schematic access is file-backed (FileSchematic); the kipy Schematic
        # type stays in the union because the IPC path can still supply one.
        self._schematic: FileSchematic | Schematic | None = None
        self._board: Board | None = None

    @property
    def kicad(self) -> KiCad:
        if self._kicad is None:
            self._kicad = self._connect_kicad()
        return self._kicad

    @property
    def project(self) -> Project:
        if self._project is None:
            if self._project_path:
                self._project = self.kicad.get_project(self._project_path)
            elif self._schematic_path:
                self._project = self.kicad.get_project(self._schematic_path)
            elif self._board_path:
                self._project = self.kicad.get_project(self._board_path)
            else:
                raise ValueError("No project, schematic, or board path provided")
        return self._project

    @property
    def schematic(self) -> Any:
        """Schematic data, preferring the file and falling back to IPC.

        Symbols, values, fields and footprints are plain s-expressions, so they
        are read from ``.kicad_sch`` directly. That keeps schematic checks
        working with no KiCad running, and only needs the IPC server for
        connectivity.
        """
        if self._schematic is None:
            if not self.has_schematic():
                raise ValueError("No schematic available")
            # The project folder goes with it: KiCad resolves a subsheet's
            # relative Sheetfile against the project, not against the file that
            # happens to contain the link.
            self._schematic = load_schematic(
                self.schematic_path,
                cli_path=self._kicad_cli_path,
                project_dir=self.project_dir,
            )
        return self._schematic

    def sheet_tree(self) -> SchematicTree | None:
        """Every sheet the schematic draws, or None when it is not file-backed.

        A KiCad schematic is a tree of files, and ``schematic`` is only its root.
        Anything that has to see the whole design -- a component check looking
        for a reference anywhere, a layout check that accepts a ``sheet``
        selector -- goes through here instead of reading one file.
        """
        schematic = self.schematic
        if not isinstance(schematic, FileSchematic):
            return None
        return tree_for(schematic)

    def sheet_symbols(self) -> list[FileSymbol]:
        """Every placed symbol in the design, each tagged with the sheet it sits on.

        This is the design-wide counterpart to ``schematic.get_symbols()``, which
        can only ever see the root file. A component-level question like "is
        there an R1?" has the same answer whichever sheet R1 was drawn on, so it
        should be asked here; geometry questions should not be, because two
        sheets' coordinates are two coordinate spaces.
        """
        tree = self.sheet_tree()
        if tree is not None:
            return [symbol for _, symbol in tree.iter_symbols()]
        # Not file-backed, so there is no tree to walk. The IPC schematic is flat
        # anyway, and its symbols are the whole design.
        schematic = self.schematic
        return list(schematic.get_symbols()) if schematic is not None else []

    @property
    def project_dir(self) -> Path | None:
        """The folder KiCad resolves a relative ``Sheetfile`` against.

        None when the caller only named a schematic file, in which case the file's
        own folder stands in and a link to a sibling sheet still resolves.
        """
        return self._project_path.parent if self._project_path else None

    @property
    def board(self) -> Board:
        if self._board is None:
            if not self.has_board():
                raise ValueError("No board available")
            self._board = self.kicad.get_board()
        return self._board

    @property
    def schematic_path(self) -> Path:
        if self._schematic_path:
            return self._schematic_path
        if self._project_path:
            return self._project_path.with_suffix(".kicad_sch")
        raise ValueError("No schematic path available")

    @property
    def board_path(self) -> Path:
        if self._board_path:
            return self._board_path
        if self._project_path:
            return self._project_path.with_suffix(".kicad_pcb")
        raise ValueError("No board path available")

    def has_schematic(self) -> bool:
        if self._schematic_path:
            return self._schematic_path.exists()
        if self._project_path:
            return self._project_path.with_suffix(".kicad_sch").exists()
        return False

    def has_board(self) -> bool:
        if self._board_path:
            return self._board_path.exists()
        if self._project_path:
            return self._project_path.with_suffix(".kicad_pcb").exists()
        return False

    def _connect_kicad(self) -> KiCad:
        from kipy import KiCad

        if self._headless:
            self._start_headless_server()

        return KiCad(port=self._kicad_instance.port, timeout=self._timeout_ms)

    def _start_headless_server(self) -> None:
        from kipy import KiCad

        if self._kicad_instance.started:
            return

        cli_path = self._kicad_cli_path
        if cli_path is None:
            import shutil

            cli_path = shutil.which("kicad-cli")
            if cli_path is None:
                import sys

                if sys.platform == "win32":
                    for base in [
                        r"C:\Program Files\KiCad\10.0\bin",
                        r"C:\Program Files\KiCad\9.0\bin",
                        r"C:\Program Files\KiCad\8.0\bin",
                    ]:
                        candidate = Path(base) / "kicad-cli.exe"
                        if candidate.exists():
                            cli_path = str(candidate)
                            break

        if cli_path is None:
            raise RuntimeError("kicad-cli not found. Install KiCad or provide kicad_cli_path.")

        file_path = None
        if self._project_path:
            file_path = str(self._project_path)
        elif self._schematic_path:
            file_path = str(self._schematic_path)
        elif self._board_path:
            file_path = str(self._board_path)

        cmd = [cli_path, "api-server", "--port", str(self._kicad_instance.port)]
        if file_path:
            cmd.extend(["--file", file_path])

        self._kicad_instance.process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._kicad_instance.started = True

        for _ in range(50):
            try:
                k = KiCad(port=self._kicad_instance.port, timeout=100)
                k.ping()
                return
            except Exception:
                time.sleep(0.1)

        raise RuntimeError("Failed to start kicad-cli api-server")

    def run_kicad_cli(self, args: list[str]) -> SubprocessResult:
        cli_path = self._kicad_cli_path
        if cli_path is None:
            import shutil

            cli_path = shutil.which("kicad-cli")
        return (
            run_kicad_cli([cli_path, *args])
            if cli_path
            else SubprocessResult(
                returncode=-1,
                stdout="",
                stderr="kicad-cli not found",
            )
        )

    def close(self) -> None:
        if self._kicad is not None:
            try:
                self._kicad.close()
            except Exception:
                pass
            self._kicad = None

        if self._kicad_instance.process is not None:
            self._kicad_instance.process.terminate()
            try:
                self._kicad_instance.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kicad_instance.process.kill()
            self._kicad_instance.process = None
            self._kicad_instance.started = False

    def __enter__(self) -> DesignContext:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
