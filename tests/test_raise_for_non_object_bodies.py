"""#94 item 2: an error body that is JSON but not an object.

`_raise_for` called `.get` on whatever the body parsed to, so a gateway answering
`[1,2]`, a string, a number or `null` raised AttributeError in place of the typed
error for that status. The retry and breaker classifiers then saw an AttributeError
and did nothing with it.

The twins are driven through their real `_request` and the real retry and breaker
stack, with a stub HTTP transport.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import resilient_circuit as rc

from agentbus_client import _env_numbers, cli
from agentbus_client.cli import _common
from agentbus_client.client import (
    AgentBus,
    AgentBusError,
    AsyncAgentBus,
    AuthError,
    NotFoundError,
    QuotaExceeded,
    RateLimited,
    ServiceUnavailable,
    TransportError,
    ValidationError,
    resilience,
)
from agentbus_client.client._async_breaker import _AsyncCircuitBreaker
from agentbus_client.client.errors import PermissionError_, _raise_for

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://stub"
PATH = "/v1/whoami"
JSON = {"content-type": "application/json"}

NOT_OBJECTS = [
    pytest.param(b"[1,2]", id="list"),
    pytest.param(b"[]", id="empty-list"),
    pytest.param(b'"bad gateway"', id="string"),
    pytest.param(b"7", id="number"),
    pytest.param(b"null", id="null"),
    pytest.param(b"true", id="bool"),
]
TYPED = [
    (400, AgentBusError),
    (401, AuthError),
    (403, PermissionError_),
    (404, NotFoundError),
    (409, ValidationError),
    (413, ValidationError),
    (418, AgentBusError),
    (422, ValidationError),
    (429, QuotaExceeded),
    (500, AgentBusError),
    (502, AgentBusError),
    (503, ServiceUnavailable),
    (504, AgentBusError),
    (599, AgentBusError),
]


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_INFLIGHT", set())
    monkeypatch.setattr(
        resilience, "_ASYNC_CIRCUIT_BREAKER", _AsyncCircuitBreaker(failure_limit=99)
    )
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())
    for name in (
        "AGENTBUS_SDK_RESILIENCE",
        "AGENTBUS_SDK_MAX_RETRIES",
        "AGENTBUS_SDK_CB_FAILURE_LIMIT",
        "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
        "AGENTBUS_SDK_CB_COOLDOWN",
    ):
        monkeypatch.delenv(name, raising=False)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(rc.ExponentialDelay, "for_attempt", lambda _self, _attempt: 0.0)


def _server(answers: list[httpx.Response]) -> tuple[list[str], Callable[..., httpx.Response]]:
    seen: list[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        position = min(len(seen), len(answers) - 1)
        seen.append(request.url.path)
        answer = answers[position]
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    return seen, _answer


def _sync_call(answers: list[httpx.Response], method: str = "GET") -> tuple[list[str], object]:
    seen, answer = _server(answers)
    bus = AgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
    bus._client = httpx.Client(transport=httpx.MockTransport(answer), base_url=URL)
    try:
        return seen, bus._request(method, PATH)
    except Exception as exc:  # noqa: BLE001
        return seen, exc


def _async_call(answers: list[httpx.Response], method: str = "GET") -> tuple[list[str], object]:
    seen, answer = _server(answers)

    async def _go() -> Any:
        bus = AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        bus._client = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url=URL)
        return await bus._request(method, PATH)

    try:
        return seen, asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        return seen, exc


TWINS = [pytest.param(_sync_call, id="sync"), pytest.param(_async_call, id="async")]


def _reply(status: int, raw: bytes) -> httpx.Response:
    return httpx.Response(status, headers=JSON, content=raw)


@pytest.mark.parametrize(("status", "expected"), TYPED)
@pytest.mark.parametrize("raw", NOT_OBJECTS)
def test_every_status_keeps_its_typed_error(
    status: int, expected: type[AgentBusError], raw: bytes
) -> None:
    with pytest.raises(AgentBusError) as caught:
        _raise_for(_reply(status, raw))
    assert type(caught.value) is expected
    assert caught.value.status == status
    assert caught.value.code == "error"
    assert caught.value.body == {}
    assert caught.value.detail == raw.decode()


@pytest.mark.parametrize("call", TWINS)
@pytest.mark.parametrize("raw", NOT_OBJECTS)
def test_a_502_stays_typed_through_the_retry_path(call: Any, raw: bytes) -> None:
    seen, outcome = call([_reply(502, raw)])
    assert seen == [PATH] * 4
    assert type(outcome) is AgentBusError
    assert outcome.status == 502
    assert outcome.detail == raw.decode()


@pytest.mark.parametrize("call", TWINS)
@pytest.mark.parametrize("raw", NOT_OBJECTS)
def test_a_503_is_still_service_unavailable_and_still_retries(call: Any, raw: bytes) -> None:
    seen, outcome = call([_reply(503, raw)])
    assert seen == [PATH] * 4
    assert type(outcome) is ServiceUnavailable
    assert outcome.status == 503
    assert outcome.retry_after is None


@pytest.mark.parametrize("call", TWINS)
def test_a_retried_503_can_recover(call: Any) -> None:
    list_503 = _reply(503, b"[1,2]")
    seen, outcome = call([list_503, list_503, httpx.Response(200, json={"ok": True})])
    assert seen == [PATH] * 3
    assert outcome == {"ok": True}


@pytest.mark.parametrize("call", TWINS)
def test_a_call_that_is_not_safe_to_repeat_gets_one_attempt(call: Any) -> None:
    seen, outcome = call([_reply(503, b"[1,2]")], "POST")
    assert seen == [PATH]
    assert type(outcome) is ServiceUnavailable
    assert outcome.status == 503


@pytest.mark.parametrize("call", TWINS)
def test_a_definite_refusal_is_not_retried(call: Any) -> None:
    seen, outcome = call([_reply(404, b"[1,2]")])
    assert seen == [PATH]
    assert type(outcome) is NotFoundError


@pytest.mark.parametrize("call", TWINS)
def test_a_429_has_empty_quota_fields(call: Any) -> None:
    seen, outcome = call([_reply(429, b"[1,2]")])
    assert seen == [PATH]
    assert type(outcome) is QuotaExceeded
    assert outcome.retry_after is None
    assert outcome.reset_at is None
    assert outcome.blocking_policy == {}


@pytest.mark.parametrize("call", TWINS)
def test_the_breaker_counts_it_as_a_failure(call: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    monkeypatch.setattr(resilience, "_ASYNC_CIRCUIT_BREAKER", None)
    list_502 = _reply(502, b"[1,2]")
    first_seen, first = call([list_502])
    second_seen, second = call([list_502])
    assert first_seen == [PATH]
    assert type(first) is AgentBusError
    assert second_seen == []
    assert type(second) is TransportError
    assert "circuit breaker is OPEN" in str(second)


def test_an_object_body_keeps_every_field() -> None:
    """The direction that keeps the fix honest: a fix that dropped every body would
    pass all of the above."""
    wire = {"code": "rate_limited", "detail": "slow down", "retry_after": 9, "reset_at": "z"}
    with pytest.raises(RateLimited) as limited:
        _raise_for(_reply(429, json.dumps(wire).encode()))
    assert limited.value.status == 429
    assert limited.value.code == "rate_limited"
    assert limited.value.detail == "slow down"
    assert limited.value.retry_after == 9
    assert limited.value.reset_at == "z"
    assert limited.value.body == wire
    busy = {"code": "dependency_down", "title": "try later", "retry_after": 3}
    with pytest.raises(ServiceUnavailable) as unavailable:
        _raise_for(_reply(503, json.dumps(busy).encode()))
    assert unavailable.value.detail == "try later"
    assert unavailable.value.retry_after == 3
    assert unavailable.value.body == busy


@pytest.mark.parametrize(
    ("raw", "detail"), [(b"<html>bad gateway</html>", "<html>bad gateway</html>"), (b"", "")]
)
def test_a_body_that_is_not_json_still_works(raw: bytes, detail: str) -> None:
    with pytest.raises(AgentBusError) as caught:
        _raise_for(httpx.Response(502, content=raw))
    assert type(caught.value) is AgentBusError
    assert caught.value.status == 502
    assert caught.value.detail == detail
    assert caught.value.body == {}


@pytest.mark.parametrize(
    "case",
    [
        (502, 4, 3, "error: [1,2]"),
        (503, 4, 5, "service unavailable: [1,2] (retry in 30s)"),
        (429, 1, 4, "quota exceeded: [1,2]"),
        (401, 1, 8, "error: [1,2]"),
    ],
    ids=["502", "503", "429", "401"],
)
def test_the_cli_says_what_the_gateway_said(
    case: tuple[int, int, int, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Through main(), the SDK and `_raise_for`, with only HTTP stubbed."""
    status, attempts, exit_code, line = case
    seen, answer = _server([_reply(status, b"[1,2]")])
    bus = AgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
    bus._client = httpx.Client(transport=httpx.MockTransport(answer), base_url=URL)
    monkeypatch.setattr(_common, "_bus", lambda _args: bus)
    assert cli.main(["whoami"]) == exit_code
    assert seen == [PATH] * attempts
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{line}\n"
