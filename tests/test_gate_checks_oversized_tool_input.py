from __future__ import annotations

import io
import json
import sys
import urllib.error
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk
from agentbus_client.hooks import _gate

LIMIT = 4096


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


class _HTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str) -> None:
        super().__init__("https://agentbus.rodmena.co.uk/v1/guard/check", code, "x", None, None)
        self._body = body

    def read(self) -> bytes:
        return self._body.encode()


def _longest_string(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return max((_longest_string(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return max((_longest_string(v) for v in value), default=0)
    return 0


def _guard_with_the_servers_contract(sent: list[dict[str, Any]]):
    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data.decode())
        sent.append(body)
        longest = _longest_string(body["tool_input"])
        if longest > LIMIT:
            raise _HTTPError(
                422,
                json.dumps(
                    {
                        "code": "validation_error",
                        "detail": f"tool_input: a field is {longest} characters, over the {LIMIT}",
                    }
                ),
            )
        if "rm -rf /" in json.dumps(body["tool_input"]):
            return _Resp({"decision": "deny", "reason": "destructive command needs approval"})
        return _Resp({"decision": "allow", "reason": "permitted"})

    return fake_urlopen


def _decide(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(hk.urllib.request, "urlopen", _guard_with_the_servers_contract(sent))
    monkeypatch.setattr(_gate, "_bus_reachable", lambda *_a, **_k: (True, ""))
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_ok")
    monkeypatch.setenv("AGENTBUS_AGENT", "wired-agent")
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out: list[str] = []

    class _Out:
        def write(self, text: str) -> int:
            out.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", _Out())
    hk.pre_tool_use(None)
    line = next(chunk for chunk in out if "hookSpecificOutput" in chunk)
    return json.loads(line)["hookSpecificOutput"], sent


def test_a_padded_destructive_command_is_still_checked_and_denied(monkeypatch):
    decision, sent = _decide(monkeypatch, "echo " + "x" * 10_000 + " ; rm -rf /")
    assert decision["permissionDecision"] == "deny", decision
    assert _longest_string(sent[-1]["tool_input"]) <= LIMIT


def test_a_small_destructive_command_is_denied(monkeypatch):
    decision, _ = _decide(monkeypatch, "rm -rf /")
    assert decision["permissionDecision"] == "deny"


def test_a_large_harmless_command_gets_a_real_verdict_not_a_degraded_allow(monkeypatch):
    decision, _ = _decide(monkeypatch, "echo " + "y" * 20_000)
    assert decision["permissionDecision"] == "allow"
    assert "UNVETTED" not in decision["permissionDecisionReason"]


def test_a_field_under_the_limit_is_sent_unchanged(monkeypatch):
    command = "echo " + "z" * 3_000
    _, sent = _decide(monkeypatch, command)
    assert sent[-1]["tool_input"]["command"] == command


def test_shortening_keeps_both_ends_and_names_the_gap():
    text = "HEAD" + "m" * 9_000 + "TAIL"
    short = _gate.fit_to_guard_limit({"a": [text]})["a"][0]
    assert len(short) <= LIMIT
    assert short.startswith("HEAD") and short.endswith("TAIL")
    assert "characters elided by the AgentBus gate" in short
