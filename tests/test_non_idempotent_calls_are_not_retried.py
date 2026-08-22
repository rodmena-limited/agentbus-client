"""#45: a 5xx must not be retried when repeating the call is unsafe.

OBSERVED IN PRODUCTION, 2026-08-22: a single `AgentBus().register(name, role=...)`
produced FOUR 500 responses with four distinct server error ids before raising.
The caller saw one exception; the server saw four attempts to create an identity.

A 5xx says THE SERVER FAILED, not that it did nothing. If the failure lands after
a partial write, four attempts are four chances to half-create — and for
`register` the object being created is an IDENTITY, which is the one thing on
this bus that must never be silently duplicated. A split identity is exactly the
defect class that cost several agents an hour the same night (#40, #42, #44).

The rule is not "never retry a POST". It is "repeat only when a repeat is safe":
GET/HEAD/OPTIONS by definition, and a mutating call only when the client minted
an idempotency key for it, which makes the server's dedup layer see one write.
"""

from __future__ import annotations

import pytest

from agentbus_client.client import AgentBus
from agentbus_client.client.errors import AgentBusError


def _client_counting_attempts(status: int):
    bus = AgentBus(api_key="ab_sk_x", base_url="http://localhost")
    calls: list[str] = []

    def _boom(*_a, **_k):
        calls.append("attempt")
        raise AgentBusError("server exploded", code="internal_error", status=status)

    return bus, calls, _boom


def test_a_mutating_call_without_an_idempotency_key_is_tried_once(monkeypatch):
    """THE REGRESSION. Four attempts is four chances to half-create an identity."""
    bus, calls, boom = _client_counting_attempts(500)
    monkeypatch.setattr(bus, "_send_once", boom, raising=False)
    monkeypatch.setattr("agentbus_client.client.sync_client._raise_for", lambda r: boom())
    monkeypatch.setattr(bus._client, "request", lambda *a, **k: object())
    with pytest.raises(AgentBusError):
        bus._request("POST", "/v1/agents/register", json={"name": "x"})
    assert len(calls) == 1, f"a non-idempotent POST was attempted {len(calls)} times"


def test_a_read_is_still_retried_so_the_fix_did_not_disable_resilience(monkeypatch):
    """KNOWN-NEGATIVE, and the one that matters.

    Without this, "attempted once" would pass in a world where retry had been
    switched off entirely — turning a resilience feature into a silent no-op
    while the first test still went green.
    """
    bus, calls, boom = _client_counting_attempts(500)
    monkeypatch.setattr("agentbus_client.client.sync_client._raise_for", lambda r: boom())
    monkeypatch.setattr(bus._client, "request", lambda *a, **k: object())
    with pytest.raises(AgentBusError):
        bus._request("GET", "/v1/inbox")
    assert len(calls) > 1, "a GET is safe to repeat and must still be retried"


def test_a_4xx_is_never_retried_on_either_method(monkeypatch):
    """A definitive refusal is not transient; repeating it only wastes quota."""
    for method, path in (("GET", "/v1/inbox"), ("POST", "/v1/agents/register")):
        bus, calls, boom = _client_counting_attempts(403)
        # bind `boom` explicitly: a late-binding closure over the loop
        # variable would test the LAST iteration three times.
        monkeypatch.setattr(
            "agentbus_client.client.sync_client._raise_for",
            lambda r, _b=boom: _b(),
        )
        monkeypatch.setattr(bus._client, "request", lambda *a, **k: object())
        with pytest.raises(AgentBusError):
            bus._request(method, path)
        assert len(calls) == 1, f"{method} retried a 403"
