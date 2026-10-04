from __future__ import annotations

import pytest

from agentbus_client.client import AgentBus, resilience
from agentbus_client.client.errors import AgentBusError, TransportError


def _bus_failing_with(monkeypatch: pytest.MonkeyPatch, status: int) -> tuple[AgentBus, list[str]]:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "3")
    bus = AgentBus(api_key="ab_sk_x", base_url="http://localhost")
    calls: list[str] = []

    def _boom(*_a: object, **_k: object) -> None:
        calls.append("attempt")
        raise AgentBusError("server exploded", code="internal_error", status=status)

    monkeypatch.setattr("agentbus_client.client.sync_client._raise_for", lambda r: _boom())
    monkeypatch.setattr(bus._client, "request", lambda *a, **k: object())
    return bus, calls


def _outcomes(bus: AgentBus, method: str, path: str, n: int) -> list[str]:
    seen = []
    for _ in range(n):
        try:
            bus._request(method, path, json={"name": "x"} if method == "POST" else None)
        except TransportError as exc:
            seen.append("breaker-open" if "circuit breaker is OPEN" in str(exc) else "transport")
        except AgentBusError as exc:
            seen.append(f"http-{exc.status}")
    return seen


def test_non_idempotent_5xx_opens_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, calls = _bus_failing_with(monkeypatch, 500)
    seen = _outcomes(bus, "POST", "/v1/agents/register", 6)
    assert seen[:3] == ["http-500"] * 3, seen
    assert "breaker-open" in seen[3:], seen
    assert len(calls) < 6


def test_non_idempotent_5xx_is_still_attempted_once_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, calls = _bus_failing_with(monkeypatch, 500)
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "3")
    _outcomes(bus, "POST", "/v1/agents/register", 1)
    assert calls == ["attempt"]


def test_non_idempotent_4xx_never_opens_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, calls = _bus_failing_with(monkeypatch, 409)
    seen = _outcomes(bus, "POST", "/v1/agents/register", 6)
    assert seen == ["http-409"] * 6, seen
    assert len(calls) == 6
