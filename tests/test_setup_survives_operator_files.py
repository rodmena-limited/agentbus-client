"""#93 items 5, 6, 7: null is absence, and a file setup cannot use is refused before it writes.

A refusal names the file, leaves its bytes alone and stops before provisioning, so a run that
fails has registered nothing, minted nothing and opted nothing in.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.onboarding import (
    _agy_setup,
    _claude_setup,
    _identity,
    _opencode_setup,
    _paths,
)

if TYPE_CHECKING:
    from pathlib import Path

FAKE_CLAUDE = """#!/bin/sh
exit 0
"""


@pytest.fixture
def provisioned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stands in for the server round trip and records each harness that reached it."""
    calls: list[str] = []

    def provision(args: Any, report: list[str], harness: str) -> str:
        calls.append(harness)
        return "alpha"

    monkeypatch.setattr(_claude_setup, "_provision_project_agent", provision)
    monkeypatch.setattr(_opencode_setup, "_provision_project_agent", provision)
    monkeypatch.setattr(_agy_setup, "_provision_project_agent", provision)
    return calls


@pytest.fixture
def claude_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provisioned: list[str]
) -> dict[str, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    project = tmp_path / "project"
    project.mkdir()
    cc = tmp_path / "claude-config"
    cc.mkdir()

    class _NoNetwork:
        def __init__(self, *a: Any, **k: Any) -> None:
            msg = "no network in this test"
            raise ConnectionError(msg)

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    monkeypatch.chdir(project)
    monkeypatch.setattr(_claude_setup, "_write_worktree_identity", lambda name, report: None)
    monkeypatch.setattr(_claude_setup, "_project_claude_dir", lambda: project / ".claude")
    monkeypatch.setattr(_claude_setup, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: None)
    monkeypatch.setattr(_claude_setup, "AgentBus", _NoNetwork)
    monkeypatch.setattr(
        _claude_setup, "refresh_skill", lambda base_url=None, harness=None: ("current", "stub")
    )
    monkeypatch.setattr(_claude_setup, "doctor_credential_scope", lambda base_url=None: [])
    (project / ".claude").mkdir()
    return {
        "local": project / ".claude" / "settings.local.json",
        "settings": cc / "settings.json",
    }


def _setup_claude() -> int:
    return _claude_setup._setup_claude(argparse.Namespace(base_url="https://example.invalid"))


def _refused(run: Any) -> str:
    with pytest.raises(SystemExit) as caught:
        run()
    return str(caught.value.code)


def test_env_null_is_absence_and_setup_writes_the_identity(
    claude_box: dict[str, Path], provisioned: list[str]
) -> None:
    claude_box["local"].write_text(json.dumps({"env": None, "permissions": {"allow": ["x"]}}))
    _setup_claude()
    data = json.loads(claude_box["local"].read_text())
    assert data == {"env": {"AGENTBUS_AGENT": "alpha"}, "permissions": {"allow": ["x"]}}
    assert provisioned == ["claude"]


def test_a_foreign_env_key_survives_the_identity_write(claude_box: dict[str, Path]) -> None:
    claude_box["local"].write_text(json.dumps({"env": {"FOO": "bar"}}))
    _setup_claude()
    assert json.loads(claude_box["local"].read_text()) == {
        "env": {"FOO": "bar", "AGENTBUS_AGENT": "alpha"}
    }


@pytest.mark.parametrize("env", ["x", ["a"], [], 5, 0, True, False, ""])
def test_env_of_the_wrong_type_is_refused_before_anything_is_written(
    claude_box: dict[str, Path], provisioned: list[str], env: object
) -> None:
    body = json.dumps({"env": env, "other": 1})
    claude_box["local"].write_text(body)
    message = _refused(_setup_claude)
    assert str(claude_box["local"]) in message
    assert '"env" is not a JSON object' in message
    assert claude_box["local"].read_text() == body
    assert provisioned == []


@pytest.mark.parametrize("target", ["local", "settings"])
@pytest.mark.parametrize("body", [b"{not json", b"[]", b"null", b"\xff\xfe\x00"])
def test_a_settings_file_setup_cannot_parse_is_refused_before_anything_is_written(
    claude_box: dict[str, Path], provisioned: list[str], target: str, body: bytes
) -> None:
    claude_box[target].write_bytes(body)
    message = _refused(_setup_claude)
    assert str(claude_box[target]) in message
    assert claude_box[target].read_bytes() == body
    assert provisioned == []


def test_a_settings_path_that_is_a_directory_is_refused_by_name(
    claude_box: dict[str, Path], provisioned: list[str]
) -> None:
    claude_box["settings"].mkdir()
    message = _refused(_setup_claude)
    assert str(claude_box["settings"]) in message
    assert provisioned == []


def test_hooks_null_is_absence_and_our_hooks_are_merged(claude_box: dict[str, Path]) -> None:
    claude_box["settings"].write_text(json.dumps({"hooks": None, "theme": "dark"}))
    _setup_claude()
    data = json.loads(claude_box["settings"].read_text())
    assert data["theme"] == "dark"
    assert sorted(data["hooks"]) == ["SessionStart", "Stop", "UserPromptSubmit"]


def test_hooks_null_with_the_plugin_enabled_defers_to_the_plugin(
    claude_box: dict[str, Path],
) -> None:
    settings = {"hooks": None, "enabledPlugins": {"agentbus@rodmena": True}}
    claude_box["settings"].write_text(json.dumps(settings))
    assert _setup_claude() == 0
    assert json.loads(claude_box["settings"].read_text())["enabledPlugins"] == {
        "agentbus@rodmena": True
    }


@pytest.mark.parametrize("hooks", [["x"], "x", 5, []])
def test_hooks_of_the_wrong_type_are_refused_before_anything_is_written(
    claude_box: dict[str, Path], provisioned: list[str], hooks: object
) -> None:
    body = json.dumps({"hooks": hooks})
    claude_box["settings"].write_text(body)
    message = _refused(_setup_claude)
    assert str(claude_box["settings"]) in message
    assert '"hooks" is not a JSON object' in message
    assert claude_box["settings"].read_text() == body
    assert provisioned == []


def test_an_event_holding_null_is_absence(claude_box: dict[str, Path]) -> None:
    claude_box["settings"].write_text(json.dumps({"hooks": {"Stop": None}}))
    _setup_claude()
    hooks = json.loads(claude_box["settings"].read_text())["hooks"]
    assert any("stop-rewake.sh" in h["command"] for g in hooks["Stop"] for h in g["hooks"])


def test_an_event_of_the_wrong_type_is_refused_and_names_the_settings_file(
    claude_box: dict[str, Path],
) -> None:
    claude_box["settings"].write_text(json.dumps({"hooks": {"Stop": "x"}}))
    message = _refused(_setup_claude)
    assert "hooks.Stop" in message
    assert str(claude_box["settings"]) in message


def test_foreign_junk_in_a_hook_event_survives_the_merge(claude_box: dict[str, Path]) -> None:
    junk = ["x", None, {"hooks": None}, {"hooks": ["y", {"command": "foreign"}]}]
    claude_box["settings"].write_text(json.dumps({"hooks": {"Stop": list(junk)}}))
    _setup_claude()
    groups = json.loads(claude_box["settings"].read_text())["hooks"]["Stop"]
    assert groups[:4] == junk
    assert "stop-rewake.sh" in groups[4]["hooks"][0]["command"]


def test_foreign_junk_survives_removal_when_the_plugin_takes_over(
    claude_box: dict[str, Path],
) -> None:
    ours = {"hooks": [{"type": "command", "command": "x/stop-rewake.sh"}, "y"]}
    junk = ["x", None, {"hooks": None}]
    settings = {
        "hooks": {"Stop": [*junk, ours]},
        "enabledPlugins": {"agentbus@rodmena": True},
    }
    claude_box["settings"].write_text(json.dumps(settings))
    assert _setup_claude() == 0
    groups = json.loads(claude_box["settings"].read_text())["hooks"]["Stop"]
    assert groups == [*junk, {"hooks": ["y"]}]


@pytest.mark.parametrize("enabled", [["agentbus@rodmena"], "agentbus@rodmena", 5, True])
def test_enabled_plugins_of_the_wrong_type_reads_as_not_enabled(enabled: object) -> None:
    assert _paths._plugin_provides_wake({"enabledPlugins": enabled}) is False


def test_an_enabled_plugin_is_still_recognised() -> None:
    assert _paths._plugin_provides_wake({"enabledPlugins": {"agentbus@rodmena": True}}) is True


@pytest.fixture
def opencode_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provisioned: list[str]
) -> dict[str, Path]:
    project = tmp_path / "oc-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(_opencode_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_opencode_setup, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(_opencode_setup, "_install_opencode_skill", lambda base_url, report: None)
    return {"config": project / "opencode.json"}


def _setup_opencode() -> int:
    return _opencode_setup._setup_opencode(argparse.Namespace(base_url="https://example.invalid"))


def test_mcp_null_is_absence_and_the_entry_is_written(
    opencode_box: dict[str, Path], provisioned: list[str]
) -> None:
    opencode_box["config"].write_text(json.dumps({"mcp": None, "theme": "dark"}))
    assert _setup_opencode() == 0
    data = json.loads(opencode_box["config"].read_text())
    assert data["theme"] == "dark"
    assert data["mcp"]["agentbus"]["headers"] == {"Authorization": "Bearer ab_sk_alpha"}
    assert data["mcp"]["agentbus"]["url"] == "https://example.invalid/mcp"
    assert data["plugin"] == [_paths.OPENCODE_PLUGIN_NPM]
    assert provisioned == ["opencode"]


def test_a_foreign_mcp_server_survives_the_merge(opencode_box: dict[str, Path]) -> None:
    opencode_box["config"].write_text(json.dumps({"mcp": {"other": {"type": "local"}}}))
    assert _setup_opencode() == 0
    data = json.loads(opencode_box["config"].read_text())
    assert data["mcp"]["other"] == {"type": "local"}
    assert "agentbus" in data["mcp"]


def test_an_agentbus_entry_holding_null_is_replaced(opencode_box: dict[str, Path]) -> None:
    opencode_box["config"].write_text(json.dumps({"mcp": {"agentbus": None}}))
    assert _setup_opencode() == 0
    data = json.loads(opencode_box["config"].read_text())
    assert data["mcp"]["agentbus"]["headers"] == {"Authorization": "Bearer ab_sk_alpha"}


@pytest.mark.parametrize("mcp", [["x"], [], "x", "", 5, 0, True, False])
def test_mcp_of_the_wrong_type_is_refused_before_anything_is_written(
    opencode_box: dict[str, Path], provisioned: list[str], mcp: object
) -> None:
    body = json.dumps({"mcp": mcp})
    opencode_box["config"].write_text(body)
    message = _refused(_setup_opencode)
    assert str(opencode_box["config"]) in message
    assert '"mcp" is not a JSON object' in message
    assert opencode_box["config"].read_text() == body
    assert provisioned == []


@pytest.mark.parametrize("plugin", [{"a": 1}, 5, True])
def test_plugin_of_the_wrong_type_is_refused_before_anything_is_written(
    opencode_box: dict[str, Path], provisioned: list[str], plugin: object
) -> None:
    body = json.dumps({"plugin": plugin})
    opencode_box["config"].write_text(body)
    message = _refused(_setup_opencode)
    assert str(opencode_box["config"]) in message
    assert '"plugin" is not a list of plugins' in message
    assert opencode_box["config"].read_text() == body
    assert provisioned == []


@pytest.mark.parametrize(
    ("plugin", "expected"),
    [
        (None, [_paths.OPENCODE_PLUGIN_NPM]),
        ("other", ["other", _paths.OPENCODE_PLUGIN_NPM]),
        (["other"], ["other", _paths.OPENCODE_PLUGIN_NPM]),
    ],
)
def test_plugin_null_string_and_list_all_merge(
    opencode_box: dict[str, Path], plugin: object, expected: list[str]
) -> None:
    opencode_box["config"].write_text(json.dumps({"plugin": plugin}))
    assert _setup_opencode() == 0
    assert json.loads(opencode_box["config"].read_text())["plugin"] == expected


@pytest.mark.parametrize("body", [b"{not json", b"[]", b"null", b"\xff\xfe\x00"])
def test_an_opencode_config_setup_cannot_parse_is_refused_before_anything_is_written(
    opencode_box: dict[str, Path], provisioned: list[str], body: bytes
) -> None:
    opencode_box["config"].write_bytes(body)
    message = _refused(_setup_opencode)
    assert str(opencode_box["config"]) in message
    assert opencode_box["config"].read_bytes() == body
    assert provisioned == []


@pytest.fixture
def agy_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provisioned: list[str]
) -> dict[str, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGY_CONFIG_HOME", str(tmp_path / "gemini"))
    monkeypatch.setattr(_agy_setup, "_git_root_or_none", lambda: repo)
    monkeypatch.setattr(_identity, "_git_root_or_none", lambda: repo)
    monkeypatch.setattr(_agy_setup, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(_agy_setup, "_skill_note", lambda base_url: "skill: stub")
    monkeypatch.setattr(_agy_setup, "_publish_sealing_key", lambda b, n, report: None)
    plugin = _paths.agy_plugin_dir()
    plugin.mkdir(parents=True)
    return {"repo": repo, "manifest": plugin / "plugin.json", "hooks": plugin / "hooks.json"}


def _setup_agy() -> int:
    return _agy_setup._setup_agy(argparse.Namespace(base_url="https://example.invalid"))


@pytest.mark.parametrize("target", ["manifest", "hooks"])
@pytest.mark.parametrize("body", [b"{not json", b"[]", b"null", b"\xff\xfe\x00"])
def test_a_plugin_file_setup_cannot_parse_is_refused_before_the_opt_in(
    agy_box: dict[str, Path], provisioned: list[str], target: str, body: bytes
) -> None:
    agy_box[target].write_bytes(body)
    message = _refused(_setup_agy)
    assert str(agy_box[target]) in message
    assert agy_box[target].read_bytes() == body
    assert _paths.agy_is_wired(agy_box["repo"]) is False
    assert not (agy_box["repo"] / ".agentbus" / "agent").exists()
    assert provisioned == []


def test_an_empty_plugin_manifest_is_replaced_by_ours(agy_box: dict[str, Path]) -> None:
    agy_box["manifest"].write_text("")
    assert _setup_agy() == 0
    assert json.loads(agy_box["manifest"].read_text())["name"] == "agentbus"
    assert _paths.agy_is_wired(agy_box["repo"]) is True


def test_a_valid_but_different_manifest_is_rewritten_as_before(agy_box: dict[str, Path]) -> None:
    agy_box["manifest"].write_text(json.dumps({"name": "old"}))
    assert _setup_agy() == 0
    assert json.loads(agy_box["manifest"].read_text())["name"] == "agentbus"
