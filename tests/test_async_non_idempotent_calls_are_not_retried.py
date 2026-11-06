"""#91: the async client repeats a failed call only when a repeat is safe.

The sync client has done this since #45. The async client retried a non-idempotent
POST on a 5xx up to four times with no Idempotency-Key, which is four chances to
half-create an identity. Each test runs the real client over a real httpx transport
and counts the requests that reached it, for both twins where the behaviour is shared.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agentbus_client.client import AgentBus, AsyncAgentBus, resilience as res
from agentbus_client.client.errors import AgentBusError, ServiceUnavailable, TransportError


@pytest.fixture(autouse=True)
def _fresh_breakers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res, "_ASYNC_CIRCUIT_BREAKER", None)
    monkeypatch.setattr(res, "_SDK_SAFETY_NET", None)

    async def _no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_wait)


class _Counter:
    def __init__(self, status: int) -> None:
        self.status = status
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(self.status, json={"code": "boom", "detail": "server failed"})


def _async_attempts(counter: _Counter, method: str, *, idempotent: bool = False) -> None:
    async def scenario() -> None:
        bus = AsyncAgentBus(api_key="ab_sk_x", base_url="http://bus.invalid")
        await bus._client.aclose()
        bus._client = httpx.AsyncClient(
            base_url="http://bus.invalid", transport=httpx.MockTransport(counter)
        )
        try:
            await bus._request(method, "/v1/agents/register", idempotent=idempotent)
        finally:
            await bus.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_non_idempotent_async_post_is_tried_once_on_a_5xx(status: int) -> None:
    counter = _Counter(status)
    with pytest.raises(AgentBusError) as caught:
        _async_attempts(counter, "POST")
    assert len(counter.seen) == 1
    assert caught.value.status == status
    assert "idempotency-key" not in counter.seen[0].headers


def test_a_caller_sees_the_original_error_not_a_wrapper() -> None:
    with pytest.raises(ServiceUnavailable):
        _async_attempts(_Counter(503), "POST")


def test_an_async_read_is_still_retried_so_resilience_is_not_switched_off() -> None:
    counter = _Counter(503)
    with pytest.raises(ServiceUnavailable):
        _async_attempts(counter, "GET")
    assert len(counter.seen) == 4


def test_an_async_post_with_an_idempotency_key_is_still_retried_on_the_same_key() -> None:
    counter = _Counter(503)
    with pytest.raises(ServiceUnavailable):
        _async_attempts(counter, "POST", idempotent=True)
    assert len(counter.seen) == 4
    keys = {request.headers["idempotency-key"] for request in counter.seen}
    assert len(keys) == 1


def test_an_async_4xx_is_never_retried_on_either_method() -> None:
    for method in ("GET", "POST"):
        counter = _Counter(404)
        with pytest.raises(AgentBusError):
            _async_attempts(counter, method)
        assert len(counter.seen) == 1


def test_a_refused_async_post_still_counts_against_the_breaker() -> None:
    for _ in range(5):
        with pytest.raises(AgentBusError):
            _async_attempts(_Counter(500), "POST")
    counter = _Counter(500)
    with pytest.raises(TransportError, match="circuit breaker is OPEN"):
        _async_attempts(counter, "POST")
    assert counter.seen == []


def test_the_twins_make_the_same_number_of_attempts_on_a_non_idempotent_5xx() -> None:
    sync_counter = _Counter(500)
    bus = AgentBus(api_key="ab_sk_x", base_url="http://bus.invalid")
    bus._client = httpx.Client(
        base_url="http://bus.invalid", transport=httpx.MockTransport(sync_counter)
    )
    with pytest.raises(AgentBusError):
        bus._request("POST", "/v1/agents/register")
    async_counter = _Counter(500)
    with pytest.raises(AgentBusError):
        _async_attempts(async_counter, "POST")
    assert len(sync_counter.seen) == len(async_counter.seen) == 1


def test_register_through_the_public_async_surface_makes_one_attempt() -> None:
    counter = _Counter(500)

    async def scenario() -> None:
        bus = AsyncAgentBus(api_key="ab_sk_x", base_url="http://bus.invalid")
        await bus._client.aclose()
        bus._client = httpx.AsyncClient(
            base_url="http://bus.invalid", transport=httpx.MockTransport(counter)
        )
        try:
            await bus.register("double-check")
        finally:
            await bus.aclose()

    with pytest.raises(AgentBusError):
        asyncio.run(scenario())
    assert len(counter.seen) == 1
