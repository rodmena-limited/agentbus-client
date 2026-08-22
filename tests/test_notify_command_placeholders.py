"""Pin every documented placeholder in `notify_command` (issuedb #12).

Backend's audit/evaluations/probe_onboarding.py used to assert that
`{agent_seq}` was a real substitution by reading
`sdk/agentbus_client/watch.py`. The client extraction (#234) moved that
file here, and the backend check was commented out. This test re-anchors
the invariant in the repo that owns the placeholder set: any future
refactor that removes a documented placeholder fails HERE instead of
silently dropping it and letting an operator's --exec template break in
production.
"""

from __future__ import annotations

import subprocess

import pytest

from agentbus_client import watch as watch_module

# Every placeholder documented in watch.notify_command's docstring and in
# the served skill. If you add one, add it here; if you remove one, this
# test failing IS the reminder that operators' templates depend on it.
DOCUMENTED_PLACEHOLDERS = [
    "subject",
    "sender",
    "delivery_id",
    "message_id",
    "thread_id",
    "agent_seq",
    "direction",
    "inbound_source",
    "envelope_count",
    "envelope_kind",
]

SAMPLE_MESSAGE = {
    "subject": "hello",
    "sender_display": "peer-a",
    "sender_address": "peer-a@example",
    "delivery_id": "01D",
    "message_id": "01M",
    "thread_id": "01T",
    "agent_seq": 42,
    "direction": "in",
    "inbound_source": "smtp",
}


@pytest.fixture
def captured_command(monkeypatch):
    """Intercept subprocess.run so the test can inspect the fully-substituted
    command string without actually spawning a shell."""
    calls: list[str] = []

    def fake_run(command, *_a, **_kw):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_every_documented_placeholder_substitutes_without_keyerror(captured_command):
    """The whole set as one template — catches a placeholder that was quietly
    removed by not appearing in the format() call."""
    template = " ".join(f"[{p}]={{{p}}}" for p in DOCUMENTED_PLACEHOLDERS)
    handler = watch_module.notify_command(template)
    handler(SAMPLE_MESSAGE)  # must not raise KeyError

    assert len(captured_command) == 1
    command = captured_command[0]
    # Every placeholder position was substituted (no literal `{name}` left).
    for name in DOCUMENTED_PLACEHOLDERS:
        assert f"{{{name}}}" not in command, (
            f"placeholder {{{name}}} was not substituted; format() dropped it"
        )


@pytest.mark.parametrize("placeholder", DOCUMENTED_PLACEHOLDERS)
def test_each_placeholder_individually_substitutes(placeholder, captured_command):
    """Parametrised failure message names WHICH placeholder broke, so a
    regression from a refactor points at the offending line immediately."""
    template = f"echo before-{{{placeholder}}}-after"
    handler = watch_module.notify_command(template)
    handler(SAMPLE_MESSAGE)
    command = captured_command[0]
    assert f"{{{placeholder}}}" not in command, (
        f"placeholder {{{placeholder}}} did not substitute — check "
        f"notify_command in src/agentbus_client/watch.py"
    )
    assert "before-" in command
    assert "-after" in command


def test_unknown_placeholder_still_raises_keyerror(captured_command):
    """The spec REQUIRES that a template typo fails LOUD rather than passing
    the literal `{typo}` through to the shell — otherwise every arrival
    would be broken silently. Keep this behaviour."""
    handler = watch_module.notify_command("echo {no_such_placeholder}")
    with pytest.raises(KeyError):
        handler(SAMPLE_MESSAGE)
