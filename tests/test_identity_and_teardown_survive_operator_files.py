"""#93 items 5 to 10: identity readers and teardown read a bad settings file as no identity."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

import pytest

from agentbus_client.onboarding import _identity, _opencode_setup

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("env", [None, "x", ["a"], 5, True])
def test_identity_resolution_reads_a_malformed_env_as_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: object
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({"env": env}))
    monkeypatch.setattr(_identity, "_project_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(_identity, "_agent_from_worktree", lambda: None)
    assert _identity._resolve_agent_name() is None
    assert _identity._session_identity() is None


@pytest.mark.parametrize("name", [5, ["alpha"], {"a": 1}, True, ""])
def test_identity_resolution_reads_a_non_string_agent_as_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: object
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": name}})
    )
    monkeypatch.setattr(_identity, "_project_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(_identity, "_agent_from_worktree", lambda: None)
    assert _identity._resolve_agent_name() is None


def test_identity_resolution_still_reads_a_declared_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}})
    )
    monkeypatch.setattr(_identity, "_project_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(_identity, "_agent_from_worktree", lambda: None)
    assert _identity._resolve_agent_name() == "alpha"
    assert _identity._session_identity() == "alpha"


@pytest.mark.parametrize("body", ["[]", "null", "5", '"x"'])
def test_a_signin_state_of_the_wrong_shape_names_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    state = tmp_path / "signin.json"
    state.write_text(body)
    monkeypatch.setattr(_identity, "_signin_state_path", lambda: state)
    monkeypatch.setattr(_identity, "_agent_from_worktree", lambda: None)
    monkeypatch.setattr(_identity, "_project_claude_dir", lambda: tmp_path / ".claude")
    assert _identity._signed_in_bound_agent() is None
    assert _identity._session_identity() is None


def test_a_signin_state_still_names_its_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "signin.json"
    state.write_text(json.dumps({"default_agent": "alpha", "operator": False}))
    monkeypatch.setattr(_identity, "_signin_state_path", lambda: state)
    monkeypatch.setattr(_identity, "_agent_from_worktree", lambda: None)
    monkeypatch.setattr(_identity, "_project_claude_dir", lambda: tmp_path / ".claude")
    assert _identity._signed_in_bound_agent() == "alpha"
    assert _identity._session_identity() == "alpha"


def _wire_for_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: object) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_opencode_setup, "_git_root_or_none", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_text("alpha\n")
    path = tmp_path / ".claude" / "settings.local.json"
    path.write_text(json.dumps(local))
    return path


def _teardown(**flags: bool) -> int:
    return _opencode_setup.cmd_teardown(argparse.Namespace(**flags))


@pytest.mark.parametrize("env", [None, "x", ["a"], 5, True, {"AGENTBUS_AGENT": 5}])
def test_teardown_leaves_a_malformed_env_alone_and_still_removes_the_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: object,
) -> None:
    path = _wire_for_teardown(tmp_path, monkeypatch, {"env": env, "other": 1})
    body = path.read_text()
    assert _teardown() == 0
    assert path.read_text() == body
    assert not (tmp_path / ".agentbus").exists()
    assert f"{tmp_path / '.agentbus'}/" in capsys.readouterr().out


def test_teardown_with_no_agent_file_and_a_malformed_env_names_no_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire_for_teardown(tmp_path, monkeypatch, {"env": "x"})
    (tmp_path / ".agentbus" / "agent").unlink()
    (tmp_path / ".agentbus").rmdir()
    assert _teardown(purge_key=True) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_teardown_with_a_non_string_agent_does_not_reach_the_key_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire_for_teardown(tmp_path, monkeypatch, {"env": {"AGENTBUS_AGENT": 5}})
    (tmp_path / ".agentbus" / "agent").unlink()
    (tmp_path / ".agentbus").rmdir()
    assert _teardown(purge_key=True) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_teardown_still_drops_only_our_key_from_a_well_formed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = {"env": {"AGENTBUS_AGENT": "alpha", "FOO": "bar"}, "other": 1}
    path = _wire_for_teardown(tmp_path, monkeypatch, local)
    assert _teardown() == 0
    assert json.loads(path.read_text()) == {"env": {"FOO": "bar"}, "other": 1}


def test_teardown_removes_a_file_it_empties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _wire_for_teardown(tmp_path, monkeypatch, {"env": {"AGENTBUS_AGENT": "alpha"}})
    assert _teardown() == 0
    assert not path.exists()
