"""Bound key on disk MUST win over a poisoned $AGENTBUS_API_KEY when the
caller names an agent (issuedb #16).

Root-caused by backend agentbus-8dc08d (thread 01M08QS3M10M49WKT8WVX3P2P7):
`resilient_circuit/storage.py` calls `load_dotenv()` at IMPORT time; its
`find_dotenv()` walks UP from the module's file inside
`.venv/lib/python3.13/site-packages/`, so any parent directory's `.env`
containing `AGENTBUS_API_KEY=<other-workspace's-key>` stomps
`os.environ["AGENTBUS_API_KEY"]`. If that stomped key is bound to a
deleted workspace, downstream calls see `WorkspaceDeleted` — even though
the correct freshly-minted bound key is sitting on disk.

Contract this file pins:
  * When `agent=NAME` is passed AND a bound key exists at
    ~/.config/agentbus/keys/NAME.env, the client uses THAT — env's
    AGENTBUS_API_KEY is IGNORED.
  * When `agent=None` (the operator-CLI path — signin, register), env
    still wins.
  * `api_key=...` explicit arg always wins (unchanged).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentbus_client.client import AgentBus


def _write_bound_key(home: Path, agent: str, secret: str) -> None:
    keys_dir = home / ".config" / "agentbus" / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    (keys_dir / f"{agent}.env").write_text(
        f"export AGENTBUS_API_KEY={secret}\nexport AGENTBUS_AGENT={agent}\n"
    )


def test_named_agent_prefers_disk_bound_key_over_stomped_env(tmp_path, monkeypatch):
    """The exact backend #243 shape: poisoned env, correct bound key on
    disk, agent=NAMED. The client MUST use the disk key."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_bound_key(tmp_path, "onboard-probe-abc123", "ab_sk_CORRECT_BOUND_KEY_ON_DISK")
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_POISONED_FROM_STOMPED_DOTENV")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    bus = AgentBus(agent="onboard-probe-abc123")
    assert bus.api_key == "ab_sk_CORRECT_BOUND_KEY_ON_DISK"


def test_unnamed_agent_still_uses_env_the_operator_cli_path(tmp_path, monkeypatch):
    """`agentbus signin`, `agentbus register --name X` etc. construct
    AgentBus() with NO agent= arg. Those must keep the historic
    env-wins-over-disk behaviour so an operator's shell-exported key
    keeps working."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_OPERATOR_ENV_KEY")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    bus = AgentBus()  # no agent named
    assert bus.api_key == "ab_sk_OPERATOR_ENV_KEY"


def test_explicit_api_key_argument_still_wins_over_everything(tmp_path, monkeypatch):
    """The `api_key=...` explicit constructor arg is the strongest signal;
    it must beat both disk and env."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_bound_key(tmp_path, "agent-x", "ab_sk_DISK_KEY")
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_ENV_KEY")

    bus = AgentBus(api_key="ab_sk_EXPLICIT_ARG", agent="agent-x")
    assert bus.api_key == "ab_sk_EXPLICIT_ARG"


def test_named_agent_with_no_disk_key_falls_back_to_env(tmp_path, monkeypatch):
    """If the caller names an agent BUT there is no bound key on disk for
    that agent, env is still consulted. The tightened rule is 'prefer disk
    if it's there', not 'refuse env when named' — the operator-CLI path
    for a freshly-registered agent must keep working.

    NOTE: the pre-existing AuthError also fires when NEITHER disk nor env
    yields a key; that older rule is not what this test is about. This
    test proves the FALLBACK PATH exists: an agent-named construction
    where disk is empty AND env has a key succeeds."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # no bound key file for this agent
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_ENV_FALLBACK")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    bus = AgentBus(agent="agent-with-no-disk-key")
    assert bus.api_key == "ab_sk_ENV_FALLBACK"


def test_named_agent_with_no_disk_and_no_env_still_raises_auth_error(tmp_path, monkeypatch):
    """The existing "no key for this agent" AuthError must still fire when
    neither disk nor env yields a credential."""
    from agentbus_client.client import AuthError

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    with pytest.raises(AuthError) as exc:
        AgentBus(agent="agent-nowhere")
    assert "no API key for agent 'agent-nowhere'" in str(exc.value)


def test_disk_wins_even_when_agentbus_agent_env_also_names_the_same_agent(tmp_path, monkeypatch):
    """A poisoned $AGENTBUS_API_KEY and a matching $AGENTBUS_AGENT together
    would previously make env win the resolution race. With the fix,
    calling AgentBus(agent=X) explicitly still prefers X's disk key."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_bound_key(tmp_path, "my-agent", "ab_sk_CORRECT_DISK_KEY_FOR_MY_AGENT")
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_POISONED_ENV_KEY")
    monkeypatch.setenv("AGENTBUS_AGENT", "my-agent")

    bus = AgentBus(agent="my-agent")
    assert bus.api_key == "ab_sk_CORRECT_DISK_KEY_FOR_MY_AGENT"
