"""Tests for DesignContext path resolution, CLI dispatch and caching.

kicad-cli discovery is monkeypatched, so no KiCad installation is required.
"""

from pathlib import Path

import pytest

from kicad_evaltor.core import context as context_module
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import SubprocessResult


class TestSchematicPath:
    def test_returns_explicit_schematic_path(self, tmp_path):
        sch = tmp_path / "board.kicad_sch"
        assert DesignContext(schematic_path=sch).schematic_path == sch

    def test_accepts_string_paths(self, tmp_path):
        sch = tmp_path / "board.kicad_sch"
        assert DesignContext(schematic_path=str(sch)).schematic_path == sch

    def test_derived_from_project_path(self, tmp_path):
        project = tmp_path / "design.kicad_pro"
        assert DesignContext(project_path=project).schematic_path == tmp_path / "design.kicad_sch"

    def test_explicit_path_wins_over_project(self, tmp_path):
        ctx = DesignContext(
            project_path=tmp_path / "d.kicad_pro", schematic_path=tmp_path / "s.kicad_sch"
        )
        assert ctx.schematic_path == tmp_path / "s.kicad_sch"

    def test_raises_without_any_path(self):
        with pytest.raises(ValueError, match="No schematic path"):
            DesignContext().schematic_path


class TestBoardPath:
    def test_returns_explicit_board_path(self, tmp_path):
        pcb = tmp_path / "board.kicad_pcb"
        assert DesignContext(board_path=pcb).board_path == pcb

    def test_derived_from_project_path(self, tmp_path):
        project = tmp_path / "design.kicad_pro"
        assert DesignContext(project_path=project).board_path == tmp_path / "design.kicad_pcb"

    def test_raises_without_any_path(self):
        with pytest.raises(ValueError, match="No board path"):
            DesignContext().board_path


class TestHasSchematic:
    def test_true_when_explicit_file_exists(self, tmp_path):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text("x")
        assert DesignContext(schematic_path=sch).has_schematic()

    def test_false_when_explicit_file_missing(self, tmp_path):
        assert not DesignContext(schematic_path=tmp_path / "gone.kicad_sch").has_schematic()

    def test_true_when_sibling_of_project_exists(self, tmp_path):
        (tmp_path / "d.kicad_sch").write_text("x")
        assert DesignContext(project_path=tmp_path / "d.kicad_pro").has_schematic()

    def test_false_when_project_sibling_missing(self, tmp_path):
        assert not DesignContext(project_path=tmp_path / "d.kicad_pro").has_schematic()

    def test_false_without_paths(self):
        assert not DesignContext().has_schematic()

    def test_missing_explicit_path_does_not_fall_back_to_project(self, tmp_path):
        (tmp_path / "d.kicad_sch").write_text("x")
        ctx = DesignContext(
            project_path=tmp_path / "d.kicad_pro", schematic_path=tmp_path / "nope.kicad_sch"
        )
        assert not ctx.has_schematic()


class TestHasBoard:
    def test_true_when_explicit_file_exists(self, tmp_path):
        pcb = tmp_path / "a.kicad_pcb"
        pcb.write_text("x")
        assert DesignContext(board_path=pcb).has_board()

    def test_false_when_explicit_file_missing(self, tmp_path):
        assert not DesignContext(board_path=tmp_path / "gone.kicad_pcb").has_board()

    def test_true_when_sibling_of_project_exists(self, tmp_path):
        (tmp_path / "d.kicad_pcb").write_text("x")
        assert DesignContext(project_path=tmp_path / "d.kicad_pro").has_board()

    def test_false_without_paths(self):
        assert not DesignContext().has_board()


class TestLazyDesignProperties:
    def test_schematic_requires_a_schematic(self, tmp_path):
        with pytest.raises(ValueError, match="No schematic available"):
            DesignContext(schematic_path=tmp_path / "missing.kicad_sch").schematic

    def test_board_requires_a_board(self, tmp_path):
        with pytest.raises(ValueError, match="No board available"):
            DesignContext(board_path=tmp_path / "missing.kicad_pcb").board

    def test_existing_schematic_is_parsed_once(self, tmp_path, monkeypatch):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text('(kicad_sch (version 20250114) (symbol (lib_id "Device:R")))')

        from kicad_evaltor.core import context as context_module

        loaded = []
        real_loader = context_module.load_schematic

        def counting_loader(path, *args, **kwargs):
            loaded.append(path)
            return real_loader(path, *args, **kwargs)

        monkeypatch.setattr(context_module, "load_schematic", counting_loader)

        ctx = DesignContext(schematic_path=sch)
        first = ctx.schematic
        assert ctx.schematic is first
        assert len(loaded) == 1
        assert [s.lib_id for s in first.get_symbols()] == ["Device:R"]

    def test_unreadable_schematic_raises(self, tmp_path):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text("not a schematic")

        with pytest.raises(ValueError, match="Not a KiCad schematic"):
            _ = DesignContext(schematic_path=sch).schematic

    def test_board_is_read_once_from_the_file(self):
        from conftest import ESP_BOARD

        ctx = DesignContext(board_path=ESP_BOARD)

        assert ctx.board is ctx.board
        assert len(ctx.board.footprints) == 46

    def test_board_reads_net_classes_from_the_project(self):
        from conftest import ESP_BOARD

        ctx = DesignContext(project_path=ESP_BOARD.with_suffix(".kicad_pro"))

        assert "VCC" in ctx.board.net_classes.classes

    def test_geometry_without_kicad_python_raises(self, monkeypatch):
        from conftest import ESP_BOARD
        from kicad_evaltor import pcbnew_bridge
        from kicad_evaltor.pcbnew_bridge import GeometryUnavailable

        monkeypatch.setattr(pcbnew_bridge, "find_kicad_python", lambda _cli=None: None)
        ctx = DesignContext(board_path=ESP_BOARD, kicad_cli_path="/custom/kicad-cli")

        with pytest.raises(GeometryUnavailable, match="KiCad's Python"):
            ctx.board_geometry()


class TestMemo:
    def test_computes_once_per_key(self):
        ctx = DesignContext()
        calls = []

        assert ctx.memo("k", lambda: calls.append(1) or "v") == "v"
        assert ctx.memo("k", lambda: calls.append(1) or "w") == "v"
        assert calls == [1]

    def test_an_error_is_not_cached(self):
        ctx = DesignContext()

        def boom():
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            ctx.memo("k", boom)
        assert ctx.memo("k", lambda: "v") == "v"

    def test_close_forgets(self):
        ctx = DesignContext()
        ctx.memo("k", lambda: "v")
        ctx.close()

        assert ctx.memo("k", lambda: "w") == "w"


class TestRunKiCadCli:
    def test_reports_missing_binary(self, monkeypatch):
        monkeypatch.setattr(context_module, "find_kicad_cli", lambda: None)
        result = DesignContext().run_kicad_cli(["sch", "erc"])

        assert result.returncode == -1
        assert "not found" in result.stderr

    def test_uses_explicit_cli_path(self, monkeypatch):
        seen = {}

        def fake_run(args, cwd=None, timeout=60):
            seen["args"] = args
            return SubprocessResult(0, "ok", "")

        monkeypatch.setattr(context_module, "run_kicad_cli", fake_run)
        ctx = DesignContext(kicad_cli_path="/custom/kicad-cli")
        result = ctx.run_kicad_cli(["sch", "erc"])

        assert seen["args"] == ["/custom/kicad-cli", "sch", "erc"]
        assert result.success

    def test_timeout_defaults_to_the_context_setting(self, monkeypatch):
        seen = {}

        def fake_run(args, cwd=None, timeout=60):
            seen["timeout"] = timeout
            return SubprocessResult(0, "ok", "")

        monkeypatch.setattr(context_module, "run_kicad_cli", fake_run)
        ctx = DesignContext(kicad_cli_path="/custom/kicad-cli", cli_timeout=900)
        ctx.run_kicad_cli(["sch", "erc"])
        assert seen["timeout"] == 900

        ctx.run_kicad_cli(["sch", "erc"], timeout=30)
        assert seen["timeout"] == 30

    def test_rejects_a_non_positive_cli_timeout(self):
        with pytest.raises(ValueError, match="cli_timeout"):
            DesignContext(cli_timeout=0)

    def test_falls_back_to_discovery(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(context_module, "find_kicad_cli", lambda: Path("/usr/bin/kicad-cli"))
        monkeypatch.setattr(
            context_module,
            "run_kicad_cli",
            lambda args, **_kw: seen.setdefault("args", args) or SubprocessResult(0, "", ""),
        )

        DesignContext().run_kicad_cli(["pcb", "drc"])
        assert seen["args"] == [str(Path("/usr/bin/kicad-cli")), "pcb", "drc"]


class TestClose:
    def test_close_without_a_server_is_a_no_op(self):
        DesignContext().close()


class TestContextManager:
    def test_enter_returns_the_context(self, tmp_path):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text("x")
        with DesignContext(schematic_path=sch) as ctx:
            assert ctx.has_schematic()


def test_path_arguments_are_coerced_to_path_objects(tmp_path):
    ctx = DesignContext(schematic_path=str(tmp_path / "a.kicad_sch"))
    assert isinstance(ctx.schematic_path, Path)
