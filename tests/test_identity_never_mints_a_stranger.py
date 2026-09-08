"""#57: three ways the identity surface lied, all from one operator transcript.

THE TRANSCRIPT. Retire an agent, delete `.agentbus/agent` and
`settings.local.json`, re-run `setup claude --role datashard` — and the OLD name
comes back with no explanation. The operator's conclusion was "we can't retire".
Retire works; the identity is simply not renameable, and nothing said so.

THE ONE THAT DID REAL DAMAGE. A bare `agentbus register` in an already-wired
project minted a brand-new RANDOM agent (the server names an unnamed, roleless
registration), wrote it a real bound key, and only THEN printed "NOT WIRED: this
project already belongs to <other>". The refusal came after the damage. Measured
live: it produced `steady-compass-69`. Evidence it had happened before anyone
looked — `clever-lantern-55.env` was already in the operator's keys directory
with no project claiming it. That is how a workspace reaches its 100-agent cap.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBusError


class _Bus:
    """Records what register() was asked for. Registering is the damage, so the
    test asserts on the REQUEST, not on a returned name."""

    agent = None

    def __init__(self):
        self.registered: list[tuple] = []

    def register(self, name, **kw):
        self.registered.append((name, kw.get("role")))
        return {"agent": {"name": name or "server-invented-name"}, "address": "a@b", "rooms": []}

    def whoami(self):
        raise AgentBusError("retired", code="agent_retired", status=409)


def _args(**over):
    base = {
        "name": None,
        "role": None,
        "workdir": None,
        "repo_remote": None,
        "capability": [],
        "label": [],
        "unlisted": False,
        "ephemeral": False,
        "persona": None,
        "json": False,
        "agent": None,
        "base_url": None,
        "qr": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_a_bare_register_in_a_wired_project_reuses_its_identity(monkeypatch, tmp_path):
    """THE REGRESSION: it used to mint a stranger and refuse afterwards."""
    bus = _Bus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    monkeypatch.setattr(
        "agentbus_client.onboarding._resolve_agent_name", lambda *a, **k: "auditor-be8047"
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_register(_args())
    assert bus.registered, "register was never called"
    assert bus.registered[0][0] == "auditor-be8047", (
        f"registered {bus.registered[0][0]!r} — a bare register in a wired project "
        "must not mint a new agent"
    )
    assert "already declares" in buf.getvalue()


def test_an_explicit_name_is_still_honoured(monkeypatch):
    """KNOWN-POSITIVE TWIN. Registering a SECOND agent in a wired project is a
    legitimate act; the fix must not make it impossible."""
    bus = _Bus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    monkeypatch.setattr(
        "agentbus_client.onboarding._resolve_agent_name", lambda *a, **k: "auditor-be8047"
    )
    with redirect_stdout(io.StringIO()):
        cli.cmd_register(_args(name="deliberate-second"))
    assert bus.registered[0][0] == "deliberate-second"


def test_a_role_is_still_honoured(monkeypatch):
    """The other twin: --role means 'derive one', not 'reuse whatever is here'."""
    bus = _Bus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    monkeypatch.setattr(
        "agentbus_client.onboarding._resolve_agent_name", lambda *a, **k: "auditor-be8047"
    )
    with redirect_stdout(io.StringIO()):
        cli.cmd_register(_args(role="datashard"))
    assert bus.registered[0] == (None, "datashard")


def test_whoami_reports_a_retired_agent_instead_of_dying(monkeypatch):
    """The diagnostic must work at the moment something IS wrong."""
    bus = _Bus()
    bus.agent = "auditor-be8047"
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_whoami(_args())
    out = buf.getvalue()
    assert rc == 0
    assert "auditor-be8047" in out
    assert "RETIRED" in out
    assert "reversible" in out or "NOT deletion" in out


def test_whoami_still_raises_on_other_errors(monkeypatch):
    """KNOWN-POSITIVE TWIN: only `agent_retired` is swallowed. A 401 must not be
    reported as a healthy retired agent."""

    class _Dead(_Bus):
        def whoami(self):
            raise AgentBusError("nope", code="unauthenticated", status=401)

    monkeypatch.setattr(cli._common, "_bus", lambda _a: _Dead())
    with pytest.raises(AgentBusError):
        cli.cmd_whoami(_args())


def test_setup_says_when_a_declared_name_overrode_your_role(monkeypatch):
    """The other half of the loop: `--role datashard` in a checkout that declares
    `auditor-be8047` did nothing, silently. A declared NAME outranks a role, and
    the 0.9.88 note only covered the case where no name was resolved at all."""
    from agentbus_client.onboarding import _provision

    report: list[str] = []
    monkeypatch.setattr(_provision, "_resolve_agent_name", lambda explain=None: "auditor-be8047")
    monkeypatch.setattr(_provision, "_operator_key", lambda: "ab_sk_op")

    class _B:
        agent = None

        def register(self, name, **kw):
            return {
                "agent": {"name": "auditor-be8047", "device_hash": "x"},
                "address": "auditor-be8047@mail.test",
                "rooms": [],
            }

        def phonebook(self, **kw):
            return []

        def whoami(self):
            return {"agent": {"name": "auditor-be8047"}, "workspace": {"slug": "w"}}

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(_provision, "AgentBus", lambda **kw: _B())
    monkeypatch.setattr(_provision, "_agent_key", lambda n: "k")
    args = argparse.Namespace(role="datashard", base_url=None, persona=None, force_new=False)
    _provision._provision_project_agent(args, report, "claude")
    joined = "\n".join(report)
    assert "was NOT applied" in joined, joined
    assert "agentbus teardown" in joined
