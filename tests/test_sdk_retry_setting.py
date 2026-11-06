"""#93 items 14 and 16: the retry setting and the in-flight set.

Item 14: the sync and async clients read AGENTBUS_SDK_MAX_RETRIES separately and
disagreed on -1: the sync client raised TransportError, the async client raised
RuntimeError, and neither called the bus. A value that is not a whole number
raised ValueError out of every call.

Item 16: `_run_with_resilience` added the cancel flag to _INFLIGHT before it
built the policies, so a failure while building them left the flag there.

The twins are driven through their real `_request` with a stub HTTP transport.
"""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn

import httpx
import pytest
import resilient_circuit as rc

from agentbus_client import _env_numbers
from agentbus_client.client import (
    AgentBus,
    AsyncAgentBus,
    ServiceUnavailable,
    _repeat,
    resilience,
)
from agentbus_client.client._async_breaker import _AsyncCircuitBreaker

URL = "https://stub"


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_INFLIGHT", set())
    monkeypatch.setattr(
        resilience, "_ASYNC_CIRCUIT_BREAKER", _AsyncCircuitBreaker(failure_limit=99)
    )
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())
    monkeypatch.delenv("AGENTBUS_SDK_RESILIENCE", raising=False)
    for name in (
        "AGENTBUS_SDK_MAX_CONCURRENT",
        "AGENTBUS_SDK_MAX_QUEUE",
        "AGENTBUS_SDK_MAX_RETRIES",
        "AGENTBUS_SDK_CB_COOLDOWN",
        "AGENTBUS_SDK_CB_FAILURE_LIMIT",
        "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(rc.ExponentialDelay, "for_attempt", lambda _self, _attempt: 0.0)


def _sync_call(status: int) -> tuple[list[object], object]:
    seen: list[object] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(status, json={"code": "c", "detail": "d", "ok": status == 200})

    bus = AgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
    bus._client = httpx.Client(transport=httpx.MockTransport(_answer), base_url=URL)
    try:
        return seen, bus._request("GET", "/v1/whoami")
    except Exception as exc:  # noqa: BLE001
        return seen, exc


def _async_call(status: int) -> tuple[list[object], object]:
    seen: list[object] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(status, json={"code": "c", "detail": "d", "ok": status == 200})

    async def _go() -> Any:
        bus = AsyncAgentBus(api_key="ab_sk_stub", base_url=URL, agent="t")
        bus._client = httpx.AsyncClient(transport=httpx.MockTransport(_answer), base_url=URL)
        return await bus._request("GET", "/v1/whoami")

    try:
        return seen, asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001
        return seen, exc


TWINS = [pytest.param(_sync_call, id="sync"), pytest.param(_async_call, id="async")]
OK_BODY = {"code": "c", "detail": "d", "ok": True}
UNUSABLE = ["abc", "", "3.5", "1e3", "three"]


@pytest.mark.parametrize("call", TWINS)
@pytest.mark.parametrize("value", ["-1", "-5"])
def test_a_negative_value_makes_one_real_attempt(
    call: Any, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", value)
    seen, outcome = call(200)
    assert seen == ["/v1/whoami"]
    assert outcome == OK_BODY


@pytest.mark.parametrize("call", TWINS)
def test_a_negative_value_surfaces_the_server_error_after_one_attempt(
    call: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both twins raised something else before, without calling the bus."""
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "-1")
    seen, outcome = call(503)
    assert seen == ["/v1/whoami"]
    assert isinstance(outcome, ServiceUnavailable)
    assert outcome.detail == "d"


@pytest.mark.parametrize("call", TWINS)
@pytest.mark.parametrize("value", UNUSABLE)
def test_an_unusable_value_does_not_reach_the_caller(
    call: Any, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", value)
    seen, outcome = call(200)
    assert seen == ["/v1/whoami"]
    assert outcome == OK_BODY


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("call", TWINS)
def test_an_unusable_value_falls_back_to_the_default_of_three_retries(
    call: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "abc")
    seen, outcome = call(503)
    assert len(seen) == 4
    assert isinstance(outcome, ServiceUnavailable)


@pytest.mark.usefixtures("no_backoff")
@pytest.mark.parametrize("call", TWINS)
@pytest.mark.parametrize(("value", "attempts"), [("0", 1), ("2", 3), (" 1 ", 2)])
def test_a_usable_value_is_honoured(
    call: Any, value: str, attempts: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direction that keeps the fix honest: a clamp that ignored the setting
    would pass every test above."""
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", value)
    seen, _ = call(503)
    assert len(seen) == attempts


@pytest.mark.parametrize(
    ("value", "used"),
    [(None, 3), ("0", 0), ("7", 7), ("-1", 0), ("-99", 0), ("abc", 3), ("", 3)],
)
def test_the_shared_reader(value: str | None, used: int, monkeypatch: pytest.MonkeyPatch) -> None:
    if value is not None:
        monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", value)
    assert _repeat._sdk_max_retries() == used


def test_a_bad_value_is_reported_once_with_what_was_used(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "-1")
    assert [_repeat._sdk_max_retries() for _ in range(3)] == [0, 0, 0]
    assert capsys.readouterr().err == (
        "agentbus: AGENTBUS_SDK_MAX_RETRIES='-1' is not a whole number of 0 or more. "
        "The client is using 0. Set it to 0 or higher, or unset it for the default of 3.\n"
    )
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "abc")
    assert _repeat._sdk_max_retries() == 3
    assert "AGENTBUS_SDK_MAX_RETRIES='abc'" in capsys.readouterr().err


def test_a_good_value_says_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "2")
    assert _repeat._sdk_max_retries() == 2
    assert capsys.readouterr().err == ""


def _broken() -> NoReturn:
    msg = "the stack could not be built"
    raise RuntimeError(msg)


@pytest.mark.parametrize("builder", ["_sdk_safety_net", "_sdk_bulkhead"])
def test_a_stack_that_cannot_be_built_leaves_nothing_in_flight(
    builder: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resilience, builder, _broken)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="could not be built"):
            resilience._run_with_resilience(lambda: "never")
    assert len(resilience._INFLIGHT) == 0


def test_a_running_call_is_in_flight_and_is_gone_afterwards() -> None:
    """The positive for the test above: the set is really used."""
    during: list[int] = []

    def _peek() -> str:
        during.append(len(resilience._INFLIGHT))
        return "done"

    assert resilience._run_with_resilience(_peek) == "done"
    assert during == [1]
    assert len(resilience._INFLIGHT) == 0
