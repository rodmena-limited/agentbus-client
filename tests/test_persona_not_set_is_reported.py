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

from agentbus_client.cli._register import _print_persona_outcome

# The advisory the live server returns (backend 601008d), captured from the wire
# rather than invented, so the parser is tested against the real string.
LIVE_ADVISORY = (
    "persona 'builder' was NOT applied: setting a persona requires an "
    "admin-scope key, and this key is 'send'. The agent was registered "
    "without it. Ask an operator to set the persona, or use an admin key."
)


def test_the_servers_explanation_is_shown_to_the_user(capsys):
    """KNOWN-POSITIVE. If this cannot go green the absence checks below are
    worthless — a printer that renders nothing passes every "no output" test."""
    _print_persona_outcome(
        {"persona_ignored": LIVE_ADVISORY, "agent": {"persona": None}}, "builder"
    )
    out = capsys.readouterr().out
    assert "PERSONA NOT SET" in out
    assert "admin-scope key" in out, "the reason must survive, not just the verdict"


def test_silent_when_no_persona_was_requested(capsys):
    """A caller who never asked must see nothing, or the notice becomes noise
    on every register and stops being read on the one that matters."""
    _print_persona_outcome({"agent": {"persona": None}}, None)
    assert capsys.readouterr().out == ""


def test_silent_when_the_persona_actually_took(capsys):
    """An admin key sets it successfully — saying nothing is correct there."""
    _print_persona_outcome({"agent": {"persona": "builder"}}, "builder")
    assert capsys.readouterr().out == ""


def test_old_server_with_no_advisory_still_reports_the_drop(capsys):
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


def test_the_flag_still_reaches_the_wire():
    """The CLI was never the bug — assert that, so a future 'fix' does not
    'repair' the send path that was already correct."""
    import inspect

    from agentbus_client.client import AgentBus

    source = inspect.getsource(AgentBus.register)
    assert "persona" in inspect.signature(AgentBus.register).parameters
    assert 'payload["persona"] = persona' in source


def test_register_wires_the_reporter_into_its_output():
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


def _setup_report(result: dict, requested: str | None) -> list[str]:
    """The persona branch of the setup panel, as _provision builds it."""
    report: list[str] = []
    if requested:
        advisory = result.get("persona_ignored")
        if advisory:
            report.append(f"PERSONA NOT SET: {advisory}")
        elif not (result.get("agent") or {}).get("persona"):
            report.append(f"PERSONA NOT SET: asked for '{requested}' ... ADMIN-scope")
        else:
            report.append(f"persona: {requested}")
    return report


def test_setup_panel_reports_a_dropped_persona():
    """The exact case the operator hit: a clean panel hiding one failed flag."""
    lines = _setup_report(
        {"persona_ignored": LIVE_ADVISORY, "agent": {"persona": None}}, "test-engineer"
    )
    assert any("PERSONA NOT SET" in line for line in lines)


def test_setup_panel_is_silent_when_no_persona_requested():
    assert _setup_report({"agent": {"persona": None}}, None) == []


def test_setup_panel_confirms_a_persona_that_took():
    lines = _setup_report({"agent": {"persona": "backend"}}, "backend")
    assert lines == ["persona: backend"]


def test_setup_panel_falls_back_when_the_server_is_old():
    lines = _setup_report({"agent": {"persona": None}}, "backend")
    assert any("ADMIN-scope" in line for line in lines)


def test_BOTH_register_and_setup_paths_report_it():
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
