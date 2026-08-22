"""#40: `.agentbus/agent` must be read even outside a git repository.

THE BUG THIS PINS, which caused repeated "brain split" identity confusion.

`_agent_from_worktree` resolved the file from the git root and returned None
when `git rev-parse` found none. So in any NON-REPO directory the file
documented as "THE AUTHORITATIVE SOURCE" was unreachable, and
`settings.local.json` — documented as "a DERIVED MIRROR, never an independent
declaration" — won permanently.

WHY IT SURVIVED SO LONG: `agentbus setup`, on an identity mismatch, prints
"mkdir -p .agentbus && printf ... > .agentbus/agent, then re-run this setup".
In a non-repo directory that remedy silently no-ops. The operator follows the
printed instruction, nothing changes, and nothing reports why — a remedy that
cannot go green. Measured on infra-manager in /home/farshid/develop: 195
`no_credential` gate failures between 2026-08-17 and 2026-08-22, all silent.

PRECEDENCE IS NOT CHANGED by the fix and is asserted below in both directions:
$AGENTBUS_AGENT still outranks the file, and the file still outranks
settings.local.json.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentbus_client.onboarding import _identity


@pytest.fixture(autouse=True)
def _no_env_identity(monkeypatch):
    """The env var outranks everything, so it must be absent for these cases."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)


def _declare(root, name):
    (root / ".agentbus").mkdir(parents=True, exist_ok=True)
    (root / ".agentbus" / "agent").write_text(f"{name}\n")


def _settings(root, name):
    d = root / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.local.json").write_text(json.dumps({"env": {"AGENTBUS_AGENT": name}}))


def test_declared_identity_is_read_outside_a_git_repo(tmp_path, monkeypatch):
    """The regression itself: no git root must not mean no identity."""
    _declare(tmp_path, "declared-name")
    monkeypatch.chdir(tmp_path)
    assert _identity._resolve_agent_name() == "declared-name"


def test_declared_identity_still_works_inside_a_git_repo(tmp_path, monkeypatch):
    """Known-positive control: the path that already worked must keep working."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    _declare(tmp_path, "repo-name")
    monkeypatch.chdir(tmp_path)
    assert _identity._resolve_agent_name() == "repo-name"


def test_no_file_outside_a_repo_still_resolves_to_nothing(tmp_path, monkeypatch):
    """Known-negative: the fallback must not invent an identity.

    Without this, the test above would pass in a world where the resolver
    returned some directory-derived name for every directory on the machine —
    which is the 'unwired scratch directory attached to another agent's inbox'
    failure the docstring records.
    """
    monkeypatch.chdir(tmp_path)
    assert _identity._resolve_agent_name() is None


def test_worktree_file_outranks_settings_local_json_outside_a_repo(tmp_path, monkeypatch):
    """THE EXACT SHAPE THAT BIT US: both files present, no git repo.

    Before the fix settings.local.json won here, so `setup` re-registered the
    mirrored name forever and the operator's declaration was inert.
    """
    _declare(tmp_path, "declared-name")
    _settings(tmp_path, "mirrored-name")
    monkeypatch.chdir(tmp_path)
    assert _identity._resolve_agent_name() == "declared-name"


def test_settings_local_json_still_answers_when_nothing_is_declared(tmp_path, monkeypatch):
    """Known-negative for the assertion above: it must be able to lose."""
    _settings(tmp_path, "mirrored-name")
    monkeypatch.chdir(tmp_path)
    assert _identity._resolve_agent_name() == "mirrored-name"


def test_env_var_still_outranks_the_declared_file(tmp_path, monkeypatch):
    """Precedence step 1 is unchanged — the 2026-08-11 incident stays fixed."""
    _declare(tmp_path, "declared-name")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", "env-name")
    assert _identity._resolve_agent_name() == "env-name"


def test_the_source_is_named_so_a_wrong_identity_is_debuggable(tmp_path, monkeypatch):
    """A wrong identity that announces its provenance is debuggable; this whole
    incident cost hours because nothing said which source had answered."""
    _declare(tmp_path, "declared-name")
    monkeypatch.chdir(tmp_path)
    explain: list[str] = []
    _identity._resolve_agent_name(explain)
    assert explain and ".agentbus/agent" in explain[0]
