"""Tests for DesignContext path resolution, CLI dispatch and lifecycle.

subprocess.Popen, shutil.which and the kipy module are all monkeypatched so no
KiCad installation is required.
"""

import subprocess
import sys
import types
from pathlib import Path

import pytest

from kicad_evaltor.core import context as context_module
from kicad_evaltor.core.context import DesignContext
from kicad_evaltor.utils.kicad_cli import SubprocessResult


class FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.terminated = False
        self.killed = False
        self.wait_raises = None
        self.waited = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        if self.wait_raises is not None:
            raise self.wait_raises
        return 0


@pytest.fixture
def popen_calls(monkeypatch):
    created = []

    def fake_popen(cmd, **kwargs):
        process = FakePopen(cmd, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return created


def install_fake_kipy(monkeypatch, ping_error=None):
    """Provide a kipy stub matching the port/timeout API context.py expects."""
    instances = []

    class FakeKiCad:
        def __init__(self, port=5051, timeout=2000):
            self.port = port
            self.timeout = timeout
            self.closed = False
            instances.append(self)

        def ping(self):
            if ping_error is not None:
                raise ping_error

        def close(self):
            self.closed = True

    module = types.ModuleType("kipy")
    module.KiCad = FakeKiCad
    monkeypatch.setitem(sys.modules, "kipy", module)
    return instances


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
    def test_project_requires_a_path(self):
        with pytest.raises(ValueError, match="No project, schematic, or board path"):
            DesignContext().project

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

    def test_board_is_fetched_once(self, tmp_path):
        pcb = tmp_path / "a.kicad_pcb"
        pcb.write_text("x")

        fetched = []
        ctx = DesignContext(board_path=pcb)
        ctx._kicad = types.SimpleNamespace(
            get_board=lambda: fetched.append("b") or "board",
            close=lambda: None,
        )

        assert ctx.board == "board"
        assert ctx.board == "board"
        assert len(fetched) == 1


class TestRunKiCadCli:
    def test_reports_missing_binary(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
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

    def test_falls_back_to_path_lookup(self, monkeypatch):
        import shutil

        seen = {}
        monkeypatch.setattr(shutil, "which", lambda _name: f"/usr/bin/{_name}")
        monkeypatch.setattr(
            context_module,
            "run_kicad_cli",
            lambda args, **_kw: seen.setdefault("args", args) or SubprocessResult(0, "", ""),
        )

        DesignContext().run_kicad_cli(["pcb", "drc"])
        assert seen["args"] == ["/usr/bin/kicad-cli", "pcb", "drc"]


class TestHeadlessServerStartup:
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)

    def test_raises_when_cli_is_nowhere_to_be_found(self, monkeypatch, tmp_path):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda _name: None)
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(RuntimeError, match="kicad-cli not found"):
            DesignContext(schematic_path=tmp_path / "a.kicad_sch")._start_headless_server()

    def test_uses_whichever_cli_path_is_given(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch",
            kicad_cli_path="/custom/kicad-cli",
        )
        ctx._start_headless_server()

        assert popen_calls[0].cmd[0] == "/custom/kicad-cli"

    def test_command_includes_port_and_schematic_file(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch",
            kicad_cli_path="/custom/kicad-cli",
            timeout_ms=1234,
        )
        ctx._start_headless_server()

        assert popen_calls[0].cmd == [
            "/custom/kicad-cli",
            "api-server",
            "--port",
            "5051",
            "--file",
            str(tmp_path / "a.kicad_sch"),
        ]

    def test_project_path_is_preferred_for_the_file_argument(
        self, monkeypatch, popen_calls, tmp_path
    ):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            project_path=tmp_path / "d.kicad_pro",
            board_path=tmp_path / "d.kicad_pcb",
            kicad_cli_path="/custom/kicad-cli",
        )
        ctx._start_headless_server()

        assert popen_calls[0].cmd[-1] == str(tmp_path / "d.kicad_pro")

    def test_board_path_used_when_it_is_the_only_input(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(board_path=tmp_path / "d.kicad_pcb", kicad_cli_path="/custom/kicad-cli")
        ctx._start_headless_server()

        assert popen_calls[0].cmd[-1] == str(tmp_path / "d.kicad_pcb")

    def test_file_argument_omitted_without_paths(self, monkeypatch, popen_calls):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        DesignContext(kicad_cli_path="/custom/kicad-cli")._start_headless_server()

        assert "--file" not in popen_calls[0].cmd

    def test_second_call_does_not_spawn_another_server(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch", kicad_cli_path="/custom/kicad-cli"
        )
        ctx._start_headless_server()
        ctx._start_headless_server()

        assert len(popen_calls) == 1

    def test_connects_with_the_configured_port_and_timeout(
        self, monkeypatch, popen_calls, tmp_path
    ):
        self._no_sleep(monkeypatch)
        instances = install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch",
            kicad_cli_path="/custom/kicad-cli",
            timeout_ms=4321,
        )
        ctx._start_headless_server()

        assert instances[-1].port == 5051
        assert instances[-1].timeout == 100

    def test_raises_when_server_never_becomes_ready(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        install_fake_kipy(monkeypatch, ping_error=ConnectionError("refused"))

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch", kicad_cli_path="/custom/kicad-cli"
        )
        with pytest.raises(RuntimeError, match="Failed to start kicad-cli api-server"):
            ctx._start_headless_server()

    def test_keeps_trying_until_the_server_answers(self, monkeypatch, popen_calls, tmp_path):
        self._no_sleep(monkeypatch)
        attempts = {"n": 0}

        class FlakyKiCad:
            def __init__(self, port=5051, timeout=2000):
                pass

            def ping(self):
                attempts["n"] += 1
                if attempts["n"] < 5:
                    raise ConnectionError("refused")

        module = types.ModuleType("kipy")
        module.KiCad = FlakyKiCad
        monkeypatch.setitem(sys.modules, "kipy", module)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch", kicad_cli_path="/custom/kicad-cli"
        )
        ctx._start_headless_server()

        assert attempts["n"] == 5


class TestClose:
    def test_close_without_a_server_is_a_no_op(self):
        DesignContext().close()

    def test_close_terminates_the_server_process(self, monkeypatch, popen_calls, tmp_path):
        monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch", kicad_cli_path="/custom/kicad-cli"
        )
        ctx._start_headless_server()
        ctx.close()

        assert popen_calls[0].terminated
        assert popen_calls[0].waited
        assert ctx._kicad_instance.process is None
        assert ctx._kicad_instance.started is False

    def test_close_kills_a_process_that_will_not_exit(self, monkeypatch, popen_calls, tmp_path):
        monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)
        install_fake_kipy(monkeypatch)

        ctx = DesignContext(
            schematic_path=tmp_path / "a.kicad_sch", kicad_cli_path="/custom/kicad-cli"
        )
        ctx._start_headless_server()
        popen_calls[0].wait_raises = subprocess.TimeoutExpired(cmd="kicad-cli", timeout=5)
        ctx.close()

        assert popen_calls[0].killed

    def test_close_closes_the_kicad_handle(self):
        ctx = DesignContext()
        ctx._kicad = types.SimpleNamespace(close=lambda: None)
        ctx.close()
        assert ctx._kicad is None

    def test_close_swallows_errors_from_the_kicad_handle(self):
        def boom():
            raise RuntimeError("already gone")

        ctx = DesignContext()
        ctx._kicad = types.SimpleNamespace(close=boom)
        ctx.close()
        assert ctx._kicad is None


class TestContextManager:
    def test_enter_returns_the_context(self, tmp_path):
        sch = tmp_path / "a.kicad_sch"
        sch.write_text("x")
        with DesignContext(schematic_path=sch) as ctx:
            assert ctx.has_schematic()

    def test_exit_closes_the_server(self, monkeypatch, popen_calls, tmp_path):
        monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)
        install_fake_kipy(monkeypatch)

        with DesignContext(
            schematic_path=tmp_path / "a.kicad_sch",
            kicad_cli_path="/custom/kicad-cli",
        ) as ctx:
            ctx._start_headless_server()

        assert popen_calls[0].terminated

    def test_exit_runs_when_the_body_raises(self, monkeypatch, popen_calls, tmp_path):
        monkeypatch.setattr(context_module.time, "sleep", lambda _seconds: None)
        install_fake_kipy(monkeypatch)

        with DesignContext(
            schematic_path=tmp_path / "a.kicad_sch",
            kicad_cli_path="/custom/kicad-cli",
        ) as ctx:
            ctx._start_headless_server()
            with pytest.raises(ValueError, match="boom"):
                raise ValueError("boom")

        assert popen_calls[0].terminated


def test_path_arguments_are_coerced_to_path_objects(tmp_path):
    ctx = DesignContext(schematic_path=str(tmp_path / "a.kicad_sch"))
    assert isinstance(ctx.schematic_path, Path)
