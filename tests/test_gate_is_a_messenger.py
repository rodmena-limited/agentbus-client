"""#90: the gate delivers a tool call to the guard and never decides on its own.

Operator ruling, 2026-11-06: AgentBus is a communication tool, not a decision tool.
The guard decides. These tests pin both halves: a call that is hard to read still
reaches the guard and the guard's answer stands, and when no answer can be had the
call runs with a loud warning.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.request
from typing import TYPE_CHECKING, Any, cast

import pytest
from typing_extensions import Self

import agentbus_client.hooks.claude_code as hk
from agentbus_client.hooks import _gate, _gate_input, _state

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

ALLOW = {"decision": "allow", "reason": "GUARD-ALLOW"}
DENY = {"decision": "deny", "reason": "GUARD-DENY"}
PLAIN = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})


class _Resp:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        pass


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        self.asked: list[dict[str, Any]] = []
        self.timeouts: list[float] = []
        self.guard_body = json.dumps(DENY).encode()
        self.reachable: tuple[bool, str] = (True, "")
        monkeypatch.setattr(_state, "_hook_state_dir", lambda: tmp_path)
        monkeypatch.setattr(_gate, "_bus_reachable", lambda *_a: self.reachable)
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)
        monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test")
        monkeypatch.setenv("AGENTBUS_AGENT", "messenger-agent")
        monkeypatch.setenv("AGENTBUS_BASE_URL", "https://bus.invalid")

    def _urlopen(self, request: Any, timeout: float = 0) -> _Resp:
        self.asked.append(json.loads(request.data.decode()))
        self.timeouts.append(timeout)
        return _Resp(self.guard_body)

    def answer(self, body: object) -> None:
        self.guard_body = json.dumps(body).encode()

    def run(self, stdin: str) -> tuple[int, dict[str, Any]]:
        out: list[str] = []

        class _Out:
            def write(self, s: str) -> int:
                out.append(s)
                return len(s)

            def flush(self) -> None:
                pass

        self.monkeypatch.setattr(sys, "stdout", _Out())
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        rc = hk.pre_tool_use(cast("argparse.Namespace", None))
        line = next(chunk for chunk in out if "hookSpecificOutput" in chunk)
        return rc, json.loads(line)["hookSpecificOutput"]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    return _Harness(monkeypatch, tmp_path)


def nested(depth: int, name: str = "Mcp") -> str:
    deep = "[" * depth + "]" * depth
    return f'{{"tool_name":"{name}","tool_input":{{"command":"rm -rf /","x":{deep}}}}}'


def degraded_reason(agent: str = "messenger-agent") -> str:
    return str(json.loads(_state._gate_degraded_file(agent).read_text())["reason"])


@pytest.mark.parametrize("depth", [50, 900, 1100, 5000, 200_000])
def test_any_depth_reaches_the_guard_and_its_deny_stands(gate: _Harness, depth: int) -> None:
    rc, out = gate.run(nested(depth))
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "GUARD-DENY"
    assert len(gate.asked) == 1
    assert gate.asked[0]["tool_name"] == "Mcp"


@pytest.mark.parametrize("depth", [1100, 200_000])
def test_the_guard_can_allow_a_call_the_gate_could_not_read_in_full(
    gate: _Harness, depth: int
) -> None:
    gate.answer(ALLOW)
    rc, out = gate.run(nested(depth))
    assert rc == 0
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == "GUARD-ALLOW"


@pytest.mark.parametrize("depth", [520, 700, 900])
def test_a_call_past_the_depth_limit_is_flagged_on_every_python(gate: _Harness, depth: int) -> None:
    gate.run(nested(depth))
    sent = gate.asked[0]
    assert sent["truncated"] is True
    assert "unreadable_tool_call" not in sent["tool_input"]


def test_a_call_too_large_to_send_whole_is_flagged(gate: _Harness) -> None:
    gate.run(nested(200_000))
    assert gate.asked[0]["truncated"] is True


@pytest.mark.parametrize("depth", [520, 900, 1100, 200_000])
def test_a_deep_call_keeps_its_command(gate: _Harness, depth: int) -> None:
    gate.run(nested(depth))
    assert "rm -rf /" in json.dumps(gate.asked[0]["tool_input"])


def test_a_call_within_the_depth_limit_is_sent_whole(gate: _Harness) -> None:
    gate.run(nested(50))
    sent = gate.asked[0]
    assert "truncated" not in sent
    assert sent["tool_input"] == json.loads(nested(50))["tool_input"]


def test_the_depth_limit_is_exact() -> None:
    def deep(levels: int) -> Any:
        value: Any = []
        for _ in range(levels - 1):
            value = [value]
        return value

    limit = _gate_input.GUARD_DEPTH_LIMIT
    assert _gate.fit_to_guard_limit(deep(limit)) == deep(limit)
    assert _gate.fit_to_guard_limit(deep(limit + 1)) != deep(limit + 1)


def test_the_depth_limit_applies_to_dicts_and_lists_alike() -> None:
    value: Any = "leaf"
    for level in range(_gate_input.GUARD_DEPTH_LIMIT + 8):
        value = {"k": value} if level % 2 else [value]
    fitted = _gate.fit_to_guard_limit(value)
    for _ in range(_gate_input.GUARD_DEPTH_LIMIT):
        fitted = fitted["k"] if isinstance(fitted, dict) else fitted[0]
    assert isinstance(fitted, str)
    assert "nesting past" in fitted


def test_the_fit_leaves_the_caller_value_alone() -> None:
    long = "x" * (_gate_input.GUARD_FIELD_LIMIT + 1)
    value = {"a": [long], "b": {"c": long}, "n": 1, "z": None}
    fitted = _gate.fit_to_guard_limit(value)
    assert value == {"a": [long], "b": {"c": long}, "n": 1, "z": None}
    assert list(fitted) == ["a", "b", "n", "z"]
    assert fitted["a"][0] != long
    assert fitted["b"]["c"] != long
    assert (fitted["n"], fitted["z"]) == (1, None)


@pytest.mark.parametrize("depth", [1100, 200_000])
def test_a_guard_outage_on_an_unreadable_call_warns_and_records(gate: _Harness, depth: int) -> None:
    gate.reachable = (False, "ConnectionRefusedError: refused")
    rc, out = gate.run(nested(depth))
    assert rc == 0
    assert out["permissionDecision"] == "allow"
    assert "UNVETTED" in out["permissionDecisionReason"]
    assert degraded_reason() == "connect_failure"
    assert gate.asked == []


@pytest.mark.parametrize(
    "stdin",
    ["{not json", '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"', "[1, 2]"],
)
def test_input_that_does_not_decode_is_delivered_as_text(gate: _Harness, stdin: str) -> None:
    rc, out = gate.run(stdin)
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert gate.asked[0]["tool_input"] == {_gate_input.UNREADABLE_CALL: stdin}


def test_the_tool_name_survives_a_decode_failure(gate: _Harness) -> None:
    gate.run('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"')
    assert gate.asked[0]["tool_name"] == "Bash"


def test_an_integer_too_long_to_decode_does_not_hide_the_command(gate: _Harness) -> None:
    digits = "1" * 5000
    stdin = f'{{"tool_name":"Bash","tool_input":{{"command":"rm -rf /","n":{digits}}}}}'
    rc, out = gate.run(stdin)
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert gate.asked[0]["tool_name"] == "Bash"
    assert "rm -rf /" in json.dumps(gate.asked[0]["tool_input"])


def test_unreadable_stdin_is_still_put_to_the_guard(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BadStdin(io.StringIO):
        def read(self, *_a: Any) -> str:
            encoding = "utf-8"
            raise UnicodeDecodeError(encoding, b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(sys, "stdin", _BadStdin())
    tool_name, tool_input = _gate_input._read_tool_call()
    assert tool_name == ""
    assert tool_input == {
        _gate_input.UNREADABLE_CALL: "stdin could not be read: UnicodeDecodeError"
    }


SETTINGS = [
    "AGENTBUS_GATE_TIMEOUT",
    "AGENTBUS_GATE_CONNECT_TIMEOUT",
    "AGENTBUS_GATE_FAST_FAIL_AFTER",
    "AGENTBUS_GATE_FAST_FAIL_COOLDOWN",
]


@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-1", "1e30"])
def test_a_bad_setting_still_asks_the_guard_and_says_so(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch, setting: str, bad: str
) -> None:
    monkeypatch.setenv(setting, bad)
    rc, out = gate.run(PLAIN)
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert len(gate.asked) == 1
    assert out["permissionDecisionReason"].startswith("GUARD-DENY")
    assert setting in out["permissionDecisionReason"]


def test_a_fractional_fast_fail_count_is_a_bad_setting(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_GATE_FAST_FAIL_AFTER", "2.5")
    rc, out = gate.run(PLAIN)
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert "AGENTBUS_GATE_FAST_FAIL_AFTER" in out["permissionDecisionReason"]


@pytest.mark.parametrize("setting", ["AGENTBUS_GATE_TIMEOUT", "AGENTBUS_GATE_CONNECT_TIMEOUT"])
def test_a_timeout_of_zero_would_switch_the_guard_off_so_it_is_refused(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv(setting, "0")
    _rc, out = gate.run(PLAIN)
    assert setting in out["permissionDecisionReason"]


def test_a_bad_setting_does_not_count_as_a_gate_failure(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_GATE_TIMEOUT", "abc")
    gate.run(PLAIN)
    assert not _state._gate_degraded_file("messenger-agent").exists()


def test_an_empty_setting_means_unset_and_gets_no_warning(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_GATE_TIMEOUT", "")
    _rc, out = gate.run(PLAIN)
    assert out["permissionDecisionReason"] == "GUARD-DENY"
    assert gate.timeouts == [4.0]


def test_a_good_setting_takes_effect_with_no_warning(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_GATE_TIMEOUT", "2.5")
    _rc, out = gate.run(PLAIN)
    assert out["permissionDecisionReason"] == "GUARD-DENY"
    assert gate.timeouts == [2.5]


def test_a_warning_from_one_call_does_not_leak_into_the_next(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_GATE_TIMEOUT", "abc")
    gate.run(PLAIN)
    monkeypatch.delenv("AGENTBUS_GATE_TIMEOUT")
    _rc, out = gate.run(PLAIN)
    assert out["permissionDecisionReason"] == "GUARD-DENY"


def test_an_unforeseen_crash_runs_the_call_loudly_and_records_it(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> None:
        msg = "unforeseen"
        raise RuntimeError(msg)

    monkeypatch.setattr(_gate, "_consult_guard", _boom)
    rc, out = gate.run(PLAIN)
    assert rc == 0
    assert out["permissionDecision"] == "allow"
    assert "UNVETTED" in out["permissionDecisionReason"]
    assert "RuntimeError" in out["permissionDecisionReason"]
    assert degraded_reason() == "gate_error"


def test_the_gate_exits_zero_when_it_cannot_even_print_a_decision(
    gate: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Gone:
        def write(self, _s: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _Gone())
    monkeypatch.setattr(sys, "stdin", io.StringIO(PLAIN))
    assert hk.pre_tool_use(cast("argparse.Namespace", None)) == 0


@pytest.mark.parametrize(
    "body", [{}, {"reason": "no decision here"}, {"decision": "maybe"}, {"decision": None}]
)
def test_an_answer_with_no_verdict_never_blocks(gate: _Harness, body: dict[str, Any]) -> None:
    gate.answer(body)
    rc, out = gate.run(PLAIN)
    assert rc == 0
    assert out["permissionDecision"] == "allow"
    assert "UNVETTED" in out["permissionDecisionReason"]
    assert _state._gate_degraded_file("messenger-agent").exists()


def test_a_verified_deny_is_the_one_thing_that_blocks(gate: _Harness) -> None:
    gate.answer(DENY)
    _rc, out = gate.run(PLAIN)
    assert out["permissionDecision"] == "deny"
    gate.answer(ALLOW)
    _rc, out = gate.run(PLAIN)
    assert out["permissionDecision"] == "allow"


@pytest.mark.parametrize(
    "base",
    ["file:///etc/passwd", "ftp://127.0.0.1/x", "gopher://127.0.0.1/x", "127.0.0.1:8080"],
)
def test_a_base_url_that_is_not_http_never_reaches_urlopen(base: str) -> None:
    reachable, why = _gate._bus_reachable(base, 0.05)
    assert reachable is False
    assert "AGENTBUS_BASE_URL" in why


@pytest.mark.parametrize("base", ["http://x:notaport/", "http://[bad"])
def test_a_base_url_that_does_not_parse_is_unreachable_not_a_crash(base: str) -> None:
    reachable, why = _gate._bus_reachable(base, 0.05)
    assert reachable is False
    assert "AGENTBUS_BASE_URL" in why


@pytest.mark.parametrize("state", ["[1, 2]", '"text"', "7", "null", '{"count": "many"}'])
def test_a_damaged_state_file_does_not_stop_the_degraded_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    monkeypatch.setattr(_state, "_hook_state_dir", lambda: tmp_path)
    path = _state._gate_degraded_file("state-agent")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state)
    _state.record_gate_degraded("state-agent", "connect_failure", "refused")
    written = json.loads(path.read_text())
    assert written["reason"] == "connect_failure"
    assert written["count"] == 1
