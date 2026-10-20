from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from kicad_evaltor.board_file import FileBoard
    from kicad_evaltor.pcbnew_bridge import BoardGeometry

from kicad_evaltor.hierarchy import SchematicTree, tree_for
from kicad_evaltor.schematic_file import FileSchematic, FileSymbol, load_schematic
from kicad_evaltor.utils.kicad_cli import (
    DEFAULT_CLI_TIMEOUT,
    REPORT_CWD,
    SubprocessResult,
    find_kicad_cli,
    run_kicad_cli,
)

T = TypeVar("T")


class DesignContext:
    """One design under test: its files, and the KiCad tools that read them.

    Everything is file-backed. The schematic and board are parsed directly;
    ``kicad-cli`` runs netlist export, ERC, DRC and fabrication exports; and
    board geometry comes from ``pcbnew`` under KiCad's bundled Python. KiCad 10
    has no IPC API server, so nothing here needs a running KiCad.

    ``headless`` and ``timeout_ms`` belonged to the removed IPC connection. They
    are still accepted, and ignored, so existing callers keep working.
    """

    def __init__(
        self,
        project_path: str | Path | None = None,
        schematic_path: str | Path | None = None,
        board_path: str | Path | None = None,
        headless: bool = True,
        kicad_cli_path: str | None = None,
        timeout_ms: int = 5000,
        cli_timeout: float = DEFAULT_CLI_TIMEOUT,
        project_name: str | None = None,
    ) -> None:
        if cli_timeout <= 0:
            raise ValueError("cli_timeout must be positive")
        self._project_path = Path(project_path) if project_path else None
        self._schematic_path = Path(schematic_path) if schematic_path else None
        self._board_path = Path(board_path) if board_path else None
        self._kicad_cli_path = kicad_cli_path
        self._cli_timeout = cli_timeout
        self._project_name = project_name or (
            self._project_path.stem if self._project_path else None
        )

        self._schematic: FileSchematic | None = None
        self._board: FileBoard | None = None
        self._memo: dict[Any, Any] = {}

    @property
    def schematic(self) -> FileSchematic:
        """The root schematic, read from ``.kicad_sch``.

        Symbols, values, fields and footprints are plain s-expressions, so they
        are read directly; only the netlist needs ``kicad-cli``.
        """
        if self._schematic is None:
            if not self.has_schematic():
                raise ValueError("No schematic available")
            # The project folder goes with it: KiCad resolves a subsheet's
            # relative Sheetfile against the project, not against the file that
            # happens to contain the link.
            self._schematic = load_schematic(
                self.schematic_path,
                cli_path=self.kicad_cli_path,
                project_dir=self.project_dir,
                cli_timeout=self._cli_timeout,
                project_name=self._project_name,
            )
        return self._schematic

    def sheet_tree(self) -> SchematicTree | None:
        """Every sheet the schematic draws.

        A KiCad schematic is a tree of files, and ``schematic`` is only its root.
        Anything that has to see the whole design -- a component check looking
        for a reference anywhere, a layout check that accepts a ``sheet``
        selector -- goes through here instead of reading one file.
        """
        return tree_for(self.schematic)

    def sheet_symbols(self) -> list[FileSymbol]:
        """Every placed symbol in the design, each tagged with the sheet it sits on.

        This is the design-wide counterpart to ``schematic.get_symbols()``, which
        can only ever see the root file. A component-level question like "is
        there an R1?" has the same answer whichever sheet R1 was drawn on, so it
        should be asked here; geometry questions should not be, because two
        sheets' coordinates are two coordinate spaces.
        """
        tree = self.sheet_tree()
        return [symbol for _, symbol in tree.iter_symbols()] if tree is not None else []

    @property
    def project_dir(self) -> Path | None:
        """The folder KiCad resolves a relative ``Sheetfile`` against.

        None when the caller only named a schematic file, in which case the file's
        own folder stands in and a link to a sibling sheet still resolves.
        """
        return self._project_path.parent if self._project_path else None

    @property
    def board(self) -> FileBoard:
        """The board, read from ``.kicad_pcb`` with its project's net classes."""
        if self._board is None:
            if not self.has_board():
                raise ValueError("No board available")
            from kicad_evaltor.board_file import load_board

            project = self._project_path or self.board_path.with_suffix(".kicad_pro")
            self._board = load_board(self.board_path, project_path=project)
        return self._board

    def board_geometry(self, *, fill_zones: bool = True) -> BoardGeometry:
        """Courtyards, pads, outline, zone fill and connectivity from pcbnew.

        Computed once per context, by one run of KiCad's bundled Python. Raises
        :class:`~kicad_evaltor.pcbnew_bridge.GeometryUnavailable` when KiCad's
        Python cannot be found or cannot read the board.
        """
        from kicad_evaltor.pcbnew_bridge import GeometryUnavailable, dump_board

        def compute() -> BoardGeometry | GeometryUnavailable:
            try:
                return dump_board(
                    self.board_path,
                    cli_path=self.kicad_cli_path,
                    fill_zones=fill_zones,
                    timeout=self._cli_timeout,
                )
            except GeometryUnavailable as exc:
                # Kept: a missing or hanging pcbnew stays so for the run, and a
                # dozen geometry checks should not each wait out the timeout.
                return exc

        found = self.memo(("board_geometry", fill_zones), compute)
        if isinstance(found, GeometryUnavailable):
            raise found
        return found

    def memo(self, key: Any, compute: Callable[[], T]) -> T:
        """``compute()`` once per ``key`` for the life of this context.

        Several checks need the same expensive answer (a DRC report, the board
        geometry); this keeps it to one kicad-cli or pcbnew run. A raised error
        is not cached, so the next caller tries again; a caller that wants a
        failure retried when it is returned rather than raised calls ``forget``.
        """
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]  # type: ignore[no-any-return]

    def index(self):
        """Identity queries without running any checks or choosing thresholds."""
        from kicad_evaltor.design_index import DesignIndex

        return self.memo("design_index", lambda: DesignIndex(self))

    def toolchain(self, *, include_pcbnew: bool = False):
        from kicad_evaltor.toolchain import probe_toolchain

        return self.memo(
            ("toolchain", include_pcbnew),
            lambda: probe_toolchain(
                self.kicad_cli_path, include_pcbnew=include_pcbnew, timeout=self.cli_timeout
            ),
        )

    def source_identities(self) -> list[dict[str, str]]:
        """Current on-disk hashes, explicitly requested and never cached."""
        from kicad_evaltor.toolchain import source_identity

        paths: set[Path] = set()
        if self.has_schematic():
            tree = self.sheet_tree()
            if tree is not None:
                paths.update(s.resolved for s in tree.sheets() if s.resolved is not None)
        if self.has_board():
            paths.add(self.board_path)
        if self._project_path and self._project_path.is_file():
            paths.add(self._project_path)
        return [source_identity(p) for p in sorted(paths)]

    def forget(self, key: Any) -> None:
        """Drop one remembered answer, so the next ``memo`` computes it again."""
        self._memo.pop(key, None)

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

    @property
    def kicad_cli_path(self) -> str | None:
        """The kicad-cli to use: the one given, else one found on ``PATH`` or in
        KiCad's standard install folders, else None."""
        if self._kicad_cli_path is None:
            found = find_kicad_cli()
            self._kicad_cli_path = str(found) if found else None
        return self._kicad_cli_path

    @property
    def cli_timeout(self) -> float:
        """Seconds a kicad-cli call may run unless the caller asks otherwise."""
        return self._cli_timeout

    def run_kicad_cli(self, args: list[str], *, timeout: float | None = None) -> SubprocessResult:
        """Run kicad-cli, allowing ``timeout`` seconds or the context's default."""
        cli_path = self.kicad_cli_path
        return (
            run_kicad_cli(
                [cli_path, *args], timeout=timeout or self._cli_timeout, cwd=REPORT_CWD.get()
            )
            if cli_path
            else SubprocessResult(
                returncode=-1,
                stdout="",
                stderr="kicad-cli not found",
            )
        )

    def invalidate(self) -> None:
        """Reload all files and tool results on next access after a design edit."""
        self._memo.clear()
        self._schematic = None
        self._board = None

    def close(self) -> None:
        """Drop all cached design data. Nothing else is held open."""
        self.invalidate()

    def __enter__(self) -> DesignContext:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
