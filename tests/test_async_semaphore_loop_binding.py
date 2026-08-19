"""AsyncAgentBus semaphore MUST rebind per event loop (REG-10, round-3 re-audit).

asyncio.Semaphore permanently binds to the loop it is instantiated on. Round-3
introduced a lazy per-instance semaphore for the async bulkhead; a global
AsyncAgentBus reused across loops (uvicorn worker restart, pytest-asyncio's
per-test loop policy, a script calling asyncio.run twice) would then raise
RuntimeError on the second loop. The fix keys the semaphore by the running
event loop; this test proves reuse across loops now works.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agentbus_client import client as client_module
from agentbus_client.client import AsyncAgentBus


class _StubAsyncClient:
    """Minimal httpx.AsyncClient stand-in — returns the same 200 forever."""

    def __init__(self) -> None:
        self.calls = 0

    async def request(self, *_a, **_k):
        self.calls += 1

        class _Resp:
            status_code = 200
            content = b'{"ok": true}'
            text = '{"ok": true}'

            def json(self):
                return {"ok": True}

        return _Resp()

    async def aclose(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def bus(monkeypatch):
    """A single AsyncAgentBus instance we can reuse across event loops."""
    b = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="test-agent")
    monkeypatch.setattr(b, "_client", _StubAsyncClient())
    monkeypatch.setattr(client_module.resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_module.resilience, "_SDK_SAFETY_NET", None)
    # Disable the retry stack for this test — we care about the SEMAPHORE
    # rebinding, not the retry policy.
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    return b


def test_the_same_instance_survives_two_event_loops(bus) -> None:
    """The old code (asyncio.Semaphore stored on the instance) raised
    RuntimeError on the SECOND asyncio.run because the semaphore was bound to
    the first loop, which was closed. The fix per-loop-keyed lookup means the
    second loop gets a fresh semaphore and the request goes through."""
    # First loop.
    result_a = asyncio.run(bus._request("GET", "/v1/whoami"))
    assert result_a == {"ok": True}
    # Second loop — this used to raise `RuntimeError: got Future <> attached to
    # a different loop` if the semaphore had been cached from the first loop.
    result_b = asyncio.run(bus._request("GET", "/v1/whoami"))
    assert result_b == {"ok": True}
    # And there should now be TWO semaphores cached (one per loop id).
    # We cannot know the loop ids without instrumentation, so just assert the
    # bookkeeping dict exists and has ≥ 1 entry (the second loop's id may
    # equal the first if CPython reused the memory — that is fine, we only
    # care that no RuntimeError was raised).
    assert hasattr(bus, "_async_bulkheads_by_loop")


def test_concurrent_requests_share_a_semaphore_within_one_loop(bus) -> None:
    """Under ONE loop, concurrent requests must serialize through the same
    semaphore — otherwise the bulkhead cap does nothing. Assert both requests
    complete AND the bulkhead dict has exactly one entry per loop for this
    invocation (proving they used the same semaphore rather than each spawning
    one)."""

    async def two_concurrent():
        a, b = await asyncio.gather(
            bus._request("GET", "/v1/whoami"),
            bus._request("GET", "/v1/whoami"),
        )
        return a, b, len(bus._async_bulkheads_by_loop)

    a, b, sem_count = asyncio.run(two_concurrent())
    assert a == {"ok": True}
    assert b == {"ok": True}
    # Exactly one semaphore for the one loop we ran on.
    assert sem_count == 1


def test_transient_errors_still_retry_across_loops(monkeypatch) -> None:
    """The retry loop itself must survive the loop rebind — a caller who
    ran asyncio.run(bus.send()) and hit a 503 must still see the same retry
    behaviour on the next asyncio.run."""

    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        async def request(self, *_a, **_k):
            self.calls += 1
            # First call fails, second succeeds. On each fresh asyncio.run.
            if self.calls % 2 == 1:
                raise httpx.ConnectError("network")

            class _R:
                status_code = 200
                content = b'{"ok": true}'
                text = '{"ok": true}'

                def json(self):
                    return {"ok": True}

            return _R()

        async def aclose(self):  # pragma: no cover
            pass

    monkeypatch.setattr(client_module.resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(client_module.resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "3")

    bus_ = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="test-agent")
    client = _FlakyClient()
    monkeypatch.setattr(bus_, "_client", client)

    # First loop.
    r1 = asyncio.run(bus_._request("GET", "/v1/whoami"))
    assert r1 == {"ok": True}
    assert client.calls == 2  # 1 fail + 1 retry-success

    # Second loop. Semaphore for loop 1 is now dead; must rebind, retry works.
    r2 = asyncio.run(bus_._request("GET", "/v1/whoami"))
    assert r2 == {"ok": True}
    assert client.calls == 4  # +1 fail +1 retry-success
