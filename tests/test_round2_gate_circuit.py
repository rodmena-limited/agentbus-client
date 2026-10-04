from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk
from agentbus_client.hooks import _gate

REAL_GMTIME = time.gmtime
AGENT = "circuit-two"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def gmtime(self, secs: float | None = None) -> time.struct_time:
        return REAL_GMTIME(self.now if secs is None else secs)

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode()
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class HTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str) -> None:
        super().__init__("https://bus.example/v1/guard/check", code, "x", None, None)
        self._body = body

    def read(self) -> bytes:
        return self._body.encode()


class _Guard:
    def __init__(self) -> None:
        self.failure: tuple[int, str] | None = (503, "{}")
        self.requests = 0

    def urlopen(self, request: Any, timeout: Any = None) -> _Resp:
        self.requests += 1
        if self.failure is not None:
            raise HTTPError(*self.failure)
        return _Resp({"decision": "deny", "reason": "GUARD-DENY circuit"})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[_Clock, _Guard]:
    clock, guard = _Clock(), _Guard()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "gmtime", clock.gmtime)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(_gate, "_bus_reachable", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(_gate.urllib.request, "urlopen", guard.urlopen)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_circuit")
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)
    monkeypatch.setenv("AGENTBUS_BASE_URL", "https://bus.example")
    for key in (
        "AGENTBUS_GATE_FAST_FAIL_AFTER",
        "AGENTBUS_GATE_FAST_FAIL_COOLDOWN",
        "AGENTBUS_GATE_TIMEOUT",
        "AGENTBUS_GATE_CONNECT_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    return clock, guard


def _call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    out: list[str] = []

    class _Out:
        def write(self, s: str) -> int:
            out.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _Out())
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}')
    )
    assert hk.pre_tool_use(None) == 0
    line = next(chunk for chunk in out if "hookSpecificOutput" in chunk)
    return dict(json.loads(line)["hookSpecificOutput"])


def _state() -> dict[str, Any]:
    return dict(json.loads(hk._gate_degraded_file(AGENT).read_text()))


def _fail(monkeypatch: pytest.MonkeyPatch, clock: _Clock, times: int) -> None:
    for _ in range(times):
        assert _call(monkeypatch)["permissionDecision"] == "allow"
        clock.now += 1


def _fast_fail_reason(count: int, cooldown: int) -> str:
    return (
        f"AgentBus gate is FAST-FAILING (circuit open, {count} recent failures; cooldown "
        f"{cooldown}s). Action runs UNVETTED — approval checking is OFF. Fix the bus or the "
        "credential; a real verdict clears the circuit."
    )


def test_the_degraded_record_counts_every_degraded_call(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, _guard = rig
    _fail(monkeypatch, clock, 1)
    assert _state()["count"] == 1
    assert _state()["reason"] == "httperror"
    _fail(monkeypatch, clock, 1)
    assert _state()["count"] == 2


def test_two_failures_do_not_open_the_default_circuit_and_three_do(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    _fail(monkeypatch, clock, 2)
    before = guard.requests
    assert _call(monkeypatch)["permissionDecision"] == "allow"
    assert guard.requests == before + 2
    clock.now += 1
    before = guard.requests
    out = _call(monkeypatch)
    assert guard.requests == before
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == _fast_fail_reason(3, 30)
    assert _state()["count"] == 4
    assert _state()["reason"] == "fast_fail"


def test_a_single_401_does_not_open_the_circuit(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    guard.failure = (401, '{"code":"invalid_api_key","detail":"API key is unknown or revoked"}')
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == (
        "AgentBus could not verify this action (HTTP Error 401: x), so it runs UNVETTED — "
        "approval checking is OFF for this call, not just for this action. If this session's "
        "actions need human approval, restore the credential (agentbus signin) and re-run."
    )
    assert guard.requests == 1
    assert clock.sleeps == []
    assert _state()["count"] == 1
    assert _state()["reason"] == "httperror"
    guard.failure = None
    clock.now += 1
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "GUARD-DENY circuit"
    assert guard.requests == 2
    assert not hk._gate_degraded_file(AGENT).exists()


def test_a_503_is_retried_once_after_a_quarter_second(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    assert _call(monkeypatch)["permissionDecision"] == "allow"
    assert guard.requests == 2
    assert clock.sleeps == [0.25]


def test_fast_fail_after_override_opens_the_circuit_after_one_failure(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_AFTER", "1")
    _fail(monkeypatch, clock, 1)
    guard.failure = None
    before = guard.requests
    out = _call(monkeypatch)
    assert guard.requests == before
    assert out["permissionDecisionReason"] == _fast_fail_reason(1, 30)


def test_fast_fail_after_override_raises_the_threshold(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_AFTER", "5")
    _fail(monkeypatch, clock, 4)
    guard.failure = None
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "deny"
    assert guard.requests == 9


def test_cooldown_override_keeps_the_circuit_open_longer(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_COOLDOWN", "100")
    _fail(monkeypatch, clock, 3)
    guard.failure = None
    clock.now += 50
    before = guard.requests
    out = _call(monkeypatch)
    assert guard.requests == before
    assert out["permissionDecisionReason"] == _fast_fail_reason(3, 100)


def test_cooldown_override_closes_the_circuit_sooner(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_COOLDOWN", "5")
    _fail(monkeypatch, clock, 3)
    guard.failure = None
    clock.now += 10
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "GUARD-DENY circuit"


@pytest.mark.parametrize(("elapsed", "probes"), [(29, False), (30, True)])
def test_the_circuit_probes_once_exactly_the_cooldown_has_elapsed(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard], elapsed: int, probes: bool
) -> None:
    clock, guard = rig
    _fail(monkeypatch, clock, 3)
    opened = clock.now - 1
    guard.failure = None
    clock.now = opened + elapsed
    before = guard.requests
    out = _call(monkeypatch)
    assert (guard.requests > before) is probes
    assert out["permissionDecision"] == ("deny" if probes else "allow")


def test_a_record_without_a_count_does_not_open_a_threshold_of_one(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_AFTER", "1")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", clock.gmtime())
    path = hk._gate_degraded_file(AGENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_at": now, "opened_at": now, "reason": "httperror"}))
    guard.failure = None
    assert _call(monkeypatch)["permissionDecision"] == "deny"
    assert guard.requests == 1


RETIRED = json.dumps(
    {"code": "agent_retired", "detail": "Agent 'circuit-two' is retired since 2026-09-01."}
)


def test_a_retired_identity_says_re_register_and_records_identity_retired(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    guard.failure = (410, RETIRED)
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == (
        "Agent 'circuit-two' is retired since 2026-09-01. The guard could not verify this "
        "action because the agent identity is retired, so it runs UNVETTED. Re-register the "
        "agent name (agentbus register) to restore gating."
    )
    assert guard.requests == 1
    assert clock.sleeps == []
    state = _state()
    assert state["reason"] == "identity_retired"
    assert state["detail"] == "Agent 'circuit-two' is retired since 2026-09-01."
    assert state["count"] == 1


@pytest.mark.parametrize(
    "body", ['{"code":"gone","detail":"resource moved"}', "not json", '{"code":"gone"}']
)
def test_a_410_that_is_not_a_retirement_degrades_generically(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard], body: str
) -> None:
    _clock, guard = rig
    guard.failure = (410, body)
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"].startswith(
        "AgentBus could not verify this action (HTTP Error 410: x), so it runs UNVETTED"
    )
    assert "Re-register" not in out["permissionDecisionReason"]
    assert guard.requests == 1
    assert _state()["reason"] == "httperror"


def test_a_422_names_the_servers_validation_code_and_detail(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    _clock, guard = rig
    guard.failure = (422, '{"code":"field_too_long","detail":"tool_input.command > 4096"}')
    out = _call(monkeypatch)
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"].startswith(
        "AgentBus could not verify this action (HTTP 422 field_too_long: "
        "tool_input.command > 4096), so it runs UNVETTED"
    )
    assert guard.requests == 1
    assert _state()["reason"] == "guardrequestrejected"
