"""#94 item 6: a file setup or teardown cannot write stops the run with one plain line.

The chmod cases raise a real PermissionError. `register` keeps its own NOT WIRED report, so
`_dump_json` still raises OSError and the two commands catch it at their own boundary.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agentbus_client
from agentbus_client.cli import _register
from agentbus_client.onboarding import _agy_setup, _claude_setup, _opencode_setup, _paths

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes every file")

ENABLED_PLUGIN = json.dumps({"enabledPlugins": {"agentbus@rodmena": True}})
STOPPED_RUN = """
from agentbus_client.onboarding import _claude_setup
from agentbus_client.cli import main

_claude_setup._provision_project_agent = lambda args, report, harness: "alpha"
main(["setup", "claude"])
"""


def _provisions_alpha(args: Any, report: list[str], harness: str) -> str:
    return "alpha"


@pytest.fixture
def claude_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (tmp_path / "claude-config").mkdir()
    (tmp_path / "claude-config" / "settings.json").write_text(ENABLED_PLUGIN)

    class _NoNetwork:
        def __init__(self, *a: Any, **k: Any) -> None:
            msg = "no network in this test"
            raise ConnectionError(msg)

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.chdir(project)
    monkeypatch.setattr(_claude_setup, "_provision_project_agent", _provisions_alpha)
    monkeypatch.setattr(_claude_setup, "_write_worktree_identity", lambda name, report: None)
    monkeypatch.setattr(_claude_setup, "_project_claude_dir", lambda: project / ".claude")
    monkeypatch.setattr(_claude_setup, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: None)
    monkeypatch.setattr(_claude_setup, "AgentBus", _NoNetwork)
    monkeypatch.setattr(
        _claude_setup, "refresh_skill", lambda base_url=None, harness=None: ("current", "stub")
    )
    monkeypatch.setattr(_claude_setup, "doctor_credential_scope", lambda base_url=None: [])
    return project / ".claude" / "settings.local.json"


def _setup(harness: str) -> int:
    return _opencode_setup.cmd_setup(
        argparse.Namespace(harness=harness, base_url="https://example.invalid")
    )


def _teardown() -> int:
    return _opencode_setup.cmd_teardown(argparse.Namespace())


def _stopped(run: Any, *args: str) -> str:
    with pytest.raises(SystemExit) as caught:
        run(*args)
    assert isinstance(caught.value.code, str)
    return caught.value.code


def test_a_read_only_settings_file_stops_setup_with_one_line_naming_it(
    claude_local: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = b'{"permissions": {"allow": ["x"]}}'
    claude_local.write_bytes(original)
    claude_local.chmod(0o400)
    message = _stopped(_setup, "claude")
    assert message == (
        f"setup stopped at {claude_local}: Permission denied. Fix that, then run setup again."
    )
    assert claude_local.read_bytes() == original
    out = capsys.readouterr().out
    assert "project identity" not in out
    assert "wake plugin" not in out


def test_setup_completes_once_the_file_can_be_written(claude_local: Path) -> None:
    claude_local.write_text('{"permissions": {"allow": ["x"]}}')
    claude_local.chmod(0o400)
    _stopped(_setup, "claude")
    claude_local.chmod(0o600)
    assert _setup("claude") == 0
    written = json.loads(claude_local.read_text())
    assert written["env"] == {"AGENTBUS_AGENT": "alpha"}
    assert written["permissions"] == {"allow": ["x"]}


def test_a_read_only_directory_stops_setup_naming_the_file_it_needed(claude_local: Path) -> None:
    claude_local.parent.chmod(0o500)
    try:
        message = _stopped(_setup, "claude")
    finally:
        claude_local.parent.chmod(0o700)
    assert message.startswith(f"setup stopped at {claude_local}: Permission denied.")
    assert not claude_local.exists()


def test_a_write_error_that_carries_no_path_still_names_the_file(
    claude_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = Path.write_text

    def full_disk(self: Path, *a: Any, **k: Any) -> int:
        if self == claude_local:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", full_disk)
    message = _stopped(_setup, "claude")
    assert message.startswith(f"setup stopped at {claude_local}: No space left on device.")


def test_an_opencode_config_setup_cannot_write_stops_setup_with_its_bytes_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "oc-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(_opencode_setup, "_provision_project_agent", _provisions_alpha)
    monkeypatch.setattr(_opencode_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_opencode_setup, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(_opencode_setup, "_install_opencode_skill", lambda base_url, report: None)
    config = project / "opencode.json"
    config.write_text("{}")
    config.chmod(0o400)
    message = _stopped(_setup, "opencode")
    assert message.startswith(f"setup stopped at {config}: Permission denied.")
    assert config.read_text() == "{}"
    assert "project identity" not in capsys.readouterr().out


def test_a_plugin_directory_setup_cannot_write_stops_agy_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGY_CONFIG_HOME", str(tmp_path / "gemini"))
    monkeypatch.setattr(_agy_setup, "_provision_project_agent", _provisions_alpha)
    monkeypatch.setattr(_agy_setup, "_git_root_or_none", lambda: repo)
    monkeypatch.setattr(_agy_setup, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(_agy_setup, "_skill_note", lambda base_url: "skill: stub")
    monkeypatch.setattr(_agy_setup, "_publish_sealing_key", lambda b, n, report: None)
    plugin = _paths.agy_plugin_dir()
    plugin.mkdir(parents=True)
    plugin.chmod(0o500)
    try:
        message = _stopped(_setup, "agy")
    finally:
        plugin.chmod(0o700)
    assert message.startswith(f"setup stopped at {plugin}")
    assert "Permission denied." in message


def _wired_for_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_opencode_setup, "_git_root_or_none", lambda: tmp_path)
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_text("alpha\n")
    (tmp_path / ".claude").mkdir()
    local = tmp_path / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}, "permissions": {}}))
    return local


def test_a_settings_file_teardown_cannot_write_stops_it_and_a_rerun_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local = _wired_for_teardown(tmp_path, monkeypatch)
    original = local.read_bytes()
    local.chmod(0o400)
    assert _stopped(_teardown) == (
        f"teardown stopped at {local}: Permission denied. Fix that, then run teardown again."
    )
    assert local.read_bytes() == original
    assert "is untouched" not in capsys.readouterr().out
    local.chmod(0o600)
    assert _teardown() == 0
    assert json.loads(local.read_text()) == {"permissions": {}}


def test_register_keeps_reporting_not_wired_for_a_file_it_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".claude").mkdir()
    local = tmp_path / ".claude" / "settings.local.json"
    local.write_text("{}")
    local.chmod(0o400)
    monkeypatch.setattr("agentbus_client.onboarding._project_claude_dir", lambda: local.parent)
    note = _register._project_wiring_note(
        argparse.Namespace(ephemeral=False, unlisted=False), "alpha"
    )
    assert note.startswith("  NOT WIRED (")
    assert str(local) in note


def test_an_error_that_is_not_a_file_error_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(args: Any) -> int:
        msg = "a bug, not a file problem"
        raise KeyError(msg)

    monkeypatch.setattr(_opencode_setup, "_setup_claude", broken)
    with pytest.raises(KeyError):
        _setup("claude")


def test_the_real_command_prints_one_line_on_stderr_and_exits_one(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    local = project / ".claude" / "settings.local.json"
    local.write_text("{}")
    local.chmod(0o400)
    source = Path(agentbus_client.__file__).parents[1]
    done = subprocess.run(
        [sys.executable, "-c", STOPPED_RUN],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=project,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(source),
            "AGENTBUS_CONFIG_DIR": str(tmp_path / "cfg"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        },
    )
    assert done.returncode == 1
    assert done.stderr.strip().splitlines() == [
        f"setup stopped at {local}: Permission denied. Fix that, then run setup again."
    ]
    assert "Traceback" not in done.stderr
