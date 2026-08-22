"""#35: the UserPromptSubmit hook must not echo its stdin payload.

CLAUDE CODE APPENDS THIS HOOK'S STDOUT TO THE PROMPT CONTEXT. So `print(raw)`
put the entire harness payload — session_id, transcript_path, cwd, permission
mode, and the user's own prompt — back into the model's context on EVERY TURN of
every session with the hook installed.

Observed live rather than reasoned about: sessions running 0.9.x show

    {"session_id":"...","transcript_path":"...","cwd":"...","prompt":"..."}

appended to the user turn after each bus wake. It is not a crash and nothing
fails, which is why it survived — it just quietly spends context and shows the
reader plumbing they did not ask for.

stdout here is a CHANNEL WITH A CONSUMER, not a log. The only thing that belongs
on it is the unread-mail notice.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from agentbus_client.hooks import _turn

PAYLOAD = json.dumps(
    {
        "session_id": "abc-123",
        "transcript_path": "/home/u/.claude/x.jsonl",
        "cwd": "/home/u/p",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "the user's actual words",
    }
)


class _FakeStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture
def _stdin(monkeypatch):
    def _set(text: str):
        monkeypatch.setattr("sys.stdin", _FakeStdin(text))

    return _set


def _run(monkeypatch, payload: str) -> str:
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))
    # No credential -> pending() takes its early exit without touching the bus.
    monkeypatch.setattr(_turn, "_resolve_agent", lambda: None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _turn.pending(None)
    return buf.getvalue()


def test_the_payload_is_not_echoed(monkeypatch):
    out = _run(monkeypatch, PAYLOAD)
    assert "session_id" not in out
    assert "transcript_path" not in out
    assert "the user's actual words" not in out


def test_a_harness_notification_exits_silently(monkeypatch):
    """The other echo site: a harness notification is not a human prompt, and
    passing it through put the same JSON into the turn."""
    # A harness event is a TAGGED BLOCK, not JSON. My first version of this
    # test used a JSON payload, which `_is_harness_notification` correctly
    # rejects — so it exited through the no-credential path instead and the
    # test passed without ever entering the branch it named. A mutation
    # restoring the echo went GREEN against it.
    notif = "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"
    assert _turn._is_harness_notification(notif), "stimulus must reach the branch"
    assert _run(monkeypatch, notif).strip() == ""


def test_the_hook_can_still_print_something(monkeypatch):
    """KNOWN-NEGATIVE, and the reason this file is not vacuous.

    'stdout is empty' would also pass if pending() had been broken into
    printing nothing at all, ever — including the unread-mail notice it exists
    to deliver. Proving the channel still works is what makes the assertions
    above mean 'the payload is gone' rather than 'the feature is gone'.
    """
    monkeypatch.setattr("sys.stdin", _FakeStdin(PAYLOAD))
    monkeypatch.setattr(_turn, "_resolve_agent", lambda: "agent-x")
    monkeypatch.setattr(_turn, "_warn_if_shadow_queue", lambda: None)

    class _Bus:
        def __init__(self, *a, **k):
            pass

        def whoami(self):
            return {"unread": {"count": 2}}

        def inbox(self, *a, **k):
            return {"messages": []}

    monkeypatch.setattr("agentbus_client.client.AgentBus", _Bus)
    buf = io.StringIO()
    with redirect_stdout(buf):
        _turn.pending(None)
    printed = buf.getvalue()
    assert printed.strip(), "the notice channel must still be able to print"
    assert "transcript_path" not in printed
    assert "the user's actual words" not in printed
