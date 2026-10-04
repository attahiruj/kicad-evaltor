"""The sheet tree a ``.kicad_sch`` design is made of.

A KiCad project is one root schematic plus a tree of child files, one per sheet.
:mod:`kicad_evaltor.schematic_file` reads exactly one file; this walks from one
root file down through every ``(sheet ...)`` block it can reach and hands back
the child :class:`~kicad_evaltor.schematic_file.FileSchematic` and
:class:`~kicad_evaltor.schematic_items.SchematicScene` for each sheet.

Two rules shape everything here.

**A failure is a finding, never an exception.** A link that does not resolve, a
file that does not parse, a sheet that links back to one of its own ancestors:
each leaves that subtree unloaded and adds an entry to :meth:`problems`, and
every other part of the design keeps working. That is what lets
``sch.sheet.file_missing`` report a broken link instead of the whole run dying
on it.

**A sheet is its own coordinate space.** Two sheets' items can never be compared
to each other, so each gets its own scene and layout checks run over them one at
a time before their findings are aggregated.

Scenes are memoised: seven layout checks over a four-sheet design would
otherwise re-extract the same geometry seven times. A cached
:class:`~kicad_evaltor.schematic_items.SchematicScene` is therefore *shared* and
must only be read. That is safe because the collision engine
(:mod:`kicad_evaltor.collisions`) sorts a copy and builds new ``Collision``
objects, and the checks build new ``CollisionItem``s rather than editing theirs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from kicad_evaltor.schematic_file import FileSchematic, FileSymbol
from kicad_evaltor.schematic_items import SchematicScene, extract
from kicad_evaltor.sheets import ROOT_PATH, Sheet

# Problems a walk can hit, as the kind reported by ``sch.sheet.*`` checks.
MISSING = "missing"
UNREADABLE = "unreadable"
CYCLE = "cycle"


@dataclass(frozen=True)
class SheetProblem:
    """One thing that stopped part of the tree from being read.

    ``chain`` names the sheets from the repeated one back to itself, which is
    the only way to describe a cycle usefully; it is empty for the other kinds.
    """

    kind: str
    sheet: Sheet
    detail: str = ""
    chain: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The readable name of the sheet the problem is attached to."""
        return self.sheet.name


class SchematicTree:
    """Every sheet reachable from one root file, in depth-first order.

    ``sheets()`` is pre-order: a sheet comes before its children, and children
    come in the order they appear on the parent. That is the order findings are
    aggregated in, so a report reads top-down through the design.
    """

    def __init__(self, root: FileSchematic) -> None:
        self._root = root
        self._root_sheet = _root_sheet(root)
        self._schematics: dict[str, FileSchematic] = {ROOT_PATH: root}
        self._children: dict[str, list[Sheet]] = {}
        self._scenes: dict[str, SchematicScene] = {}
        self._order: list[Sheet] = []
        self._problems: list[SheetProblem] = []
        self._walk()

    @classmethod
    def load(
        cls,
        root: Path,
        *,
        project_dir: Path | None = None,
        cli_path: str | None = None,
    ) -> SchematicTree:
        """Read the root file and walk down from it.

        A root that does not parse raises, because there is no design to check
        anything against. Everything below it does not.
        """
        return cls(FileSchematic(root, cli_path=cli_path, project_dir=project_dir))

    @property
    def root(self) -> FileSchematic:
        """The root file, which is also a sheet in its own right."""
        return self._root

    @property
    def root_sheet(self) -> Sheet:
        """The root file as a :class:`Sheet`, which no ``(sheet ...)`` node describes.

        Its name is the file's stem and its path is ``"/"``, matching how KiCad
        identifies the root instance.
        """
        return self._root_sheet

    def sheets(self) -> list[Sheet]:
        """Every sheet, root first, then each subtree depth-first."""
        return list(self._order)

    def children(self, sheet: Sheet) -> list[Sheet]:
        """The sheets drawn directly on ``sheet``."""
        return list(self._children.get(sheet.path, ()))

    def schematic_for(self, sheet: Sheet) -> FileSchematic | None:
        """The file drawing ``sheet``, or None when it could not be read."""
        return self._schematics.get(sheet.path)

    def scene_for(self, sheet: Sheet) -> SchematicScene | None:
        """``sheet``'s geometry, extracted once and shared from then on."""
        if sheet.path in self._scenes:
            return self._scenes[sheet.path]
        schematic = self.schematic_for(sheet)
        if schematic is None:
            return None
        scene = extract(schematic)
        self._scenes[sheet.path] = scene
        return scene

    def find(self, name_or_path: str) -> Sheet | None:
        """Look a sheet up by name, then instance path, then linked-file stem.

        The three are tried in that order because that is how a reader is most
        likely to know the sheet: by what it is called. Instance paths are the
        unambiguous fallback when one file is instantiated twice, and the stem
        is what someone reading a ``Sheetfile`` off disk would type.
        """
        for sheet in self._order:
            if sheet.name == name_or_path:
                return sheet
        for sheet in self._order:
            if sheet.path == name_or_path:
                return sheet
        for sheet in self._order:
            if sheet.stem == name_or_path:
                return sheet
            if sheet.resolved is not None and sheet.resolved.stem == name_or_path:
                return sheet
        return None

    def iter_symbols(self) -> Iterator[tuple[Sheet, FileSymbol]]:
        """Every symbol in the design, with the sheet it belongs to.

        Each symbol is stamped with that sheet's readable name and uuid path.
        Root-sheet symbols keep ``sheet is None``, which is what the component
        models already expect from a flat schematic.
        """
        for sheet in self._order:
            schematic = self.schematic_for(sheet)
            if schematic is None:
                continue
            name = None if sheet.path == ROOT_PATH else sheet.name
            for symbol in schematic.get_symbols():
                symbol.sheet = name
                symbol.sheet_path = sheet.path
                yield sheet, symbol

    def problems(self) -> list[SheetProblem]:
        """Everything that stopped part of the design from being read."""
        return list(self._problems)

    def _walk(self) -> None:
        """Depth-first, refusing to revisit a file that is already on the path.

        Refusing is keyed on the *stack*, not on every file seen: KiCad
        legitimately instantiates one file under several sheet symbols, and only
        a path that reaches one of its own ancestors is a cycle.

        Children are pushed in reverse so they pop in the order they are drawn,
        and their problems are recorded on the way past in that same order. Both
        lists then read top-down through the design, the way a reader opens it.
        """
        stack: list[tuple[Sheet, tuple[Sheet, ...]]] = [(self._root_sheet, (self._root_sheet,))]
        while stack:
            sheet, chain = stack.pop()
            self._order.append(sheet)
            schematic = self.schematic_for(sheet)
            kids = schematic.sheets() if schematic is not None else []
            self._children[sheet.path] = kids
            reachable = []
            for child in kids:
                problem = self._unreachable(child, chain)
                if problem is not None:
                    self._problems.append(problem)
                elif self._load_child(child, chain) is not None:
                    reachable.append(child)
            for child in reversed(reachable):
                stack.append((child, (*chain, child)))

    def _unreachable(self, child: Sheet, chain: tuple[Sheet, ...]) -> SheetProblem | None:
        if child.resolved is None:
            reason = "no Sheetfile" if not child.filename else "no candidate exists"
            return SheetProblem(MISSING, child, f"{child.filename!r}: {reason}")
        if any(sheet.resolved == child.resolved for sheet in chain):
            return SheetProblem(CYCLE, child, chain=_cycle_names(child, chain))
        return None

    def _load_child(self, child: Sheet, chain: tuple[Sheet, ...]) -> FileSchematic | None:
        assert child.resolved is not None  # guarded by _unreachable
        try:
            schematic = FileSchematic(
                child.resolved,
                cli_path=self._root.cli_path(),
                project_dir=self._root.project_dir(),
                instance_path=child.path,
            )
        except (OSError, ValueError) as exc:
            self._problems.append(SheetProblem(UNREADABLE, child, str(exc)))
            return None
        self._schematics[child.path] = schematic
        return schematic


_TREES: WeakKeyDictionary[FileSchematic, SchematicTree] = WeakKeyDictionary()


def tree_for(schematic: FileSchematic) -> SchematicTree:
    """The sheet tree rooted at ``schematic``, built once per parsed file.

    Checks each run against the same memoised ``FileSchematic``, and a tree
    memoises its scenes, so without this seven layout checks over one design
    would parse and re-extract the same files seven times. The table holds its
    keys weakly, so a discarded context takes its tree with it.
    """
    tree = _TREES.get(schematic)
    if tree is None:
        # Built from the schematic rather than from its path, so the root file is
        # parsed once: it carries its own project folder and cli path already.
        tree = SchematicTree(schematic)
        _TREES[schematic] = tree
    return tree


def select_sheets(tree: SchematicTree, selector: str | None) -> list[Sheet] | None:
    """The sheets a ``sheet`` parameter names, or None when it names none.

    ``None`` means the whole tree, and ``"all"`` is the same thing said out
    loud. Defaulting to everything rather than to the root is what keeps the
    parameter optional and still general: a flat design *is* a one-sheet tree, so
    this changes nothing for one, while a hierarchical design would otherwise
    check a root sheet that holds nothing but sheet symbols and report a clean
    bill of health. Findings carry their sheet's name, so widening the default
    does not blur where a problem is.

    Any other value is a name, an instance path or a linked-file stem, resolved
    by :meth:`SchematicTree.find`.

    ``None`` as a *return* means the selector matched nothing. Reporting that as
    a skip listing the valid names is the point: a typo that quietly checked
    nothing would look exactly like a design with no findings.
    """
    if selector is None or selector.strip().lower() == "all":
        return tree.sheets()
    found = tree.find(selector)
    return [found] if found is not None else None


def _root_sheet(root: FileSchematic) -> Sheet:
    """The root file as a sheet, which no ``(sheet ...)`` node in it describes."""
    return Sheet(
        name=root.path.stem,
        filename=root.path.name,
        resolved=root.path,
        candidates=(root.path,),
        uuid=root.uuid(),
        x=0.0,
        y=0.0,
        width=0.0,
        height=0.0,
        pages={},
        path=ROOT_PATH,
        pins=(),
        properties={},
    )


@dataclass(frozen=True)
class SheetTarget:
    """One sheet's file and its geometry: everything a per-sheet check needs.

    ``prefix`` is what a finding from this sheet carries in its label, so a check
    that aggregates over sheets does not have to remember the root-sheet
    exception each time.
    """

    sheet: Sheet
    schematic: FileSchematic
    scene: SchematicScene

    @property
    def prefix(self) -> str:
        return label_prefix(self.sheet)


def select_targets(
    tree: SchematicTree, selector: str | None
) -> tuple[list[SheetTarget] | None, bool]:
    """The sheets a per-sheet check should look at, and whether they were found.

    ``(None, False)`` means the selector named no sheet, which the caller reports
    as a skip listing the names that would have worked. A sheet whose file could
    not be read is left out silently: ``sch.sheet.file_missing`` owns that
    finding, and a layout check has nothing useful to say about a file it cannot
    parse.
    """
    sheets = select_sheets(tree, selector)
    if sheets is None:
        return None, False
    targets: list[SheetTarget] = []
    for sheet in sheets:
        schematic = tree.schematic_for(sheet)
        scene = tree.scene_for(sheet)
        if schematic is not None and scene is not None:
            targets.append(SheetTarget(sheet, schematic, scene))
    return targets, True


def label_prefix(sheet: Sheet) -> str:
    """The qualifier a finding from ``sheet`` carries in its label.

    Empty for the root sheet, so every label a flat design has ever produced is
    unchanged. Anything from another sheet is qualified the same way a symbol's
    own text already is: ``R3.Reference`` becomes ``Main.R3.Reference``.
    """
    return "" if sheet.path == ROOT_PATH else f"{sheet.name}."


def _cycle_names(child: Sheet, chain: tuple[Sheet, ...]) -> tuple[str, ...]:
    """The names from the sheet a cycle re-enters back round to itself."""
    start = next(i for i, sheet in enumerate(chain) if sheet.resolved == child.resolved)
    return (*(sheet.name for sheet in chain[start:]), child.name)
