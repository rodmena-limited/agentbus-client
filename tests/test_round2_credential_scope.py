from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agentbus_client.client import sync_client
from agentbus_client.onboarding import _credentials

FLAG = {
    "send": "  — FINDING: reachable by inheritance; a send-scope key in an inherited slot "
    "can act as any agent it is not bound to — this slot wants `read`, nothing above",
    "full": "  — FINDING: reachable by inheritance; a `full` key here can MINT a bound key "
    "for any agent and send at platform_attested — this slot wants `read`, nothing above",
    "admin": "  — FINDING: reachable by inheritance; a `admin` key here is worse than full: "
    "it can revoke and purge, and is inherited by every unwired directory",
}
BASE = "http://bus.test:8080"


class FakeBus:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}
        self.default = httpx.Response(200, json={"key": {"scope": "read"}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        return self.responses.get(token, self.default)


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> FakeBus:
    fake = FakeBus()
    real_client = httpx.Client

    def _client(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(sync_client.httpx, "Client", _client)
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", "0")
    return fake


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _claude_json(home: Path, header: str) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"agentbus": {"headers": {"Authorization": header}}}})
    )


def _opencode(home: Path, name: str, header: str) -> None:
    target = home / ".config" / "opencode" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"mcp": {"agentbus": {"type": "remote", "headers": {"Authorization": header}}}})
    )


def _settings(root: Path, agent: str) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": agent}})
    )


@pytest.mark.parametrize("scope", ["send", "full", "admin"])
def test_inherited_flag_names_the_escalation_exactly(scope: str) -> None:
    assert _credentials._inherited_flag(scope) == FLAG[scope]


@pytest.mark.parametrize(
    "scope", ["read", "unknown", "unusable (invalid_api_key)", "", "FULL", "Send"]
)
def test_inherited_flag_is_silent_below_send(scope: str) -> None:
    assert _credentials._inherited_flag(scope) == ""


@pytest.mark.parametrize("scope", ["read", "send", "full", "admin"])
def test_scope_comes_from_whoami_at_the_given_base(bus: FakeBus, scope: str) -> None:
    bus.default = httpx.Response(200, json={"key": {"scope": scope}, "agent": None})
    assert _credentials._scope_of_bearer("Bearer ab_sk_tok_1", BASE) == scope
    [request] = bus.requests
    assert str(request.url) == f"{BASE}/v1/whoami"
    assert request.method == "GET"
    assert request.headers["Authorization"] == "Bearer ab_sk_tok_1"
    assert "X-AgentBus-Agent" not in request.headers


def test_no_base_url_asks_the_default_bus(bus: FakeBus) -> None:
    assert _credentials._scope_of_bearer("Bearer ab_sk_tok_1") == "read"
    assert str(bus.requests[0].url) == "https://agentbus.rodmena.co.uk/v1/whoami"


def test_only_the_ab_sk_token_is_sent(bus: FakeBus) -> None:
    _credentials._scope_of_bearer("Bearer  ab_sk_Ab9_z-trailing junk", BASE)
    assert bus.requests[0].headers["Authorization"] == "Bearer ab_sk_Ab9_z"


@pytest.mark.parametrize("header", ["", "Bearer sk_live_nope", "Bearer ab_sk_", "Basic xyz"])
def test_a_header_without_an_ab_sk_token_is_unknown_and_never_sent(
    bus: FakeBus, header: str
) -> None:
    assert _credentials._scope_of_bearer(header, BASE) == "unknown"
    assert bus.requests == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"key": None}, {"key": {}}, {"key": {"scope": 5}}, {"key": {"scope": None}}],
)
def test_a_whoami_without_a_string_scope_is_unknown(bus: FakeBus, payload: object) -> None:
    bus.default = httpx.Response(200, json=payload)
    assert _credentials._scope_of_bearer("Bearer ab_sk_x", BASE) == "unknown"


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "invalid_api_key", "unusable (invalid_api_key)"),
        (403, "forbidden", "unusable (forbidden)"),
        (404, "not_found", "unusable (not_found)"),
        (500, "internal", "unreachable (internal)"),
        (503, "unavailable", "unreachable (unavailable)"),
        (401, "expired", "unreachable (expired)"),
    ],
)
def test_server_refusals_are_reported_honestly(
    bus: FakeBus, status: int, code: str, expected: str
) -> None:
    bus.default = httpx.Response(status, json={"code": code, "detail": "no"})
    assert _credentials._scope_of_bearer("Bearer ab_sk_x", BASE) == expected


def test_a_dead_bus_is_unreachable(monkeypatch: pytest.MonkeyPatch, bus: FakeBus) -> None:
    def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(bus, "handler", _refuse)
    assert _credentials._scope_of_bearer("Bearer ab_sk_x", BASE) == "unreachable (error)"


def test_a_non_json_answer_is_a_resolution_failure(bus: FakeBus) -> None:
    bus.default = httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    assert _credentials._scope_of_bearer("Bearer ab_sk_x", BASE) == "unknown (resolution failed)"


def test_nothing_reachable_is_an_empty_report(bus: FakeBus, project: Path) -> None:
    assert _credentials.doctor_credential_scope(BASE) == []
    assert bus.requests == []


def test_project_wiring_is_named(bus: FakeBus, project: Path) -> None:
    _settings(project, "proj-agent")
    assert _credentials.doctor_credential_scope(BASE) == [
        f"project ({project}/.claude/settings.local.json): agent proj-agent"
    ]


@pytest.mark.parametrize("scope", ["full", "admin", "send"])
def test_an_inherited_claude_key_at_send_or_above_is_a_finding(
    bus: FakeBus, project: Path, tmp_path: Path, scope: str
) -> None:
    bus.default = httpx.Response(200, json={"key": {"scope": scope}})
    _claude_json(tmp_path, "Bearer ab_sk_claude")
    lines = _credentials.doctor_credential_scope(BASE)
    assert lines == [f"user-scope ~/.claude.json agentbus MCP: {scope}" + FLAG[scope]]
    assert "FINDING" in lines[0]
    assert [str(r.url) for r in bus.requests] == [f"{BASE}/v1/whoami"]
    assert bus.requests[0].headers["Authorization"] == "Bearer ab_sk_claude"


def test_an_inherited_read_key_is_not_a_finding(
    bus: FakeBus, project: Path, tmp_path: Path
) -> None:
    _claude_json(tmp_path, "Bearer ab_sk_claude")
    _opencode(tmp_path, "opencode.json", "Bearer ab_sk_oc")
    lines = _credentials.doctor_credential_scope(BASE)
    assert lines == [
        "user-scope ~/.claude.json agentbus MCP: read",
        "opencode opencode.json agentbus MCP: read",
    ]
    assert not any("FINDING" in line for line in lines)


@pytest.mark.parametrize("scope", ["full", "admin"])
def test_an_inherited_opencode_key_is_a_finding(
    bus: FakeBus, project: Path, tmp_path: Path, scope: str
) -> None:
    bus.default = httpx.Response(200, json={"key": {"scope": scope}})
    _opencode(tmp_path, "opencode.json", "Bearer ab_sk_oc")
    assert _credentials.doctor_credential_scope(BASE) == [
        f"opencode opencode.json agentbus MCP: {scope}" + FLAG[scope]
    ]
    assert str(bus.requests[0].url) == f"{BASE}/v1/whoami"


def test_opencode_jsonc_is_read_in_preference_to_json(
    bus: FakeBus, project: Path, tmp_path: Path
) -> None:
    bus.responses["ab_sk_jsonc"] = httpx.Response(200, json={"key": {"scope": "full"}})
    _opencode(tmp_path, "opencode.jsonc", "Bearer ab_sk_jsonc")
    _opencode(tmp_path, "opencode.json", "Bearer ab_sk_json")
    assert _credentials.doctor_credential_scope(BASE) == [
        "opencode opencode.jsonc agentbus MCP: full" + FLAG["full"]
    ]
    assert [r.headers["Authorization"] for r in bus.requests] == ["Bearer ab_sk_jsonc"]


def test_each_slot_is_resolved_with_its_own_key(
    bus: FakeBus, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus.responses["ab_sk_claude"] = httpx.Response(200, json={"key": {"scope": "admin"}})
    bus.responses["ab_sk_oc"] = httpx.Response(401, json={"code": "invalid_api_key"})
    _settings(project, "proj-agent")
    _claude_json(tmp_path, "Bearer ab_sk_claude")
    _opencode(tmp_path, "opencode.json", "Bearer ab_sk_oc")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "operator.env").write_text("AGENTBUS_API_KEY=ab_sk_op\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    assert _credentials.doctor_credential_scope(BASE) == [
        f"project ({project}/.claude/settings.local.json): agent proj-agent",
        "user-scope ~/.claude.json agentbus MCP: admin" + FLAG["admin"],
        "opencode opencode.json agentbus MCP: unusable (invalid_api_key)",
        f"operator: {cfg / 'operator.env'} — full (operator.env: can MINT — never auto-inherit it)",
    ]
    assert [r.headers["Authorization"] for r in bus.requests] == [
        "Bearer ab_sk_claude",
        "Bearer ab_sk_oc",
    ]
    assert {str(r.url) for r in bus.requests} == {f"{BASE}/v1/whoami"}


def test_entries_without_an_agentbus_server_are_skipped(
    bus: FakeBus, project: Path, tmp_path: Path
) -> None:
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
    target = tmp_path / ".config" / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcp": {"other": {}}}))
    assert _credentials.doctor_credential_scope(BASE) == []
    assert bus.requests == []


def test_an_unparseable_claude_json_is_reported_not_omitted(
    bus: FakeBus, project: Path, tmp_path: Path
) -> None:
    (tmp_path / ".claude.json").write_text(
        '{"mcpServers": {"agentbus": {"headers": {"Authorization": "Bearer ab_sk_x"}},},}'
    )
    lines = _credentials.doctor_credential_scope(BASE)
    assert any(line.startswith("user-scope ~/.claude.json") for line in lines), lines


def _keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *names: str) -> Path:
    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    for name in names:
        (cfg / "keys" / f"{name}.env").write_text("AGENTBUS_API_KEY=ab_sk_peer\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    return cfg / "keys"


def _hint(keys: Path, peer: str) -> str:
    return (
        f"  * simplest, and needs no credential: run from {peer}'s own project directory, "
        "or wire this one with `agentbus setup claude --role <role>`\n"
        "  * OPERATOR ONLY (a human at a terminal — not an agent, and not something to go "
        f"hunting for): source that agent's own key file, {keys}/{peer}.env"
    )


def test_explain_refusal_is_none_without_a_peer_or_its_key(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keys(monkeypatch, tmp_path, "peer")
    assert _credentials.explain_refusal(None) is None
    assert _credentials.explain_refusal("") is None
    assert _credentials.explain_refusal("stranger") is None


def test_explain_refusal_with_no_session_identity(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = _keys(monkeypatch, tmp_path, "peer")
    assert _credentials.explain_refusal("peer") == (
        "cannot act as 'peer': this session has NO recorded identity — no AGENTBUS_AGENT "
        "in this project's .claude/settings.local.json and no signin default — so there is "
        "nothing to check 'peer' against.\n"
        "This is NOT a claim that you are not peer. It is that nothing here says who you are.\n"
        "  * if this is your project: run `agentbus setup claude` in it\n"
        "  * if you have signed in on this machine: run from the project directory, or "
        "re-run `agentbus signin <key>`\n" + _hint(keys, "peer")
    )


def test_explain_refusal_names_the_session_it_is_not(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = _keys(monkeypatch, tmp_path, "peer")
    _settings(project, "me")
    assert _credentials.explain_refusal("peer") == (
        "refusing to act as 'peer': that agent has a bound key on this machine, but it is "
        "not this session's identity (this session is 'me'), and --agent says WHICH AGENT "
        "TO ACT AS, not 'load that agent's credential'.\n" + _hint(keys, "peer")
    )


def test_fake_bus_answers_a_known_positive(bus: FakeBus) -> None:
    bus.default = httpx.Response(200, json={"key": {"scope": "full"}})
    assert _credentials._scope_of_bearer("Bearer ab_sk_known") == "full"
    assert len(bus.requests) == 1
