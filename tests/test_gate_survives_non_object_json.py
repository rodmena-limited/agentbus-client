from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk


class _Resp:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


def _run(
    monkeypatch: pytest.MonkeyPatch, stdin: str, guard_body: bytes
) -> tuple[int, dict[str, Any]]:
    out: list[str] = []

    class _Out:
        def write(self, s: str) -> int:
            out.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    requests: list[Any] = []

    def _urlopen(request: Any, timeout: float = 0) -> _Resp:
        requests.append(json.loads(request.data.decode()))
        return _Resp(guard_body)

    monkeypatch.setattr(hk._gate, "_bus_reachable", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(hk._gate.urllib.request, "urlopen", _urlopen)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test")
    monkeypatch.setenv("AGENTBUS_AGENT", "json-agent")
    monkeypatch.setenv("AGENTBUS_BASE_URL", "https://bus.invalid")
    monkeypatch.setattr(sys, "stdout", _Out())
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = hk.pre_tool_use(None)
    line = next(chunk for chunk in out if "hookSpecificOutput" in chunk)
    return rc, json.loads(line)["hookSpecificOutput"]


DENY = json.dumps({"decision": "deny", "reason": "GUARD-DENY"}).encode()


@pytest.mark.parametrize("stdin", ["[1, 2]", '"a string"', "42", "null"])
def test_non_object_stdin_gets_a_decision_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    rc, out = _run(monkeypatch, stdin, DENY)
    assert rc == 0
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"


@pytest.mark.parametrize("body", [b"[]", b'"allow"', b"null", b"7"])
def test_a_non_object_guard_body_degrades_loudly_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    rc, out = _run(monkeypatch, '{"tool_name":"Bash","tool_input":{"command":"ls"}}', body)
    assert rc == 0
    assert out["permissionDecision"] == "allow"
    assert "UNVETTED" in out["permissionDecisionReason"]
    assert "not an object" in out["permissionDecisionReason"]


def test_an_object_guard_body_is_still_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, out = _run(monkeypatch, '{"tool_name":"Bash","tool_input":{"command":"ls"}}', DENY)
    assert rc == 0
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "GUARD-DENY"
