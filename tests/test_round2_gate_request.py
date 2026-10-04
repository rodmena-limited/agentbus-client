from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk
from agentbus_client.hooks import _gate


class _Resp:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode()
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class _Rig:
    def __init__(self, verdict: Any) -> None:
        self.verdict = verdict
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []
        self.probes: list[tuple[Any, ...]] = []
        self.out: list[str] = []

    def urlopen(self, request: Any, timeout: Any = None) -> _Resp:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return _Resp(self.verdict)

    def reachable(self, *args: Any) -> tuple[bool, str]:
        self.probes.append(args)
        return True, ""

    def body(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].data.decode())

    def decision(self) -> dict[str, Any]:
        line = next(chunk for chunk in self.out if "hookSpecificOutput" in chunk)
        return dict(json.loads(line))


def _install(
    monkeypatch: pytest.MonkeyPatch, verdict: Any, stdin: str, env: dict[str, str]
) -> _Rig:
    rig = _Rig(verdict)

    class _Out:
        def write(self, s: str) -> int:
            rig.out.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(_gate, "_bus_reachable", rig.reachable)
    monkeypatch.setattr(_gate.urllib.request, "urlopen", rig.urlopen)
    for key in (
        "AGENTBUS_GATE_TIMEOUT",
        "AGENTBUS_GATE_CONNECT_TIMEOUT",
        "AGENTBUS_GATE_FAST_FAIL_AFTER",
        "AGENTBUS_GATE_FAST_FAIL_COOLDOWN",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "stdout", _Out())
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return rig


WIRED = {
    "AGENTBUS_API_KEY": "ab_sk_request",
    "AGENTBUS_AGENT": "request-agent",
    "AGENTBUS_BASE_URL": "https://bus.example",
}
BASH_LS = '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}'
DENY = {"decision": "deny", "reason": "GUARD-DENY: rm needs approval (futex ticket 42)"}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    verdict: Any = None,
    stdin: str = BASH_LS,
    **env: str,
) -> _Rig:
    rig = _install(monkeypatch, DENY if verdict is None else verdict, stdin, {**WIRED, **env})
    assert hk.pre_tool_use(None) == 0
    return rig


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("https://bus.example", "https://bus.example/v1/guard/check"),
        ("https://bus.example/", "https://bus.example/v1/guard/check"),
        ("http://127.0.0.1:8099", "http://127.0.0.1:8099/v1/guard/check"),
        ("https://bus.example/prefix/", "https://bus.example/prefix/v1/guard/check"),
    ],
)
def test_the_guard_request_goes_to_the_check_endpoint_of_the_configured_base(
    monkeypatch: pytest.MonkeyPatch, base: str, url: str
) -> None:
    rig = _run(monkeypatch, AGENTBUS_BASE_URL=base)
    assert [r.full_url for r in rig.requests] == [url]
    assert rig.probes == [(base, 1.5)]


def test_the_default_base_is_the_production_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _install(
        monkeypatch, DENY, BASH_LS, {k: v for k, v in WIRED.items() if k != "AGENTBUS_BASE_URL"}
    )
    monkeypatch.delenv("AGENTBUS_BASE_URL", raising=False)
    assert hk.pre_tool_use(None) == 0
    assert [r.full_url for r in rig.requests] == ["https://agentbus.rodmena.co.uk/v1/guard/check"]
    assert rig.probes == [("https://agentbus.rodmena.co.uk", 1.5)]


def test_the_guard_request_is_an_authenticated_json_post(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _run(monkeypatch)
    assert len(rig.requests) == 1
    request = rig.requests[0]
    assert request.get_method() == "POST"
    assert dict(request.header_items()) == {
        "Content-type": "application/json",
        "Authorization": "Bearer ab_sk_request",
        "X-agentbus-agent": "request-agent",
    }
    assert rig.body() == {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}


@pytest.mark.parametrize(
    ("stdin", "sent"),
    [
        (
            '{"tool_name":"Write","tool_input":{"file_path":"/etc/x","content":"y"}}',
            {"tool_name": "Write", "tool_input": {"file_path": "/etc/x", "content": "y"}},
        ),
        ('{"tool_input":{"command":"ls"}}', {"tool_name": "", "tool_input": {"command": "ls"}}),
        ('{"tool_name":"Read"}', {"tool_name": "Read", "tool_input": None}),
        ("not json{", {"tool_name": "", "tool_input": None}),
        ("", {"tool_name": "", "tool_input": None}),
    ],
)
def test_the_body_carries_tool_name_and_tool_input_from_the_hook_input(
    monkeypatch: pytest.MonkeyPatch, stdin: str, sent: dict[str, Any]
) -> None:
    rig = _run(monkeypatch, stdin=stdin)
    assert rig.body() == sent
    assert rig.decision()["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(("env", "expected"), [({}, 4.0), ({"AGENTBUS_GATE_TIMEOUT": "2.5"}, 2.5)])
def test_the_read_timeout_honours_the_env_override(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: float
) -> None:
    rig = _run(monkeypatch, **env)
    assert rig.timeouts == [expected]


def test_the_connect_timeout_honours_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _run(monkeypatch, AGENTBUS_GATE_CONNECT_TIMEOUT="0.3")
    assert rig.probes == [("https://bus.example", 0.3)]


def test_a_deny_is_emitted_with_the_servers_reason_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _run(monkeypatch)
    assert rig.decision() == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "GUARD-DENY: rm needs approval (futex ticket 42)",
        }
    }


def test_an_allow_is_emitted_with_the_servers_reason_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _run(monkeypatch, {"decision": "allow", "reason": "rule 7: read-only command"})
    assert rig.decision() == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "rule 7: read-only command",
        }
    }


@pytest.mark.parametrize(
    ("verdict", "decision", "reason"),
    [
        ({"decision": "deny"}, "deny", "this action requires human approval"),
        ({"decision": "allow"}, "allow", "permitted"),
        ({"decision": "maybe", "reason": "odd"}, "deny", "odd"),
        ({}, "deny", "this action requires human approval"),
    ],
)
def test_a_verdict_without_a_reason_gets_the_documented_default(
    monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any], decision: str, reason: str
) -> None:
    out = _run(monkeypatch, verdict).decision()["hookSpecificOutput"]
    assert out["permissionDecision"] == decision
    assert out["permissionDecisionReason"] == reason


def test_main_pre_tool_use_exits_zero_on_a_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _install(monkeypatch, DENY, BASH_LS, WIRED)
    assert hk.main(["pre-tool-use"]) == 0
    out = rig.decision()["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert len(rig.requests) == 1


def test_a_real_verdict_clears_the_degraded_record(monkeypatch: pytest.MonkeyPatch) -> None:
    hk.record_gate_degraded("request-agent", "httperror", "HTTP Error 503")
    state = hk._gate_degraded_file("request-agent")
    assert state.exists()
    _run(monkeypatch)
    assert not state.exists()


@pytest.fixture
def unwired_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "unwired-project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


@pytest.mark.parametrize(
    "env",
    [
        {"AGENTBUS_API_KEY": "ab_sk_orphan"},
        {"AGENTBUS_AGENT": "lonely-agent"},
        {},
    ],
)
def test_a_session_missing_key_or_identity_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch, unwired_dir: Path, env: dict[str, str]
) -> None:
    for key in ("AGENTBUS_API_KEY", "AGENTBUS_AGENT"):
        monkeypatch.delenv(key, raising=False)
    rig = _install(monkeypatch, DENY, BASH_LS, {"AGENTBUS_BASE_URL": "https://bus.example", **env})
    assert hk.pre_tool_use(None) == 0
    assert rig.requests == []
    assert rig.probes == []
    assert rig.decision()["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "this project has not opted into AgentBus; ungated",
    }


def test_an_opted_in_project_without_a_key_degrades_loudly_without_asking(
    monkeypatch: pytest.MonkeyPatch, unwired_dir: Path
) -> None:
    (unwired_dir / ".claude").mkdir()
    (unwired_dir / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "settings-agent"}})
    )
    for key in ("AGENTBUS_API_KEY", "AGENTBUS_AGENT"):
        monkeypatch.delenv(key, raising=False)
    rig = _install(monkeypatch, DENY, BASH_LS, {"AGENTBUS_BASE_URL": "https://bus.example"})
    assert hk.pre_tool_use(None) == 0
    assert rig.requests == []
    assert rig.probes == []
    out = rig.decision()["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == (
        "this project is wired for AgentBus but this session has no credential, so the guard "
        "CANNOT check whether this action needs approval. The action runs UNVETTED. This is a "
        "degraded session, not a protected one. To restore gating, source the agent's key "
        "file (agentbus signin) and re-run."
    )
    state = json.loads(hk._gate_degraded_file("settings-agent").read_text())
    assert state["reason"] == "no_credential"
    assert state["count"] == 1


LIMIT = _gate.GUARD_FIELD_LIMIT


def test_the_guard_field_limit_is_the_servers_4096() -> None:
    assert LIMIT == 4096


@pytest.mark.parametrize("size", [0, 1, LIMIT - 1, LIMIT])
def test_a_field_within_the_limit_passes_unchanged(size: int) -> None:
    value = "x" * size
    assert _gate.fit_to_guard_limit(value) == value
    assert _gate.fit_to_guard_limit({"a": [value]}) == {"a": [value]}


@pytest.mark.parametrize("extra", [1, 97, 5000])
def test_a_field_over_the_limit_keeps_head_and_tail_and_counts_the_elision(extra: int) -> None:
    middle = LIMIT + extra - 4000
    value = "H" * 2000 + "M" * middle + "T" * 2000
    marker = f"\n...[{middle} characters elided by the AgentBus gate]...\n"
    assert _gate.fit_to_guard_limit(value) == "H" * 2000 + marker + "T" * 2000
    assert _gate.fit_to_guard_limit([1, {"k": value}, None]) == [
        1,
        {"k": "H" * 2000 + marker + "T" * 2000},
        None,
    ]


def test_an_exact_limit_field_is_sent_whole_and_not_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = "c" * LIMIT
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    rig = _run(monkeypatch, stdin=stdin)
    assert rig.body() == {"tool_name": "Bash", "tool_input": {"command": command}}


def test_a_field_one_over_the_limit_is_elided_and_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = "H" * 2000 + "M" * 97 + "T" * 2000
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    rig = _run(monkeypatch, stdin=stdin)
    assert rig.body() == {
        "tool_name": "Bash",
        "tool_input": {
            "command": "H" * 2000
            + "\n...[97 characters elided by the AgentBus gate]...\n"
            + "T" * 2000
        },
        "truncated": True,
    }
