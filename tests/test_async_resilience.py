"""Async client resilience: circuit breaker + outer deadline (ticket #18).

The async `_run_with_resilience_async` added a circuit breaker and an outer
wall-clock deadline to close the gap with the sync `_run_with_resilience`
(which has resilient_circuit's breaker and a `future.result(timeout=+5)`
bound). Before this, a sustained outage left every async call retrying at full
concurrency with no memory of prior failures and no cap on how long the retry
sequence ran.

The breaker is a module-level singleton (like the sync `_SDK_SAFETY_NET`), so
each test installs a FRESH controlled breaker via monkeypatch and resets the
real singleton afterward — otherwise a tripped breaker would leak into the
other async tests in the suite.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.client import errors as client_errors
from agentbus_client.client import resilience as client_resilience
from agentbus_client.client.async_client import AsyncAgentBus
from agentbus_client.client.resilience import _AsyncCircuitBreaker


@pytest.fixture(autouse=True)
def _isolate_breaker(monkeypatch):
    """Give every test a fresh, controlled breaker; restore the real singleton."""
    fresh = _AsyncCircuitBreaker()
    # Patch the global singleton so _async_circuit_breaker() returns it
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)
    return fresh


def _stub_client(calls: list):
    """A stub httpx.AsyncClient whose request appends to `calls` and succeeds."""

    class _Stub:
        def __init__(self):
            self.calls = calls

        async def request(self, *_a, **_k):
            self.calls.append(1)

            class _R:
                status_code = 200
                content = b'{"ok": true}'
                text = '{"ok": true}'

                def json(self):
                    return {"ok": True}

            return _R()

        async def aclose(self):  # pragma: no cover
            pass

    return _Stub()


@pytest.fixture
def bus(monkeypatch):
    """An AsyncAgentBus whose transport is a controllable stub."""
    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    monkeypatch.setattr(b, "_client", _stub_client([]))
    monkeypatch.setattr(client_resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "3")
    return b


# ---------------------------------------------------------------- breaker opens


def test_breaker_opens_then_calls_fail_fast(monkeypatch):
    """After `failure_limit` consecutive failing retry-sequences the breaker
    opens; subsequent calls fail fast with the last error and attempt no retry
    and no new transport call."""
    calls: list = []

    class _Flaky:
        def __init__(self):
            self.calls = calls

        async def request(self, *_a, **_k):
            self.calls.append(1)
            raise httpx.ConnectError("bus down")

        async def aclose(self):  # pragma: no cover
            pass

    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    monkeypatch.setattr(b, "_client", _Flaky())
    monkeypatch.setattr(client_resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "3")  # 1 call + 3 retries = 4 attempts

    fresh = _AsyncCircuitBreaker(failure_limit=3, success_limit=2, cooldown=60.0)
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)

    # Each _request runs 1 real call + 3 retries = 4 transport attempts before
    # exhausting, and records ONE failing sequence against the breaker. So 3
    # requests trip it (3 failing sequences >= failure_limit 3). The underlying
    # httpx.ConnectError is wrapped as the SDK TransportError by _do_request.
    for _ in range(3):
        with pytest.raises(client_errors.TransportError):
            asyncio.run(b._request("GET", "/v1/whoami"))
    assert fresh.is_open(), "breaker did not open after 3 failing sequences"

    attempts_so_far = len(calls)
    # Now the breaker is open — the next call fails fast: NO new transport
    # attempt at all, still surfacing the last (wrapped) error.
    with pytest.raises(client_errors.TransportError):
        asyncio.run(b._request("GET", "/v1/whoami"))
    assert len(calls) == attempts_so_far, "open breaker still hit the transport"
    assert fresh.is_open()


# ---------------------------------------------------------------- breaker recovers


def test_breaker_closes_after_clean_successes(monkeypatch):
    """Once the cooldown lapses, a successful call (half-open probe) closes the
    breaker, and subsequent calls proceed normally."""
    state = {"fail": True, "calls": 0}

    class _Flip:
        def __init__(self):
            self.state = state

        async def request(self, *_a, **_k):
            self.state["calls"] += 1
            if self.state["fail"]:
                raise httpx.ConnectError("bus down")

            class _R:
                status_code = 200
                content = b'{"ok": true}'
                text = '{"ok": true}'

                def json(self):
                    return {"ok": True}

            return _R()

        async def aclose(self):  # pragma: no cover
            pass

    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    monkeypatch.setattr(b, "_client", _Flip())
    monkeypatch.setattr(client_resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")  # 1 attempt, no retry

    # Open the breaker quickly: failure_limit=1 trips it after ONE sequence.
    fresh = _AsyncCircuitBreaker(failure_limit=1, success_limit=1, cooldown=0.05)
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)

    with pytest.raises(client_errors.TransportError):
        asyncio.run(b._request("GET", "/v1/whoami"))
    assert fresh.is_open()

    # While open, a call fails fast with no transport attempt (still the SDK
    # TransportError; ConnectError was wrapped by _do_request).
    before = state["calls"]
    with pytest.raises(client_errors.TransportError):
        asyncio.run(b._request("GET", "/v1/whoami"))
    assert state["calls"] == before

    # After the cooldown lapses the probe goes through; the bus is back up, so
    # the call succeeds and the breaker closes.
    time.sleep(0.06)
    state["fail"] = False
    result = asyncio.run(b._request("GET", "/v1/whoami"))
    assert result == {"ok": True}
    assert not fresh.is_open(), "breaker did not close after a clean success"

    # And normal traffic flows.
    result = asyncio.run(b._request("GET", "/v1/whoami"))
    assert result == {"ok": True}


# ---------------------------------------------------- sustained outage re-opens


def test_half_open_probe_failure_reopens_immediately(monkeypatch):
    """After the cooldown lapses, a FAILED probe must re-open the breaker on the
    FIRST failure (not failure_limit more) — a sustained outage costs one probe
    burst per cooldown, not N. This is the release-vs-block durability half: it
    must keep refusing while the bus stays down."""
    state = {"calls": 0}

    class _Down:
        def __init__(self):
            self.state = state

        async def request(self, *_a, **_k):
            self.state["calls"] += 1
            raise httpx.ConnectError("still down")

        async def aclose(self):  # pragma: no cover
            pass

    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    monkeypatch.setattr(b, "_client", _Down())
    monkeypatch.setattr(client_resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")  # 1 attempt, no retry

    # failure_limit=5 to isolate: a half-open probe failure must re-open even
    # though only ONE post-cooldown failure has occurred.
    fresh = _AsyncCircuitBreaker(failure_limit=5, success_limit=2, cooldown=0.05)
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)

    # Prime open: 5 failing sequences (failure_limit=5 -> opens).
    for _ in range(5):
        with pytest.raises(client_errors.TransportError):
            asyncio.run(b._request("GET", "/v1/whoami"))
    assert fresh.is_open()

    # Wait for the cooldown to lapse -> half-open. The probe FAILS and must
    # re-open immediately (only 1 post-cooldown failure, well under limit 5).
    time.sleep(0.06)
    assert not fresh.is_open(), "cooldown did not lapse into half-open"
    with pytest.raises(client_errors.TransportError):
        asyncio.run(b._request("GET", "/v1/whoami"))
    # A single half-open failure re-opened it — not 5.
    assert fresh.is_open(), "half-open probe failure did not re-open the breaker"


# ---------------------------------------------------------------- outer deadline


def test_outer_deadline_surfaces_transport_error():
    """A retry sequence that exceeds the deadline must raise TransportError
    (mirroring the sync future.result(timeout=) translation), not hang."""

    async def hang():
        await asyncio.sleep(0.5)
        raise AssertionError("should have been cut off by the deadline")

    start = time.monotonic()
    with pytest.raises(client_errors.TransportError) as exc:
        asyncio.run(_call_with_deadline(hang, deadline=0.05))
    elapsed = time.monotonic() - start
    assert elapsed < 0.4, "deadline did not cut off the retry sequence"
    assert "did not complete within" in str(exc.value)


async def _call_with_deadline(fn, *, deadline):
    """Drive _run_with_resilience_async with a synthetic deadline (no bus)."""
    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    return await b._run_with_resilience_async(fn, deadline=deadline)


# ---------------------------------------------------------- disable still works


def test_resilience_disable_skips_breaker_and_deadline(monkeypatch):
    """AGENTBUS_SDK_RESILIENCE=0 must bypass the resilience layer entirely — a
    slow call is awaited directly (no deadline cut, no breaker engagement)."""
    slow = {"calls": 0}

    class _Slow:
        def __init__(self):
            self.slow = slow

        async def request(self, *_a, **_k):
            self.slow["calls"] += 1
            await asyncio.sleep(0.2)  # slower than any deadline we'd use

            class _R:
                status_code = 200
                content = b'{"ok": true}'
                text = '{"ok": true}'

                def json(self):
                    return {"ok": True}

            return _R()

        async def aclose(self):  # pragma: no cover
            pass

    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    monkeypatch.setattr(b, "_client", _Slow())
    monkeypatch.setattr(client_resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "0")

    fresh = _AsyncCircuitBreaker(failure_limit=1, success_limit=1, cooldown=60.0)
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)
    # A tiny self.timeout so call_timeout+5 would be small IF the deadline were
    # engaged — but with resilience off it must be ignored.
    b.timeout = 0.01

    # The slow request is awaited directly and completes — with the deadline
    # engaged it would have been cut off as TransportError.
    result = asyncio.run(b._request("GET", "/v1/whoami", timeout=0.01))
    assert result == {"ok": True}
    assert not fresh.is_open(), "breaker must not engage when resilience is off"


# -------------------------------------------------------- non-transient bypasses


def test_non_transient_error_passes_through_without_tripping_breaker(bus, monkeypatch):
    """A definitive (non-transient) error passes through immediately and does
    NOT count toward the breaker — same as the sync should_handle classifier."""

    class _Deny:
        def __init__(self):
            self.calls = 0

        async def request(self, *_a, **_k):
            self.calls += 1
            raise client_errors.AuthError("revoked")

        async def aclose(self):  # pragma: no cover
            pass

    b = bus
    monkeypatch.setattr(b, "_client", _Deny())
    fresh = _AsyncCircuitBreaker(failure_limit=1, success_limit=1, cooldown=60.0)
    monkeypatch.setattr(client_resilience, "_ASYNC_CIRCUIT_BREAKER", fresh)

    with pytest.raises(client_errors.AuthError):
        asyncio.run(b._request("GET", "/v1/whoami"))
    assert not fresh.is_open(), "a 4xx/definitive error must not trip the breaker"
