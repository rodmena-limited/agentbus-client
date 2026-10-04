from __future__ import annotations

import asyncio
import time

import pytest

from agentbus_client.client import resilience
from agentbus_client.client.async_client import AsyncAgentBus
from agentbus_client.client.errors import TransportError
from agentbus_client.client.resilience import _AsyncCircuitBreaker


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_ASYNC_CIRCUIT_BREAKER", None)
    for name in (
        "AGENTBUS_SDK_CB_COOLDOWN",
        "AGENTBUS_SDK_CB_FAILURE_LIMIT",
        "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
        "AGENTBUS_SDK_MAX_RETRIES",
        "AGENTBUS_SDK_RESILIENCE",
    ):
        monkeypatch.delenv(name, raising=False)


def _trip(breaker: _AsyncCircuitBreaker) -> None:
    for _ in range(breaker.failure_limit):
        breaker.on_failure(TransportError("down"))


def _half_open(breaker: _AsyncCircuitBreaker) -> None:
    _trip(breaker)
    assert breaker.is_open() is True
    time.sleep(breaker.cooldown + 0.05)
    assert breaker.is_open() is False


def test_singleton_defaults() -> None:
    breaker = resilience._async_circuit_breaker()
    assert breaker.failure_limit == 5
    assert breaker.success_limit == 2
    assert breaker.cooldown == 30.0
    assert resilience._async_circuit_breaker() is breaker


def test_singleton_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "4")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "3")
    monkeypatch.setenv("AGENTBUS_SDK_CB_COOLDOWN", "17")
    breaker = resilience._async_circuit_breaker()
    assert breaker.failure_limit == 4
    assert breaker.success_limit == 3
    assert breaker.cooldown == 17.0


def test_singleton_failure_path_opens_and_names_the_error() -> None:
    breaker = resilience._async_circuit_breaker()
    errors = [TransportError(f"down {i}") for i in range(5)]
    for exc in errors[:4]:
        breaker.on_failure(exc)
    assert breaker.is_open() is False
    breaker.on_failure(errors[4])
    assert breaker.is_open() is True
    assert breaker.admit() is False
    assert breaker.last_error() is errors[4]


def test_fresh_breaker_is_closed_and_admits_many() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=3, success_limit=2, cooldown=0.1)
    assert breaker.is_open() is False
    assert [breaker.admit() for _ in range(4)] == [True, True, True, True]
    assert breaker.last_error() is None


def test_opens_exactly_at_the_failure_limit() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=3, success_limit=2, cooldown=5.0)
    breaker.on_failure(TransportError("1"))
    breaker.on_failure(TransportError("2"))
    assert breaker.is_open() is False
    assert breaker.admit() is True
    breaker.on_failure(TransportError("3"))
    assert breaker.is_open() is True
    assert breaker.admit() is False


def test_a_success_resets_the_consecutive_failure_count() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=3, success_limit=2, cooldown=5.0)
    breaker.on_failure(TransportError("1"))
    breaker.on_failure(TransportError("2"))
    breaker.on_success()
    breaker.on_failure(TransportError("3"))
    breaker.on_failure(TransportError("4"))
    assert breaker.is_open() is False
    breaker.on_failure(TransportError("5"))
    assert breaker.is_open() is True


def test_half_open_admits_exactly_one_probe_at_a_time() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=2, success_limit=2, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    assert breaker.admit() is False
    assert breaker.admit() is False


def test_success_limit_probes_are_needed_to_close() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=2, success_limit=2, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    breaker.on_success()
    assert breaker.admit() is True
    assert breaker.admit() is False
    breaker.on_success()
    assert [breaker.admit() for _ in range(3)] == [True, True, True]


def test_after_closing_one_failure_does_not_reopen() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=2, success_limit=1, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    breaker.on_success()
    breaker.on_failure(TransportError("one"))
    assert breaker.is_open() is False
    assert [breaker.admit() for _ in range(2)] == [True, True]
    breaker.on_failure(TransportError("two"))
    assert breaker.is_open() is True


def test_a_failed_probe_reopens_immediately() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=3, success_limit=2, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    probe_error = TransportError("probe failed")
    breaker.on_failure(probe_error)
    assert breaker.is_open() is True
    assert breaker.admit() is False
    assert breaker.last_error() is probe_error


def test_a_failed_probe_discards_earlier_probe_successes() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=2, success_limit=2, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    breaker.on_success()
    assert breaker.admit() is True
    breaker.on_failure(TransportError("probe failed"))
    time.sleep(0.15)
    assert breaker.admit() is True
    breaker.on_success()
    assert breaker.admit() is True
    assert breaker.admit() is False
    breaker.on_success()
    assert [breaker.admit() for _ in range(2)] == [True, True]


def test_release_probe_frees_the_slot() -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=2, success_limit=2, cooldown=0.1)
    _half_open(breaker)
    assert breaker.admit() is True
    assert breaker.admit() is False
    breaker.release_probe()
    assert breaker.admit() is True
    assert breaker.admit() is False


def _bus_with(monkeypatch: pytest.MonkeyPatch, request) -> AsyncAgentBus:
    bus = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")

    class _Stub:
        async def request(self, *a, **k):
            return await request(*a, **k)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(bus, "_client", _Stub())
    return bus


class _Ok:
    status_code = 200
    content = b'{"ok": true}'
    text = '{"ok": true}'

    def json(self) -> dict:
        return {"ok": True}


def test_cancelled_probe_through_the_client_does_not_wedge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    breaker = _AsyncCircuitBreaker(failure_limit=1, success_limit=1, cooldown=0.1)
    monkeypatch.setattr(resilience, "_ASYNC_CIRCUIT_BREAKER", breaker)
    gate = {"hang": True, "calls": 0}

    async def _request(*_a, **_k):
        gate["calls"] += 1
        if gate["hang"]:
            await asyncio.sleep(30)
        return _Ok()

    bus = _bus_with(monkeypatch, _request)
    _half_open(breaker)

    async def _scenario() -> object:
        task = asyncio.ensure_future(bus._request("GET", "/v1/whoami"))
        await asyncio.sleep(0.05)
        assert breaker._probe_inflight is True
        with pytest.raises(TransportError):
            await bus._request("GET", "/v1/whoami")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker._probe_inflight is False
        gate["hang"] = False
        return await bus._request("GET", "/v1/whoami")

    assert asyncio.run(_scenario()) == {"ok": True}
    assert gate["calls"] == 2
    assert breaker._half_open is False


def test_open_async_breaker_fails_fast_through_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "2")
    calls: list[int] = []

    async def _request(*_a, **_k):
        calls.append(1)
        raise httpx.ConnectError("down")

    bus = _bus_with(monkeypatch, _request)

    async def _scenario() -> list[str]:
        seen = []
        for _ in range(4):
            try:
                await bus._request("GET", "/v1/whoami")
            except TransportError as exc:
                seen.append("open" if "circuit breaker is OPEN" in str(exc) else "transport")
        return seen

    assert asyncio.run(_scenario()) == ["transport", "transport", "open", "open"]
    assert calls == [1, 1]
