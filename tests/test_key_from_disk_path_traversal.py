"""Path-traversal defense on _key_from_disk (REG-8, round-3 re-audit).

The identity resolver takes an `agent` name from three sources: the ctor arg,
$AGENTBUS_AGENT, and .agentbus/agent inside a checkout. The middle one is
attacker-controllable — a hostile workspace can commit `.agentbus/agent`
containing `../operator`. Without sanitization, os.path.join(config, 'keys',
'../operator.env') resolves to <config>/operator.env, so the client picks up
the operator credential when it thought it was picking up a bound agent key.

SEV-1-B closed the operator.env fallback branch. This test closes the
path-traversal side door into the SAME credential.
"""

from __future__ import annotations

import pathlib

import pytest

from agentbus_client import client as client_module


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME so the developer's real credentials cannot mask a bug."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    cfg = tmp_path / ".config" / "agentbus"
    (cfg / "keys").mkdir(parents=True)
    return cfg


def _write(path: pathlib.Path, key: str) -> None:
    path.write_text(f"export AGENTBUS_API_KEY={key}\n")


def test_dotdot_traversal_cannot_reach_operator_env(home):
    """A hostile .agentbus/agent containing '../operator' MUST NOT pivot onto
    the workspace-wide operator credential. Slug-sanitizing collapses `..` to
    `_`, so the resolved path is <config>/keys/__operator.env — which does not
    exist, so we get the empty-string "no key for this agent" answer."""
    _write(home / "operator.env", "ab_sk_OPERATOR_TOP_SECRET")

    # The attack: agent name attempts traversal.
    assert client_module._key_from_disk("../operator") == ""


def test_absolute_path_traversal_also_refused(home):
    """`/etc/passwd`-style names must not reach through either. The slug
    replaces every non-[A-Za-z0-9._-] character with underscore, so path
    separators cannot survive."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    assert client_module._key_from_disk("/etc/passwd") == ""
    assert client_module._key_from_disk("../../etc/passwd") == ""


def test_backslash_traversal_refused(home):
    """Windows-style separators too — on any OS `os.path.join` may honour them."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    assert client_module._key_from_disk("..\\operator") == ""


def test_slash_in_middle_of_name_refused(home):
    """`keys/../../foo` — separators anywhere in the name become underscores."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    assert client_module._key_from_disk("evil/../operator") == ""


def test_a_legitimate_agent_name_still_loads_its_bound_key(home):
    """The sanitizer MUST NOT change legitimate names. Agent names are
    already validated on the server (^[A-Za-z0-9_-]+$-ish), and the slug
    preserves that character set. If it stripped anything real, agents that
    signed in yesterday would silently start authenticating differently
    today, which is the exact class of bug SEV-1-B closed."""
    _write(home / "keys" / "agentbus-abc123.env", "ab_sk_BOUND")
    assert client_module._key_from_disk("agentbus-abc123") == "ab_sk_BOUND"


def test_traversal_via_agentbus_agent_env_also_blocked(home, monkeypatch):
    """Belt-and-braces: even if $AGENTBUS_AGENT is somehow set to a traversal
    payload (an env-var injection in a shared shell), the same sanitizer
    applies because _key_from_disk is called with the env-var value."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    monkeypatch.setenv("AGENTBUS_AGENT", "../operator")
    # The ctor path: agent=None triggers the env-var fallback in _Base.__init__.
    with pytest.raises(client_module.AuthError):
        client_module.AgentBus(base_url="https://x")
