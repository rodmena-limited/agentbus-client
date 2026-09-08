"""#57: `retire <name>` must act AS <name> when we hold its bound key.

THE OPERATOR'S LAST STEP, and it failed:

    rm -rf .agentbus .claude/settings.local.json
    agentbus retire auditor-be8047
    -> permission_denied: an agent may act only on itself; acting as no agent

while ~/.config/agentbus/keys/auditor-be8047.env sat on disk the whole time.
`retire` built its client from the AMBIENT identity — which the operator had
just deleted, deliberately, as part of tearing the project down — and then asked
the server to retire a name that identity had nothing to do with.

Every piece of information needed was present and the command refused anyway.
That is the shape of every identity bug in this release.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBusError


class _Bus:
    def __init__(self, fail_code: str | None = None):
        self.fail_code = fail_code
        self.calls: list[str] = []

    def _request(self, method, path, **kw):
        self.calls.append(path)
        if self.fail_code:
            raise AgentBusError("nope", code=self.fail_code, status=409)
        return {"retired": True}


def _args(name=None, **over):
    base = dict(name=name, agent=None, api_key=None, base_url=None, json=False)
    base.update(over)
    return argparse.Namespace(**base)


def _run(monkeypatch, args, key_for_agent, bus=None):
    bus = bus or _Bus()
    seen: dict = {}

    def fake_bus(a):
        seen["api_key"] = a.api_key
        seen["agent"] = a.agent
        return bus

    monkeypatch.setattr(cli._common, "_bus", fake_bus)
    monkeypatch.setattr(cli._common, "_key_for_agent", key_for_agent)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_retire(args)
    return rc, buf.getvalue(), seen, bus


def test_it_uses_the_named_agents_own_key_when_we_hold_it(monkeypatch):
    """THE REGRESSION. No ambient identity, but the target's key is on disk."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    rc, out, seen, bus = _run(
        monkeypatch,
        _args("auditor-be8047"),
        lambda n: "ab_sk_auditor_key" if n == "auditor-be8047" else None,
    )
    assert rc == 0
    assert seen["api_key"] == "ab_sk_auditor_key", (
        "retire ignored the bound key it holds for the agent it was told to retire"
    )
    assert seen["agent"] == "auditor-be8047"
    assert "/v1/agents/auditor-be8047/retire" in bus.calls


def test_an_agent_whose_key_we_do_not_hold_still_needs_admin(monkeypatch):
    """KNOWN-POSITIVE TWIN. Retiring somebody else's agent genuinely does need
    an admin key — the fix must not quietly act as whoever is ambient."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    rc, out, seen, bus = _run(monkeypatch, _args("someone-elses-agent"), lambda n: None)
    assert seen["api_key"] is None, "must not invent a credential we do not hold"


def test_an_explicit_api_key_still_wins(monkeypatch):
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    rc, out, seen, _ = _run(
        monkeypatch, _args("auditor-be8047", api_key="ab_sk_operator"), lambda n: "ab_sk_bound"
    )
    assert seen["api_key"] == "ab_sk_operator"


def test_retiring_an_already_retired_agent_is_not_a_failure(monkeypatch):
    """The operator's second `retire` printed an error that read like something
    had gone wrong when nothing had — it was the state they asked for."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    rc, out, _, _ = _run(
        monkeypatch, _args("auditor-be8047"), lambda n: "k", bus=_Bus(fail_code="agent_retired")
    )
    assert rc == 0
    assert "already retired" in out


def test_other_errors_still_propagate(monkeypatch):
    """KNOWN-POSITIVE TWIN: only `agent_retired` is swallowed."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    with pytest.raises(AgentBusError):
        _run(monkeypatch, _args("x"), lambda n: "k", bus=_Bus(fail_code="permission_denied"))
