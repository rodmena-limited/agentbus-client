"""#94: register and the identity banner cope with an odd settings.local.json.

`register` has already created the agent on the server by the time it wires the
project, so an odd file there has to reach the operator as a note.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

import pytest

from agentbus_client import onboarding
from agentbus_client.cli import _identity_cmd, _register

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.setattr(onboarding, "_project_claude_dir", lambda: claude)
    return claude / "settings.local.json"


def wire() -> str:
    args = argparse.Namespace(ephemeral=False, unlisted=False)
    return _register._project_wiring_note(args, "new-agent")


@pytest.mark.parametrize("settings", ['{"env": null}', "{}"])
def test_absent_env_is_wired(project: Path, settings: str) -> None:
    project.write_text(settings)
    note = wire()
    assert "NOT WIRED" not in note
    assert json.loads(project.read_text())["env"] == {"AGENTBUS_AGENT": "new-agent"}


def test_other_env_values_survive_wiring(project: Path) -> None:
    project.write_text('{"env": {"KEEP": "me"}}')
    wire()
    assert json.loads(project.read_text())["env"] == {"KEEP": "me", "AGENTBUS_AGENT": "new-agent"}


@pytest.mark.parametrize("settings", ['{"env": "x"}', '{"env": [1]}', "[]", "{not json"])
def test_a_file_register_cannot_use_is_a_note_and_stays_as_it_was(
    project: Path, settings: str
) -> None:
    project.write_text(settings)
    note = wire()
    assert "NOT WIRED" in note
    assert str(project) in note
    assert project.read_text() == settings


def test_a_project_that_belongs_to_someone_else_is_still_refused(project: Path) -> None:
    settings = '{"env": {"AGENTBUS_AGENT": "someone-else"}}'
    project.write_text(settings)
    note = wire()
    assert "already belongs to 'someone-else'" in note
    assert project.read_text() == settings


@pytest.mark.parametrize("settings", ['{"env": "x"}', '{"env": null}', "[]", '"text"', "{not json"])
def test_the_identity_banner_reads_past_a_file_with_no_agent(project: Path, settings: str) -> None:
    project.write_text(settings)
    _name, sources, conflict = _identity_cmd._identity_resolution()
    assert not any("settings.local.json" in source for source in sources)
    assert conflict is False


def test_the_identity_banner_still_reads_a_good_file(project: Path) -> None:
    project.write_text('{"env": {"AGENTBUS_AGENT": "real-agent"}}')
    _name, sources, _conflict = _identity_cmd._identity_resolution()
    assert sources == ["settings.local.json (real-agent)"]
