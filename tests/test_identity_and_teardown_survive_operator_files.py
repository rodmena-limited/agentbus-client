"""#93 items 5 to 10, #94 item 13: identity readers and teardown survive bad files.

A settings file of the wrong shape and a `.agentbus/agent` that is not text both read as no
identity, the same as an empty or unreadable file.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

import pytest

from agentbus_client.cli import _service
from agentbus_client.hooks import _identity as _hook_identity, _session
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


NOT_UTF8 = b"\xff\xfe\x00"


def test_a_worktree_file_that_is_not_text_is_no_declared_agent(tmp_path: Path) -> None:
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_bytes(NOT_UTF8)
    assert _identity._agent_from_worktree(tmp_path) is None


def test_a_worktree_file_that_is_text_still_declares_its_agent(tmp_path: Path) -> None:
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_text("alpha\n")
    assert _identity._agent_from_worktree(tmp_path) == "alpha"


def test_setup_replaces_a_worktree_file_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_identity, "_git_root_or_none", lambda: tmp_path)
    agent = tmp_path / ".agentbus" / "agent"
    agent.parent.mkdir()
    agent.write_bytes(NOT_UTF8)
    report: list[str] = []
    _identity._write_worktree_identity("alpha", report)
    assert agent.read_text() == "alpha\n"
    assert report == [f"worktree identity: {agent} = alpha"]


def test_setup_leaves_a_worktree_file_that_already_declares_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_identity, "_git_root_or_none", lambda: tmp_path)
    agent = tmp_path / ".agentbus" / "agent"
    agent.parent.mkdir()
    agent.write_text("alpha\n")
    report: list[str] = []
    _identity._write_worktree_identity("alpha", report)
    assert report == [f"worktree identity: {agent} (already alpha)"]


def test_teardown_falls_back_to_the_settings_agent_past_a_file_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    local = _wire_for_teardown(tmp_path, monkeypatch, {"env": {"AGENTBUS_AGENT": "alpha"}})
    (tmp_path / ".agentbus" / "agent").write_bytes(NOT_UTF8)
    assert _teardown() == 0
    assert not (tmp_path / ".agentbus").exists()
    assert not local.exists()
    assert "server-side agent 'alpha' is untouched" in capsys.readouterr().out


def test_the_opencode_reader_prefers_a_text_worktree_file_over_settings(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    agent.write_text("beta\n")
    local = {"env": {"AGENTBUS_AGENT": "alpha"}}
    assert _opencode_setup._declared_agent(agent, local) == "beta"
    agent.write_bytes(NOT_UTF8)
    assert _opencode_setup._declared_agent(agent, local) == "alpha"


def test_the_retire_warning_reads_past_a_worktree_file_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("agentbus_client.onboarding._git_root_or_none", lambda: tmp_path)
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_bytes(NOT_UTF8)
    (tmp_path / ".claude").mkdir()
    local = tmp_path / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}}))
    _service._warn_local_wiring_will_revive("alpha")
    out = capsys.readouterr().out
    assert "still DECLARES 'alpha'" in out
    assert str(local) in out
    assert str(tmp_path / ".agentbus" / "agent") not in out


def test_the_retire_warning_names_a_worktree_file_that_is_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("agentbus_client.onboarding._git_root_or_none", lambda: tmp_path)
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_text("alpha\n")
    _service._warn_local_wiring_will_revive("alpha")
    assert str(tmp_path / ".agentbus" / "agent") in capsys.readouterr().out


def test_the_hook_readers_read_past_a_worktree_file_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_hook_identity, "_repo_root", lambda: tmp_path)
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_bytes(NOT_UTF8)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}})
    )
    assert _hook_identity._read_declared_agent(tmp_path) == "alpha"
    assert _hook_identity._agent_from_worktree() is None


def test_the_session_warning_reads_past_a_worktree_file_that_is_not_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_hook_identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", "other")
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_bytes(NOT_UTF8)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}})
    )
    _session._warn_if_env_overrides_this_checkout("other")
    assert "this checkout is wired to 'alpha'" in capsys.readouterr().out
