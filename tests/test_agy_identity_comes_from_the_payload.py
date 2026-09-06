"""#56: on Antigravity the hook's cwd is NOT the workspace.

Antigravity's own docs, verbatim: "The working directory is set to the directory
containing `hooks.json`." Our hooks.json lives in the plugin, so a hook runs with
cwd = `<repo>/.agents/plugins/agentbus/`.

WHY THIS FILE IS THE DECISIVE ONE. Every other test here passes against a
cwd-based implementation, because the plugin sits INSIDE the repo and
`_resolve_agent()`'s walk up from cwd still happens to reach `.agentbus/agent`.
The implementation only diverges when cwd and the workspace genuinely disagree —
a plugin installed globally, or registered from a shared path via `plugins.json`,
both of which Antigravity supports. So the regression is written the only way it
can go red: cwd pointed somewhere with no identity at all, and the real workspace
named only in the payload.

Also pinned here: the traversal guard. `.agentbus/agent` is attacker-controllable
in this repo's threat model, and the Antigravity lane adopts a credential from a
name read out of it.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from agentbus_client.hooks import _antigravity
from agentbus_client.onboarding import _paths


def _agent_seen(payload: dict, monkeypatch) -> str | None:
    """Run the Stop hook and report which agent it polled as."""
    seen: list[str] = []
    import agentbus_client.rewake as rewake

    monkeypatch.setattr(
        rewake, "poll_for_fresh_mail", lambda agent, **k: seen.append(agent) or None
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with redirect_stdout(io.StringIO()):
        _antigravity.agy_stop(None)
    return seen[0] if seen else None


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".agentbus").mkdir(parents=True)
    (repo / ".agentbus" / "agent").write_text("alpha\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    _paths.agy_mark_wired(repo)
    # cwd is the PLUGIN directory, as agy sets it — and deliberately somewhere
    # that carries no identity of its own.
    elsewhere = tmp_path / "not-the-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test_key")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    return repo


def test_identity_comes_from_workspace_paths_not_cwd(repo, monkeypatch):
    """THE REGRESSION: cwd knows nothing; the payload knows the workspace."""
    assert _agent_seen({"workspacePaths": [str(repo)]}, monkeypatch) == "alpha"


def test_env_still_outranks_the_payload(repo, monkeypatch):
    """#90 precedence is unchanged: an explicit AGENTBUS_AGENT wins. Adding a
    fourth identity rule that quietly outranked the operator would be the
    split-identity bug all over again."""
    monkeypatch.setenv("AGENTBUS_AGENT", "operator-said-so")
    assert _agent_seen({"workspacePaths": [str(repo)]}, monkeypatch) == "operator-said-so"


def test_an_absent_workspace_falls_back_rather_than_guessing(repo, monkeypatch):
    """No workspacePaths and no identity under cwd ⇒ the kill switch fires."""
    assert _agent_seen({}, monkeypatch) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"workspacePaths": []},
        {"workspacePaths": ["relative/path"]},
        {"workspacePaths": ["/nonexistent/absolute/path"]},
        {"workspacePaths": [None]},
        {"workspacePaths": "not-a-list"},
    ],
)
def test_a_junk_workspace_is_ignored_not_trusted(repo, monkeypatch, bad):
    """A wrong workspace is worse than none: it would resolve identity against
    the wrong tree. Absent is honest."""
    assert _agent_seen(bad, monkeypatch) is None


def test_a_hostile_declared_agent_cannot_reach_the_operator_key(tmp_path, monkeypatch):
    """REG-8b, on the new call site.

    `.agentbus/agent` is attacker-controllable, and this lane feeds that name
    into credential adoption. A name that traverses out of the keys directory
    must not pick up `operator.env`, which can MINT a bound key for any agent.
    """
    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    (cfg / "operator.env").write_text("export AGENTBUS_API_KEY=ab_sk_OPERATOR_SECRET\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)

    assert _antigravity._with_credential("../operator") is False
    assert "OPERATOR_SECRET" not in (__import__("os").environ.get("AGENTBUS_API_KEY") or ""), (
        "a traversing agent name reached the operator credential"
    )


def test_a_legitimate_agent_does_get_its_key(tmp_path, monkeypatch):
    """KNOWN-POSITIVE TWIN. Without this, the traversal test above passes just
    as well against a function that can never adopt anything at all."""
    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    (cfg / "keys" / "alpha.env").write_text("export AGENTBUS_API_KEY=ab_sk_alpha_key\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)

    assert _antigravity._with_credential("alpha") is True
    assert __import__("os").environ["AGENTBUS_API_KEY"] == "ab_sk_alpha_key"
