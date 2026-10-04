from __future__ import annotations

import io
import json
import socket
import sys
from typing import Any

import pytest

import agentbus_client.hooks.claude_code as hk
from agentbus_client.hooks import _gate


class _Conn:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Dialer:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.conns: list[_Conn] = []
        self.error = error

    def __call__(self, address: Any, timeout: Any = None, *rest: Any, **kw: Any) -> _Conn:
        self.calls.append((address, timeout))
        if self.error is not None:
            raise self.error
        conn = _Conn()
        self.conns.append(conn)
        return conn


@pytest.fixture
def listener() -> Any:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv
    srv.close()


@pytest.fixture
def closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


def test_a_listening_local_socket_is_reachable(listener: Any) -> None:
    port = listener.getsockname()[1]
    assert _gate._bus_reachable(f"http://127.0.0.1:{port}", 1.0) == (True, "")


def test_a_listening_local_socket_is_reachable_with_a_trailing_path(listener: Any) -> None:
    port = listener.getsockname()[1]
    assert _gate._bus_reachable(f"https://127.0.0.1:{port}/", 1.0) == (True, "")


def test_a_closed_local_port_is_unreachable(closed_port: int) -> None:
    reachable, why = _gate._bus_reachable(f"http://127.0.0.1:{closed_port}", 1.0)
    assert reachable is False
    assert why == "ConnectionRefusedError: [Errno 111] Connection refused"


@pytest.mark.parametrize(
    ("base", "address"),
    [
        ("https://bus.example", ("bus.example", 443)),
        ("https://bus.example/", ("bus.example", 443)),
        ("HTTPS://bus.example", ("bus.example", 443)),
        ("http://bus.example", ("bus.example", 80)),
        ("http://bus.example/", ("bus.example", 80)),
        ("https://bus.example:8443", ("bus.example", 8443)),
        ("http://bus.example:8080/v1", ("bus.example", 8080)),
        ("https://BUS.Example", ("bus.example", 443)),
    ],
)
def test_the_probe_dials_the_scheme_default_or_explicit_port(
    monkeypatch: pytest.MonkeyPatch, base: str, address: tuple[str, int]
) -> None:
    dialer = _Dialer()
    monkeypatch.setattr(socket, "create_connection", dialer)
    assert _gate._bus_reachable(base, 1.25) == (True, "")
    assert dialer.calls == [(address, 1.25)]
    assert [c.closed for c in dialer.conns] == [1]


@pytest.mark.parametrize("base", ["not-a-url", "", "https://", "http:///v1/guard"])
def test_a_base_without_a_host_is_unreachable_and_never_dials(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    dialer = _Dialer()
    monkeypatch.setattr(socket, "create_connection", dialer)
    assert _gate._bus_reachable(base, 1.0) == (False, f"no host in AGENTBUS_BASE_URL {base!r}")
    assert dialer.calls == []


@pytest.mark.parametrize(
    ("error", "why"),
    [
        (OSError("boom"), "OSError: boom"),
        (TimeoutError("timed out"), "TimeoutError: timed out"),
        (
            ConnectionRefusedError(111, "Connection refused"),
            "ConnectionRefusedError: [Errno 111] Connection refused",
        ),
        (
            socket.gaierror(-2, "Name or service not known"),
            "gaierror: [Errno -2] Name or service not known",
        ),
    ],
)
def test_a_connect_error_is_reported_unreachable_with_its_class(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, why: str
) -> None:
    dialer = _Dialer(error)
    monkeypatch.setattr(socket, "create_connection", dialer)
    assert _gate._bus_reachable("https://bus.example", 1.0) == (False, why)
    assert dialer.calls == [(("bus.example", 443), 1.0)]


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


def _run_real_probe(
    monkeypatch: pytest.MonkeyPatch, base: str, urls: list[str], **env: str
) -> dict[str, Any]:
    out: list[str] = []

    class _Out:
        def write(self, s: str) -> int:
            out.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    def _urlopen(request: Any, timeout: Any = None) -> _Resp:
        urls.append(request.full_url)
        return _Resp({"decision": "deny", "reason": "GUARD-DENY probe"})

    monkeypatch.setattr(_gate.urllib.request, "urlopen", _urlopen)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_probe")
    monkeypatch.setenv("AGENTBUS_AGENT", "probe-agent")
    monkeypatch.setenv("AGENTBUS_BASE_URL", base)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "stdout", _Out())
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"tool_name":"Bash","tool_input":{"command":"ls"}}')
    )
    assert hk.pre_tool_use(None) == 0
    line = next(chunk for chunk in out if "hookSpecificOutput" in chunk)
    return dict(json.loads(line)["hookSpecificOutput"])


def test_a_reachable_bus_is_consulted_and_its_deny_enforced(
    monkeypatch: pytest.MonkeyPatch, listener: Any
) -> None:
    port = listener.getsockname()[1]
    urls: list[str] = []
    out = _run_real_probe(monkeypatch, f"http://127.0.0.1:{port}", urls)
    assert out == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "GUARD-DENY probe",
    }
    assert urls == [f"http://127.0.0.1:{port}/v1/guard/check"]


def test_an_unreachable_bus_degrades_without_asking_and_opens_the_circuit(
    monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    urls: list[str] = []
    out = _run_real_probe(monkeypatch, f"http://127.0.0.1:{closed_port}", urls)
    assert out["permissionDecision"] == "allow"
    assert out["permissionDecisionReason"] == (
        "AgentBus is unreachable (ConnectionRefusedError: [Errno 111] Connection refused), "
        "so this action runs UNVETTED — approval checking is OFF until the bus is reachable "
        "again (circuit open for 30s). Restore the network or the bus, then re-run any gated "
        "tool to confirm gating is back on."
    )
    assert urls == []
    state = json.loads(hk._gate_degraded_file("probe-agent").read_text())
    assert state["reason"] == "connect_failure"
    assert state["count"] == 1
    assert state["detail"] == "ConnectionRefusedError: [Errno 111] Connection refused"


def test_the_unreachable_message_names_the_configured_cooldown(
    monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    urls: list[str] = []
    out = _run_real_probe(
        monkeypatch,
        f"http://127.0.0.1:{closed_port}",
        urls,
        AGENTBUS_GATE_FAST_FAIL_COOLDOWN="45",
    )
    assert "(circuit open for 45s)" in out["permissionDecisionReason"]
    assert urls == []


@pytest.mark.parametrize(
    ("env", "expected"), [({}, 1.5), ({"AGENTBUS_GATE_CONNECT_TIMEOUT": "0.7"}, 0.7)]
)
def test_the_hook_passes_the_connect_timeout_to_the_socket(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: float
) -> None:
    monkeypatch.delenv("AGENTBUS_GATE_CONNECT_TIMEOUT", raising=False)
    dialer = _Dialer()
    monkeypatch.setattr(socket, "create_connection", dialer)
    urls: list[str] = []
    out = _run_real_probe(monkeypatch, "https://bus.example", urls, **env)
    assert dialer.calls == [(("bus.example", 443), expected)]
    assert out["permissionDecision"] == "deny"
    assert urls == ["https://bus.example/v1/guard/check"]
