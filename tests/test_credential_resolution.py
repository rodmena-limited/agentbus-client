"""#49: the credential resolution path, which decides WHOSE KEY a command uses.

This module sat at 21% coverage while carrying a privilege boundary. The
boundary is the one `runflow` reported with a reproduction: `--agent PEER` used
to feed straight into the key lookup, so asking to act as a peer silently loaded
THAT PEER'S bound key from keys/PEER.env. On a shared host,
`agentbus --agent mailapi whoami` answered as mailapi to a caller holding no
credential of their own.

The rule now: `--agent` is an ASSERTION OF IDENTITY, not an instruction to go
find that identity's credential. Acting as someone else is legitimate — with an
OPERATOR key, which may act as any agent by design — but never by borrowing
their bound one.

File permissions were never the control: every key file is 0600 under one UID,
so anyone who can run the CLI could already read them. What changed is that you
have to mean it.
"""

from __future__ import annotations

import pytest

from agentbus_client.onboarding import _credentials


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No ambient identity, no ambient keys — every case states its own world."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.setattr(_credentials, "_session_identity", lambda: None)
    monkeypatch.setattr(_credentials, "_agent_key", lambda name: None)
    monkeypatch.setattr(_credentials, "_operator_key", lambda: None)


def _with(monkeypatch, *, identity=None, keys=None, operator=None):
    monkeypatch.setattr(_credentials, "_session_identity", lambda: identity)
    monkeypatch.setattr(_credentials, "_agent_key", lambda name: (keys or {}).get(name))
    monkeypatch.setattr(_credentials, "_operator_key", lambda: operator)


def test_this_sessions_own_bound_key_is_loaded(monkeypatch):
    _with(monkeypatch, identity="me", keys={"me": "ab_sk_mine"})
    assert _credentials.resolve_credentials() == ("ab_sk_mine", "me")


def test_asking_to_act_as_a_PEER_never_loads_the_peers_bound_key(monkeypatch):
    """THE PRIVILEGE BOUNDARY. runflow's reproduction, pinned."""
    _with(monkeypatch, identity="me", keys={"me": "ab_sk_mine", "peer": "ab_sk_PEER"})
    key, agent = _credentials.resolve_credentials(preferred_agent="peer")
    assert key != "ab_sk_PEER", "borrowed a peer's bound credential"
    assert key is None and agent is None


def test_acting_as_a_peer_IS_allowed_with_an_operator_key(monkeypatch):
    """Known-negative for the boundary: it must not block the legitimate route.

    Without this, the assertion above would also pass in a world where acting as
    another agent were impossible altogether — which would make the operator
    credential useless rather than the boundary correct.
    """
    _with(monkeypatch, identity="me", keys={"peer": "ab_sk_PEER"}, operator="ab_sk_op")
    assert _credentials.resolve_credentials(preferred_agent="peer") == ("ab_sk_op", "peer")


def test_asking_for_your_OWN_name_explicitly_still_loads_your_key(monkeypatch):
    """`--agent me` when you ARE me is not impersonation."""
    _with(monkeypatch, identity="me", keys={"me": "ab_sk_mine"})
    assert _credentials.resolve_credentials(preferred_agent="me") == ("ab_sk_mine", "me")


def test_the_env_var_names_the_session_identity_when_nothing_else_does(monkeypatch):
    """SPECS/0038 + stabilize's container finding: in a fresh container with no
    project settings, no signin and no operator key, AGENTBUS_AGENT is the only
    record of who this session is — and its bound key must still auto-load."""
    monkeypatch.setenv("AGENTBUS_AGENT", "containerized")
    _with(monkeypatch, identity=None, keys={"containerized": "ab_sk_c"})
    assert _credentials.resolve_credentials() == ("ab_sk_c", "containerized")


def test_the_env_var_does_NOT_unlock_a_peers_key(monkeypatch):
    """The container path must not become a way around the boundary."""
    monkeypatch.setenv("AGENTBUS_AGENT", "me")
    _with(monkeypatch, identity=None, keys={"me": "ab_sk_mine", "peer": "ab_sk_PEER"})
    key, _ = _credentials.resolve_credentials(preferred_agent="peer")
    assert key != "ab_sk_PEER"


def test_the_operator_key_is_the_fallback_not_the_first_choice(monkeypatch):
    """Own bound key wins; the operator key is broader and should not be used
    when a narrower credential for this identity exists."""
    _with(monkeypatch, identity="me", keys={"me": "ab_sk_mine"}, operator="ab_sk_op")
    assert _credentials.resolve_credentials() == ("ab_sk_mine", "me")


def test_nothing_available_returns_nothing_rather_than_guessing(monkeypatch):
    _with(monkeypatch, identity=None)
    assert _credentials.resolve_credentials() == (None, None)


def test_an_identity_with_no_key_file_falls_through_to_the_operator(monkeypatch):
    _with(monkeypatch, identity="me", keys={}, operator="ab_sk_op")
    assert _credentials.resolve_credentials() == ("ab_sk_op", "me")
