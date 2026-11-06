"""#94 item 4: the SDK's own numeric settings, read through the real clients.

Each setting used to go through a bare `int()` or `float()`, so a typo raised
ValueError out of every call. The out-of-range values were quieter. A pool of 0
connections times out every request, a semaphore of 0 parks every async call until
its deadline, and a cooldown of nan or inf leaves the async breaker never open or
open for good. The two twins parsed the cooldown differently: `int()` and `float()`.

The pool tests run against a real local HTTP server, because a stub transport does
not go through the connection pool.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import resilient_circuit as rc

from agentbus_client import _env_numbers
from agentbus_client.client import AgentBus, AsyncAgentBus, TransportError, resilience

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

URL = "https://stub"
PATH = "/v1/whoami"
OK_BODY = {"ok": True}
SETTINGS = (
    "AGENTBUS_SDK_CB_COOLDOWN",
    "AGENTBUS_SDK_CB_FAILURE_LIMIT",
    "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
    "AGENTBUS_SDK_MAX_CONCURRENT",
    "AGENTBUS_SDK_MAX_QUEUE",
    "AGENTBUS_SDK_MAX_RETRIES",
    "AGENTBUS_SDK_RESILIENCE",
    "AGENTBUS_MAX_CONNECTIONS",
    "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS",
    "AGENTBUS_KEEPALIVE_EXPIRY",
)


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_ASYNC_CIRCUIT_BREAKER", None)
    monkeypatch.setattr(resilience, "_INFLIGHT", set())
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rc.ExponentialDelay, "for_attempt", lambda _self, _attempt: 0.0)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        owner: Any = self.server
        owner.seen.append(self.path)
        if self.path == "/v1/hold":
            owner.arrived.set()
            owner.release.wait(10)
        body = json.dumps(OK_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@pytest.fixture
def server() -> Iterator[Any]:
    real = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    real.daemon_threads = True
    owner: Any = real
    owner.seen = []
    owner.arrived = threading.Event()
    owner.release = threading.Event()
    owner.url = f"http://127.0.0.1:{real.server_port}"
    thread = threading.Thread(target=real.serve_forever, daemon=True)
    thread.start()
    yield owner
    owner.release.set()
    real.shutdown()
    real.server_close()
    thread.join(5)


def _stub_sync() -> tuple[list[str], object]:
    seen: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=OK_BODY)

    try:
        bus = AgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        bus._client = httpx.Client(transport=httpx.MockTransport(_answer), base_url=URL)
        return seen, bus._request("GET", PATH)
    except Exception as exc:  # noqa: BLE001
        return seen, exc


def _stub_async() -> tuple[list[str], object]:
    seen: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=OK_BODY)

    async def _go() -> Any:
        bus = AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        bus._client = httpx.AsyncClient(transport=httpx.MockTransport(_answer), base_url=URL)
        return await bus._request("GET", PATH, timeout=0.2)

    try:
        return seen, asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        return seen, exc


def _real_sync(server: Any, *, timeout: float = 0.3) -> object:
    with AgentBus(api_key="ab_sk_stub", base_url=server.url, agent="t", timeout=timeout) as bus:
        return bus._request("GET", PATH)


def _real_async(server: Any, *, timeout: float = 0.3) -> object:
    async def _go() -> Any:
        key = "ab_sk_stub"
        async with AsyncAgentBus(api_key=key, base_url=server.url, agent="t", timeout=timeout) as b:
            return await b._request("GET", PATH)

    return asyncio.run(_go())


def _sync_pool() -> Any:
    return AgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")._client._transport._pool


def _async_pool() -> Any:
    return AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")._client._transport._pool


def _sync_cooldown() -> float:
    seconds: float = resilience._sdk_safety_net().policies[0].cooldown.total_seconds()
    return seconds


READERS: dict[str, Callable[[], float]] = {
    "failure-limit": lambda: resilience._sdk_cb_limits()[0],
    "success-limit": lambda: resilience._sdk_cb_limits()[1],
    "sync-cooldown": _sync_cooldown,
    "async-cooldown": lambda: resilience._async_circuit_breaker().cooldown,
    "bulkhead-workers": lambda: resilience._sdk_bulkhead().config.max_concurrent_calls,
    "executor-workers": lambda: resilience._sdk_bulkhead()._executor._max_workers,
    "queue": lambda: resilience._sdk_bulkhead().config.max_queue_size,
    "sync-connections": lambda: _sync_pool()._max_connections,
    "async-connections": lambda: _async_pool()._max_connections,
    "sync-keepalive": lambda: _sync_pool()._max_keepalive_connections,
    "async-keepalive": lambda: _async_pool()._max_keepalive_connections,
    "sync-expiry": lambda: _sync_pool()._keepalive_expiry,
    "async-expiry": lambda: _async_pool()._keepalive_expiry,
}
NOT_WHOLE = ["abc", "", "1.5", "1e3", "nan", "inf"]
NOT_SECONDS = ["abc", "", "nan", "inf", "-inf", "-1", "1e14", "1e400"]
TABLE = [
    ("failure-limit", "AGENTBUS_SDK_CB_FAILURE_LIMIT", 5, NOT_WHOLE),
    ("success-limit", "AGENTBUS_SDK_CB_SUCCESS_LIMIT", 2, NOT_WHOLE),
    ("sync-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", 30, NOT_SECONDS),
    ("async-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", 30, NOT_SECONDS),
    ("bulkhead-workers", "AGENTBUS_SDK_MAX_CONCURRENT", 8, [*NOT_WHOLE, "0", "-1"]),
    ("executor-workers", "AGENTBUS_SDK_MAX_CONCURRENT", 8, [*NOT_WHOLE, "0", "-1"]),
    ("queue", "AGENTBUS_SDK_MAX_QUEUE", 100, [*NOT_WHOLE, "-1"]),
    ("sync-connections", "AGENTBUS_MAX_CONNECTIONS", 20, [*NOT_WHOLE, "0", "-1"]),
    ("async-connections", "AGENTBUS_MAX_CONNECTIONS", 20, [*NOT_WHOLE, "0", "-1"]),
    ("sync-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", 10, [*NOT_WHOLE, "-1"]),
    ("async-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", 10, [*NOT_WHOLE, "-1"]),
    ("sync-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", 30, NOT_SECONDS),
    ("async-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", 30, NOT_SECONDS),
]
BAD_CASES = [
    pytest.param((reader, name, default, value), id=f"{reader}={value!r}")
    for reader, name, default, values in TABLE
    for value in values
]
GOOD_CASES = [
    ("failure-limit", "AGENTBUS_SDK_CB_FAILURE_LIMIT", "7", 7),
    ("success-limit", "AGENTBUS_SDK_CB_SUCCESS_LIMIT", "3", 3),
    ("sync-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", "2.5", 2.5),
    ("async-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", "2.5", 2.5),
    ("sync-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", "0", 0),
    ("async-cooldown", "AGENTBUS_SDK_CB_COOLDOWN", "0", 0),
    ("bulkhead-workers", "AGENTBUS_SDK_MAX_CONCURRENT", "3", 3),
    ("executor-workers", "AGENTBUS_SDK_MAX_CONCURRENT", "3", 3),
    ("queue", "AGENTBUS_SDK_MAX_QUEUE", "7", 7),
    ("queue", "AGENTBUS_SDK_MAX_QUEUE", "0", 0),
    ("sync-connections", "AGENTBUS_MAX_CONNECTIONS", "3", 3),
    ("async-connections", "AGENTBUS_MAX_CONNECTIONS", "3", 3),
    ("sync-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "4", 4),
    ("async-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "4", 4),
    ("sync-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "0", 0),
    ("async-keepalive", "AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "0", 0),
    ("sync-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", "2.5", 2.5),
    ("async-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", "2.5", 2.5),
    ("sync-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", "0", 0),
    ("async-expiry", "AGENTBUS_KEEPALIVE_EXPIRY", "0", 0),
]


@pytest.mark.parametrize("case", BAD_CASES)
def test_a_bad_setting_builds_the_stack_with_the_default(
    case: tuple[str, str, float, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reader, name, default, value = case
    monkeypatch.setenv(name, value)
    assert READERS[reader]() == default
    err = capsys.readouterr().err
    assert err.startswith(f"agentbus: {name}={value!r} is not a ")
    assert len(err.splitlines()) == 1


@pytest.mark.parametrize("case", GOOD_CASES, ids=lambda case: f"{case[0]}={case[2]}")
def test_a_usable_setting_is_honoured(
    case: tuple[str, str, str, float],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The direction that keeps the fix honest. 0 is a real answer for the queue, the
    keep-alive pool, its expiry and the cooldown, so those are not bad values."""
    reader, name, value, used = case
    monkeypatch.setenv(name, value)
    assert READERS[reader]() == used
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("call", [_stub_sync, _stub_async], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENTBUS_SDK_CB_FAILURE_LIMIT", "abc"),
        ("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "abc"),
        ("AGENTBUS_SDK_CB_COOLDOWN", "abc"),
        ("AGENTBUS_SDK_CB_COOLDOWN", "1e14"),
        ("AGENTBUS_SDK_MAX_CONCURRENT", "abc"),
        ("AGENTBUS_SDK_MAX_CONCURRENT", "0"),
        ("AGENTBUS_SDK_MAX_QUEUE", "abc"),
        ("AGENTBUS_SDK_MAX_QUEUE", "-1"),
        ("AGENTBUS_MAX_CONNECTIONS", "abc"),
        ("AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "abc"),
        ("AGENTBUS_KEEPALIVE_EXPIRY", "abc"),
    ],
)
def test_a_bad_setting_does_not_reach_the_caller(
    call: Any, name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    seen, outcome = call()
    assert seen == [PATH]
    assert outcome == OK_BODY
    assert len(resilience._INFLIGHT) == 0


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("call", [_real_sync, _real_async], ids=["sync", "async"])
@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_a_pool_that_admits_nothing_still_reaches_the_server(
    call: Any, value: str, server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_CONNECTIONS", value)
    assert call(server) == OK_BODY
    assert server.seen == [PATH]


def test_the_sync_pool_cap_blocks_and_then_resumes(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_CONNECTIONS", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    results: list[object] = []
    with AgentBus(api_key="ab_sk_stub", base_url=server.url, agent="t") as bus:
        holder = threading.Thread(
            target=lambda: results.append(bus._request("GET", "/v1/hold", timeout=8))
        )
        holder.start()
        assert server.arrived.wait(5)
        with pytest.raises(TransportError):
            bus._request("GET", PATH, timeout=0.2)
        server.release.set()
        holder.join(5)
        assert results == [OK_BODY]
        assert bus._request("GET", PATH, timeout=5) == OK_BODY
    assert server.seen == ["/v1/hold", PATH]


def test_the_async_pool_cap_blocks_and_then_resumes(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_CONNECTIONS", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")

    async def _go() -> None:
        async with AsyncAgentBus(api_key="ab_sk_stub", base_url=server.url, agent="t") as bus:
            holder = asyncio.create_task(bus._request("GET", "/v1/hold", timeout=8))
            assert await asyncio.to_thread(server.arrived.wait, 5)
            with pytest.raises(TransportError):
                await bus._request("GET", PATH, timeout=0.2)
            server.release.set()
            assert await holder == OK_BODY
            assert await bus._request("GET", PATH, timeout=5) == OK_BODY

    asyncio.run(_go())
    assert server.seen == ["/v1/hold", PATH]


@pytest.mark.parametrize(("value", "peak"), [("1", 1), ("2", 2), ("0", 3), ("-1", 3), ("abc", 3)])
def test_the_async_bulkhead_runs_and_still_caps(
    value: str, peak: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad value means the default of 8, so all 3 calls run. Before, 0 parked every
    call until the deadline."""
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", value)
    running = 0
    highest = 0

    async def _work() -> str:
        nonlocal running, highest
        running += 1
        highest = max(highest, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "done"

    async def _go() -> list[str]:
        bus = AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        return await asyncio.gather(
            *(bus._run_with_resilience_async(_work, deadline=1.0) for _ in range(3))
        )

    assert asyncio.run(_go()) == ["done"] * 3
    assert highest == peak


def test_a_queue_of_zero_rejects_and_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_QUEUE", "0")
    started = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def _hold() -> str:
        started.set()
        release.wait(5)
        return "held"

    holder = threading.Thread(
        target=lambda: results.append(resilience._run_with_resilience(_hold, timeout=8))
    )
    holder.start()
    assert started.wait(5)
    with pytest.raises(TransportError, match="bulkhead full"):
        resilience._run_with_resilience(lambda: "never", timeout=1)
    release.set()
    holder.join(5)
    assert results == ["held"]
    assert resilience._run_with_resilience(lambda: "after", timeout=5) == "after"


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_a_bad_cooldown_leaves_the_async_breaker_working(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nan the breaker never opened. With inf it opened for good."""
    monkeypatch.setenv("AGENTBUS_SDK_CB_COOLDOWN", value)
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    calls: list[int] = []

    async def _fail() -> None:
        calls.append(1)
        msg = "connection refused"
        raise TransportError(msg)

    async def _go() -> None:
        bus = AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        with pytest.raises(TransportError, match="connection refused"):
            await bus._run_with_resilience_async(_fail)
        with pytest.raises(TransportError, match="circuit breaker is OPEN"):
            await bus._run_with_resilience_async(_fail)

    asyncio.run(_go())
    assert calls == [1]
    breaker = resilience._async_circuit_breaker()
    assert breaker.cooldown == 30
    assert 0 < breaker._open_until - time.monotonic() <= 30


@pytest.mark.parametrize(("limit", "used"), [("0", 1), ("-4", 1), ("1", 1)])
def test_the_breaker_limits_keep_their_floor(
    limit: str, used: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", limit)
    assert resilience._sdk_cb_limits() == (used, 2)
    named = "The client is using 1." in capsys.readouterr().err
    assert named is (limit != "1")


def test_many_clients_report_a_bad_setting_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_MAX_CONNECTIONS", "abc")
    for _ in range(3):
        _sync_pool()
        _async_pool()
    assert capsys.readouterr().err == (
        "agentbus: AGENTBUS_MAX_CONNECTIONS='abc' is not a whole number of 1 or more. "
        "The client is using 20. Set it to 1 or higher, or unset it for the default of 20.\n"
    )
