"""SDK resilience wrapper — retry+breaker+bulkhead around _Base._request (REG-7).

Round-2 shipped bare httpx calls; round-3 wrapped them. This test does two things:
1. TRANSIENT errors (TransportError, ServiceUnavailable, httpx.HTTPError) MUST
   retry — a rolling deploy of the bus that returns 503s for a few seconds
   used to fail every send with TransportError; now the SDK absorbs it.
2. NON-TRANSIENT errors (AuthError, PermissionError_, NotFoundError,
   ValidationError, QuotaExceeded, RateLimited) MUST pass through unchanged
   on the FIRST attempt — retrying a 401 hammers the bus with a credential
   that will never work and turns one clear failure into many.

Extra: the classifier itself is exercised as pure logic so the sync path's
should_handle rule is guaranteed correct regardless of the wrapper's plumbing.
"""

from __future__ import annotations

import pytest

from agentbus_client import client as client_module
from agentbus_client.client import (
    AgentBus,
    AuthError,
    NotFoundError,
    QuotaExceeded,
    ServiceUnavailable,
    TransportError,
    ValidationError,
    _is_transient_sdk_error,
)


# ---------------------------------------------------------------- classifier
def test_transient_errors_classify_as_transient() -> None:
    assert _is_transient_sdk_error(TransportError("bus unreachable"))
    assert _is_transient_sdk_error(ServiceUnavailable("503"))
    # 5xx that the server maps to a BARE AgentBusError (500/502/504 surface
    # via a proxy/gateway mid-deploy; only 503 becomes ServiceUnavailable).
    # These MUST retry or a deploy fails every in-flight call definitively.
    for status in (500, 502, 504):
        assert _is_transient_sdk_error(client_module.AgentBusError("gateway", status=status)), (
            f"bare AgentBusError status {status} MUST be transient"
        )


def test_non_transient_errors_classify_as_definitive() -> None:
    for exc in (
        AuthError("revoked"),
        NotFoundError("no such"),
        ValidationError("malformed"),
        QuotaExceeded("over", body={}),
    ):
        assert not _is_transient_sdk_error(exc), f"{type(exc).__name__} MUST NOT retry"


def test_httpx_transport_level_error_classifies_as_transient() -> None:
    import httpx

    assert _is_transient_sdk_error(httpx.ConnectError("network"))
    assert _is_transient_sdk_error(httpx.ReadTimeout("slow"))


def test_sync_and_async_breakers_share_cb_env_knobs(monkeypatch) -> None:
    """A1: AGENTBUS_SDK_CB_FAILURE_LIMIT / _SUCCESS_LIMIT used to be honored
    only by the async breaker; the sync breaker hardcoded 5/5 and 2/2. One
    helper must feed both, so one operator setting tunes both surfaces."""
    from agentbus_client.client import resilience as resilience_module

    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "7")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "3")
    failure, success = resilience_module._sdk_cb_limits()
    assert (failure, success) == (7, 3)

    # Defaults still sane when unset.
    monkeypatch.delenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", raising=False)
    monkeypatch.delenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", raising=False)
    assert resilience_module._sdk_cb_limits() == (5, 2)


# ---------------------------------------------------------------- retry behavior
class _StubClient:
    """Minimal httpx.Client stand-in — records attempts, raises on schedule."""

    def __init__(self, script: list) -> None:
        # `script` is a list of either an exception to raise or a (status, body)
        # tuple to return as a mock response.
        self.script = list(script)
        self.calls = 0

    def request(self, *_a, **_k):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        status, body = step
        return _StubResp(status, body)

    def close(self) -> None:  # pragma: no cover
        pass


class _StubResp:
    def __init__(self, status: int, body: bytes | dict) -> None:
        self.status_code = status
        if isinstance(body, dict):
            import json

            self.content = json.dumps(body).encode()
            self._body = body
        else:
            self.content = body
            self._body = None
        self.text = self.content.decode() if isinstance(self.content, bytes) else ""

    def json(self):
        return self._body


def _make_bus(client: _StubClient, monkeypatch) -> AgentBus:
    """Build an AgentBus with the stub as its _client."""
    bus = AgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="test-agent")
    monkeypatch.setattr(bus, "_client", client)
    # Reset the module-level bulkhead + safety-net across tests so the
    # breaker's cross-test state does not leak. Setting to None re-lazies.
    monkeypatch.setattr(client_module.resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_module.resilience, "_SDK_SAFETY_NET", None)
    return bus


def test_transient_transport_error_retries_and_eventually_succeeds(monkeypatch) -> None:
    """503 -> 503 -> 200. The caller sees success; three attempts on the wire."""
    import httpx

    client = _StubClient(
        [
            httpx.ConnectError("network"),
            httpx.ConnectError("still network"),
            (200, {"ok": True}),
        ]
    )
    bus = _make_bus(client, monkeypatch)
    result = bus._request("GET", "/v1/whoami")
    assert result == {"ok": True}
    assert client.calls == 3


def test_gateway_502_retries_and_eventually_succeeds(monkeypatch) -> None:
    """502 -> 502 -> 200. A bare AgentBusError 502 (proxy mid-deploy) used to
    be treated as DEFINITIVE and passed through on the first attempt — the
    same transient-classification gap the audit found in rewake.py, one layer
    over. Now it retries with backoff like a 503."""
    client = _StubClient(
        [
            (502, {"code": "bad_gateway", "detail": "upstream raced"}),
            (502, {"code": "bad_gateway", "detail": "upstream raced"}),
            (200, {"ok": True}),
        ]
    )
    bus = _make_bus(client, monkeypatch)
    result = bus._request("GET", "/v1/whoami")
    assert result == {"ok": True}
    assert client.calls == 3


def test_401_passes_through_immediately(monkeypatch) -> None:
    """A revoked key returns AuthError on the FIRST attempt. Retrying a 401
    hammers the bus with a credential that will never work."""
    client = _StubClient([(401, {"code": "invalid_api_key", "detail": "no"})])
    bus = _make_bus(client, monkeypatch)
    with pytest.raises(AuthError):
        bus._request("GET", "/v1/whoami")
    assert client.calls == 1, "AuthError MUST NOT be retried"


def test_404_passes_through_immediately(monkeypatch) -> None:
    client = _StubClient([(404, {"code": "not_found", "detail": "gone"})])
    bus = _make_bus(client, monkeypatch)
    with pytest.raises(NotFoundError):
        bus._request("GET", "/v1/deliveries/missing")
    assert client.calls == 1


def test_429_passes_through_immediately(monkeypatch) -> None:
    """Quota exceeded is a definitive answer — retrying hammers the vendor."""
    client = _StubClient([(429, {"code": "quota_exceeded", "retry_after": 60})])
    bus = _make_bus(client, monkeypatch)
    with pytest.raises(QuotaExceeded):
        bus._request("POST", "/v1/messages")
    assert client.calls == 1


def test_persistent_transport_errors_eventually_raise(monkeypatch) -> None:
    """After max_retries transient errors, the SDK gives up — but with the
    ORIGINAL TransportError, not a library-specific type."""
    import httpx

    # Default max_retries=3, so 4 attempts total.
    client = _StubClient([httpx.ConnectError("network")] * 10)
    bus = _make_bus(client, monkeypatch)
    with pytest.raises(TransportError):
        bus._request("GET", "/v1/whoami")
    assert client.calls == 4  # 1 initial + 3 retries


def test_resilience_opt_out(monkeypatch) -> None:
    """A caller with its own resilience layer around the SDK can opt out via
    env — the wrapper becomes a passthrough, so retries stop doubling."""
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "0")
    import httpx

    client = _StubClient([httpx.ConnectError("network")])
    bus = _make_bus(client, monkeypatch)
    with pytest.raises(TransportError):
        bus._request("GET", "/v1/whoami")
    # Exactly ONE attempt, no retry.
    assert client.calls == 1


def test_idempotency_key_is_stable_across_retries(monkeypatch) -> None:
    """The mint-once-outside-the-loop rule: the vendor's dedup layer must see
    a retry, not two distinct writes."""
    import httpx

    keys_seen: list[str | None] = []

    class _CapturingClient(_StubClient):
        def request(self, *args, **kwargs):
            keys_seen.append(kwargs.get("headers", {}).get("Idempotency-Key"))
            return super().request(*args, **kwargs)

    client = _CapturingClient(
        [
            httpx.ConnectError("network"),
            (200, {"ok": True}),
        ]
    )
    bus = _make_bus(client, monkeypatch)
    bus._request("POST", "/v1/messages", idempotent=True)
    assert len(keys_seen) == 2, "expected 2 attempts (1 retry)"
    assert keys_seen[0] and keys_seen[0] == keys_seen[1], (
        "the same Idempotency-Key must be sent on retry — otherwise the "
        "vendor cannot dedup and a caller who wraps in try/retry duplicates."
    )
