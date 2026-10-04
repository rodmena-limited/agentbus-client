from __future__ import annotations

import datetime
import time
from fractions import Fraction

import httpx
import pytest
import resilient_circuit.exceptions as rc_exc

from agentbus_client.client import AgentBus, resilience
from agentbus_client.client.errors import (
    AgentBusError,
    AuthError,
    ServiceUnavailable,
    TransportError,
)

OPEN_MARK = "agentbus SDK circuit breaker is OPEN"


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_ASYNC_CIRCUIT_BREAKER", None)
    for name in (
        "AGENTBUS_SDK_MAX_CONCURRENT",
        "AGENTBUS_SDK_MAX_QUEUE",
        "AGENTBUS_SDK_CB_COOLDOWN",
        "AGENTBUS_SDK_MAX_RETRIES",
        "AGENTBUS_SDK_CB_FAILURE_LIMIT",
        "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
        "AGENTBUS_SDK_RESILIENCE",
    ):
        monkeypatch.delenv(name, raising=False)


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        import json

        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self) -> dict:
        return self._body


class _Stub:
    def __init__(self, step: object) -> None:
        self.step = step
        self.calls = 0

    def request(self, *_a: object, **_k: object) -> _Resp:
        self.calls += 1
        step = self.step
        if isinstance(step, BaseException):
            raise step
        status, body = step
        return _Resp(status, body)

    def close(self) -> None:
        return None


def _bus(monkeypatch: pytest.MonkeyPatch, step: object) -> tuple[AgentBus, _Stub]:
    bus = AgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="test-agent")
    stub = _Stub(step)
    monkeypatch.setattr(bus, "_client", stub)
    return bus, stub


def _outcomes(bus: AgentBus, n: int) -> list[str]:
    seen: list[str] = []
    for _ in range(n):
        try:
            bus._request("GET", "/v1/whoami")
            seen.append("ok")
        except TransportError as exc:
            seen.append("open" if OPEN_MARK in str(exc) else "transport")
        except ServiceUnavailable:
            seen.append("503")
        except AuthError:
            seen.append("401")
    return seen


def _rlr_chain(root: BaseException, depth: int) -> list[BaseException]:
    chain: list[BaseException] = [root]
    for _ in range(depth):
        wrapper = rc_exc.RetryLimitReached("exhausted")
        wrapper.__cause__ = chain[-1]
        chain.append(wrapper)
    return chain


def test_cb_window_values() -> None:
    assert resilience._cb_window(5) == Fraction(4, 5)
    assert resilience._cb_window(5).denominator == 5
    assert resilience._cb_window(2) == Fraction(1, 2)
    assert resilience._cb_window(2).denominator == 2
    assert resilience._cb_window(1) == Fraction(1, 1)
    assert resilience._cb_window(3) == Fraction(2, 3)
    assert resilience._cb_window(7) == Fraction(6, 7)


def test_root_cause_unwraps_up_to_eight_hops() -> None:
    root = TransportError("down")
    for depth in range(1, 9):
        chain = _rlr_chain(root, depth)
        assert resilience._root_cause(chain[-1]) is root


def test_root_cause_stops_after_eight_hops() -> None:
    root = TransportError("down")
    chain = _rlr_chain(root, 9)
    assert resilience._root_cause(chain[-1]) is chain[1]


def test_root_cause_leaves_other_errors_alone() -> None:
    plain = TransportError("down")
    assert resilience._root_cause(plain) is plain
    bare = rc_exc.RetryLimitReached("no cause")
    assert resilience._root_cause(bare) is bare
    other = ValueError("x")
    other.__cause__ = TransportError("hidden")
    assert resilience._root_cause(other) is other


def test_breaker_should_handle_classification() -> None:
    assert resilience._breaker_should_handle(resilience._Abandoned("late")) is True
    assert resilience._breaker_should_handle(_rlr_chain(TransportError("x"), 1)[-1]) is True
    assert resilience._breaker_should_handle(_rlr_chain(TransportError("x"), 3)[-1]) is True
    assert resilience._breaker_should_handle(_rlr_chain(ServiceUnavailable("x"), 1)[-1]) is True
    assert resilience._breaker_should_handle(_rlr_chain(AuthError("x"), 1)[-1]) is False
    assert resilience._breaker_should_handle(_rlr_chain(resilience._Abandoned("x"), 1)[-1])
    wrapped_500 = resilience._NonIdempotent(AgentBusError("boom", status=500))
    assert resilience._breaker_should_handle(wrapped_500) is True
    wrapped_409 = resilience._NonIdempotent(AgentBusError("dup", status=409))
    assert resilience._breaker_should_handle(wrapped_409) is False
    assert resilience._breaker_should_handle(AuthError("revoked")) is False
    assert resilience._breaker_should_handle(TransportError("down")) is True


def test_safety_net_wires_the_breaker_classifier_and_windows() -> None:
    net = resilience._sdk_safety_net()
    breaker, retry = net.policies
    assert breaker.should_consider_failure is resilience._breaker_should_handle
    assert retry.should_consider_failure is resilience._is_transient_sdk_error
    assert breaker.failure_limit == Fraction(4, 5)
    assert breaker.success_limit == Fraction(1, 2)
    assert breaker.cooldown == datetime.timedelta(seconds=30)
    assert retry.max_attempts == 4
    assert retry.backoff.jitter == 0.2
    assert retry.backoff.min_delay == datetime.timedelta(milliseconds=500)
    assert retry.backoff.max_delay == datetime.timedelta(seconds=8)


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_n_failing_sequences_open_the_breaker(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", str(limit))
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    seen = _outcomes(bus, limit + 2)
    assert seen == ["transport"] * limit + ["open", "open"]
    assert stub.calls == limit


def test_default_limit_opens_after_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    seen = _outcomes(bus, 7)
    assert seen == ["transport"] * 5 + ["open", "open"]
    assert stub.calls == 5


def test_open_breaker_error_carries_the_protected_call_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    bus, _stub = _bus(monkeypatch, httpx.ConnectError("down"))
    _outcomes(bus, 1)
    with pytest.raises(TransportError) as info:
        bus._request("GET", "/v1/whoami")
    assert OPEN_MARK in str(info.value)
    assert isinstance(info.value.__cause__, rc_exc.ProtectedCallError)


def test_a_single_503_surfaces_as_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    bus, stub = _bus(monkeypatch, (503, {"code": "unavailable", "detail": "deploy"}))
    with pytest.raises(ServiceUnavailable) as info:
        bus._request("GET", "/v1/whoami")
    assert info.value.status == 503
    assert stub.calls == 1


def test_repeated_503_sequences_open_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "2")
    bus, stub = _bus(monkeypatch, (503, {"code": "unavailable", "detail": "deploy"}))
    assert _outcomes(bus, 3) == ["503", "503", "open"]
    assert stub.calls == 2


def test_401s_never_open_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "2")
    bus, stub = _bus(monkeypatch, (401, {"code": "invalid_api_key", "detail": "no"}))
    assert _outcomes(bus, 12) == ["401"] * 12
    assert stub.calls == 12


def test_breaker_closes_again_after_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_CB_COOLDOWN", "1")
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    assert _outcomes(bus, 2) == ["transport", "open"]
    stub.step = (200, {"ok": True})
    time.sleep(1.3)
    assert _outcomes(bus, 3) == ["ok", "ok", "ok"]
    assert stub.calls == 4


def test_cooldown_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_COOLDOWN", "17")
    breaker = resilience._sdk_safety_net().policies[0]
    assert breaker.cooldown == datetime.timedelta(seconds=17)


def test_cooldown_holds_the_breaker_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_CB_COOLDOWN", "30")
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    _outcomes(bus, 1)
    stub.step = (200, {"ok": True})
    time.sleep(1.2)
    assert _outcomes(bus, 2) == ["open", "open"]
    assert stub.calls == 1


def test_max_retries_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "1")
    assert resilience._sdk_safety_net().policies[1].max_attempts == 2
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    assert _outcomes(bus, 1) == ["transport"]
    assert stub.calls == 2


def test_max_retries_zero_attempts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "0")
    bus, stub = _bus(monkeypatch, httpx.ConnectError("down"))
    assert _outcomes(bus, 1) == ["transport"]
    assert stub.calls == 1


def test_failure_and_success_limit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "1")
    assert resilience._sdk_cb_limits() == (1, 1)
    breaker = resilience._sdk_safety_net().policies[0]
    assert breaker.failure_limit == Fraction(1, 1)
    assert breaker.success_limit == Fraction(1, 1)


def test_limits_env_overrides_above_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "6")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "3")
    assert resilience._sdk_cb_limits() == (6, 3)
    breaker = resilience._sdk_safety_net().policies[0]
    assert breaker.failure_limit == Fraction(5, 6)
    assert breaker.success_limit == Fraction(2, 3)


def test_limits_are_floored_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_CB_FAILURE_LIMIT", "0")
    monkeypatch.setenv("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "-4")
    assert resilience._sdk_cb_limits() == (1, 1)
