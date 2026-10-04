from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbus_client.onboarding import _credentials


def _project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: str) -> Path:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(settings)
    monkeypatch.chdir(project)
    monkeypatch.setattr(_credentials, "_project_claude_dir", lambda: project / ".claude")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(_credentials, "_scope_of_bearer", lambda header, base_url=None: "full")
    return project


def test_invalid_project_settings_are_reported_and_the_other_slots_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _project(monkeypatch, tmp_path, '{"env": {"AGENTBUS_AGENT": "x"},,}')
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"agentbus": {"headers": {"Authorization": "Bearer ab_sk_x"}}}})
    )
    lines = _credentials.doctor_credential_scope("http://127.0.0.1:9")
    assert any("settings.local.json): NOT CHECKED" in line for line in lines), lines
    assert any(
        line.startswith("user-scope ~/.claude.json agentbus MCP: full") and "FINDING" in line
        for line in lines
    ), lines


def test_valid_project_settings_still_name_the_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(monkeypatch, tmp_path, json.dumps({"env": {"AGENTBUS_AGENT": "agent-7"}}))
    lines = _credentials.doctor_credential_scope("http://127.0.0.1:9")
    assert lines[0] == f"project ({project / '.claude'}/settings.local.json): agent agent-7"
