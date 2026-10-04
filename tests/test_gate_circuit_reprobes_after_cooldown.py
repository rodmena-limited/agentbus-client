from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk

REAL_GMTIME = time.gmtime


class _Clock:
    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def time(self) -> float:
        return self.now

    def gmtime(self, secs: float | None = None) -> time.struct_time:
        return REAL_GMTIME(self.now if secs is None else secs)


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class _Guard:
    def __init__(self) -> None:
        self.healthy = False
        self.requests = 0

    def urlopen(self, request: Any, timeout: float = 0) -> _Resp:
        self.requests += 1
        if not self.healthy:
            raise urllib.error.HTTPError(request.full_url, 503, "down", None, io.BytesIO(b"{}"))
        return _Resp({"decision": "deny", "reason": "GUARD-DENY needs approval"})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[_Clock, _Guard]:
    clock, guard = _Clock(), _Guard()
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "gmtime", clock.gmtime)
    monkeypatch.setattr(hk._gate, "_bus_reachable", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(hk._gate.urllib.request, "urlopen", guard.urlopen)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test")
    monkeypatch.setenv("AGENTBUS_AGENT", "circuit-agent")
    monkeypatch.setenv("AGENTBUS_BASE_URL", "https://bus.invalid")
    monkeypatch.delenv("AGENTBUS_GATE_FAST_FAIL_COOLDOWN", raising=False)
    monkeypatch.delenv("AGENTBUS_GATE_FAST_FAIL_AFTER", raising=False)
    return clock, guard


def _call(monkeypatch: pytest.MonkeyPatch) -> str:
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
    return str(json.loads(line)["hookSpecificOutput"]["permissionDecision"])


def _open_circuit(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> None:
    for _ in range(3):
        assert _call(monkeypatch) == "allow"
        clock.now += 1


def test_continuous_calls_reach_the_guard_once_the_cooldown_has_passed(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    _open_circuit(monkeypatch, clock)
    guard.healthy = True
    before = guard.requests
    decisions = []
    for _ in range(4):
        decisions.append(_call(monkeypatch))
        clock.now += 20
    assert decisions[:2] == ["allow", "allow"]
    assert "deny" in decisions[2:], decisions
    assert guard.requests > before


def test_the_circuit_still_fast_fails_inside_the_cooldown(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    _open_circuit(monkeypatch, clock)
    guard.healthy = True
    before = guard.requests
    clock.now += 10
    assert _call(monkeypatch) == "allow"
    assert guard.requests == before


def test_a_failed_probe_reopens_the_circuit_for_a_full_cooldown(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, guard = rig
    _open_circuit(monkeypatch, clock)
    clock.now += 31
    before = guard.requests
    assert _call(monkeypatch) == "allow"
    assert guard.requests > before
    probed = guard.requests
    clock.now += 10
    assert _call(monkeypatch) == "allow"
    assert guard.requests == probed


def test_a_fast_fail_keeps_the_time_the_circuit_opened(
    monkeypatch: pytest.MonkeyPatch, rig: tuple[_Clock, _Guard]
) -> None:
    clock, _guard = rig
    _open_circuit(monkeypatch, clock)
    state_file = hk._gate_degraded_file("circuit-agent")
    opened = json.loads(state_file.read_text())["opened_at"]
    clock.now += 5
    _call(monkeypatch)
    after = json.loads(state_file.read_text())
    assert after["opened_at"] == opened
    assert after["last_at"] != opened
    assert after["reason"] == "fast_fail"
