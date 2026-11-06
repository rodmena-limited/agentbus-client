"""`agentbus register --persona X` must not report success for a silent no-op.

issuedb #37 (F10), SPECS/0025. Reported by a third tester on thread
01M0GTYM8NWWBFYSJQWDDG83V4 and reproduced by a wire trace here.

THE DEFECT AND ITS VICTIM: persona is admin-only POLICY (backend #264). A
non-admin write is DROPPED rather than rejected — deliberately, so an old client
passing the field does not break. Authorization is checked BEFORE validation, so
a valid lane and a nonsense one behave identically: you never reach the
validator. A peer set `--persona`, read it back as null, could not distinguish
"not wired" from "wired and dropped" (identical from outside), concluded the CLI
was broken, and filed a defect against the wrong layer.

The client sends the field correctly. What it did wrong was print a clean
success for something that had not happened.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

from agentbus_client.cli._register import _print_persona_outcome

if TYPE_CHECKING:
    import pytest

# The advisory the live server returns (backend 601008d), captured from the wire
# rather than invented, so the parser is tested against the real string.
LIVE_ADVISORY = (
    "persona 'builder' was NOT applied: setting a persona requires an "
    "admin-scope key, and this key is 'send'. The agent was registered "
    "without it. Ask an operator to set the persona, or use an admin key."
)


def test_the_servers_explanation_is_shown_to_the_user(capsys: pytest.CaptureFixture[str]) -> None:
    """KNOWN-POSITIVE. If this cannot go green the absence checks below are
    worthless — a printer that renders nothing passes every "no output" test."""
    _print_persona_outcome(
        {"persona_ignored": LIVE_ADVISORY, "agent": {"persona": None}}, "builder"
    )
    out = capsys.readouterr().out
    assert "PERSONA NOT SET" in out
    assert "admin-scope key" in out, "the reason must survive, not just the verdict"


def test_silent_when_no_persona_was_requested(capsys: pytest.CaptureFixture[str]) -> None:
    """A caller who never asked must see nothing, or the notice becomes noise
    on every register and stops being read on the one that matters."""
    _print_persona_outcome({"agent": {"persona": None}}, None)
    assert capsys.readouterr().out == ""


def test_silent_when_the_persona_actually_took(capsys: pytest.CaptureFixture[str]) -> None:
    """An admin key sets it successfully — saying nothing is correct there."""
    _print_persona_outcome({"agent": {"persona": "builder"}}, "builder")
    assert capsys.readouterr().out == ""


def test_old_server_with_no_advisory_still_reports_the_drop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FORWARD-COMPATIBILITY, and the branch most likely to rot unnoticed.

    A server predating backend 601008d returns no `persona_ignored` at all. The
    persona is still silently dropped, so relying on the server to explain
    itself would reproduce the original defect against every older deployment.
    When we ASKED for a lane and the agent came back without one, say so.
    """
    _print_persona_outcome({"agent": {"persona": None}}, "builder")
    out = capsys.readouterr().out
    assert "PERSONA NOT SET" in out
    assert "builder" in out, "name what was asked for"
    assert "ADMIN" in out.upper(), "and why it did not take"


def test_the_flag_still_reaches_the_wire() -> None:
    """The CLI was never the bug — assert that, so a future 'fix' does not
    'repair' the send path that was already correct."""
    import inspect

    from agentbus_client.client import AgentBus

    source = inspect.getsource(AgentBus.register)
    assert "persona" in inspect.signature(AgentBus.register).parameters
    assert 'payload["persona"] = persona' in source


def test_register_wires_the_reporter_into_its_output() -> None:
    """The helper must actually be CALLED. A correct printer nobody invokes is
    the dead-exemption defect in another costume."""
    import inspect

    from agentbus_client.cli import _register

    assert "_print_persona_outcome(result" in inspect.getsource(_register.cmd_register)


# --------------------------------------------------------------- the SETUP path
#
# F10 SECOND PATH. The first fix landed on `agentbus register` only — one of two
# call sites — and was declared closed. `agentbus setup claude --persona X` kept
# printing a full success panel with no persona line at all, and the session that
# came up reported persona: null.
#
# That is worse than the original bug for one reason: `setup` is the command the
# skill and the docs tell a new agent to run. The fix landed on the quiet
# entrypoint and missed the loud one, because one call site was checked and the
# conclusion was generalised to the capability. Each call site is its own claim.


SETUP_AGENT = "setup-agent"


class _SetupBus:
    def __init__(self, registered: dict[str, Any]) -> None:
        self.registered = registered
        self.sent_persona: str | None = None

    def whoami(self) -> dict[str, Any]:
        return {"agent": {"name": SETUP_AGENT}, "workspace": {"slug": "w"}}

    def register(self, _name: str, **kw: Any) -> dict[str, Any]:
        self.sent_persona = kw["persona"]
        return {"address": f"{SETUP_AGENT}@mail.test", "rooms": [], **self.registered}


def _setup_report(
    monkeypatch: pytest.MonkeyPatch, agent: dict[str, Any], requested: str | None, **fields: Any
) -> list[str]:
    """The persona lines of the setup panel, from the shared setup path."""
    from agentbus_client.onboarding import _provision

    bus = _SetupBus({"agent": {"name": SETUP_AGENT, **agent}, **fields})
    monkeypatch.setattr(_provision, "_resolve_agent_name", lambda explain=None: SETUP_AGENT)
    monkeypatch.setattr(_provision, "_operator_key", lambda: "ab_sk_op")
    monkeypatch.setattr(_provision, "_agent_key", lambda _name: "k")
    monkeypatch.setattr(_provision, "AgentBus", lambda **_kw: bus)

    args = argparse.Namespace(role=None, base_url=None, persona=requested, force_new=False)
    report: list[str] = []
    assert _provision._provision_project_agent(args, report, "claude") == SETUP_AGENT
    assert bus.sent_persona == requested
    return [line for line in report if line.startswith(("PERSONA NOT SET", "persona:"))]


def test_setup_panel_reports_a_dropped_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact case the operator hit: a clean panel hiding one failed flag."""
    lines = _setup_report(
        monkeypatch, {"persona": None}, "test-engineer", persona_ignored=LIVE_ADVISORY
    )
    assert lines == [f"PERSONA NOT SET: {LIVE_ADVISORY}"]


def test_setup_panel_is_silent_when_no_persona_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _setup_report(monkeypatch, {"persona": None}, None) == []


def test_setup_panel_confirms_a_persona_that_took(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _setup_report(monkeypatch, {"persona": "backend"}, "backend")
    assert lines == ["persona: backend"]


def test_setup_panel_falls_back_when_the_server_is_old(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _setup_report(monkeypatch, {"persona": None}, "backend")
    assert len(lines) == 1
    assert lines[0].startswith("PERSONA NOT SET: asked for 'backend'")
    assert "ADMIN-scope" in lines[0]


def test_both_register_and_setup_paths_report_it() -> None:
    """THE REGRESSION GUARD FOR THE MISS ITSELF.

    There are exactly two call sites that pass persona to register(). The first
    fix covered one and was called done. This asserts both carry the advisory,
    so a third entrypoint added later cannot quietly repeat it.
    """
    import inspect

    from agentbus_client.cli import _register
    from agentbus_client.onboarding import _provision

    for module in (_register, _provision):
        assert "PERSONA NOT SET" in inspect.getsource(module), (
            f"{module.__name__} passes persona to register() but never reports a silent drop"
        )
