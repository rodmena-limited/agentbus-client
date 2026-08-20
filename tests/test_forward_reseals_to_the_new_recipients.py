"""#219: forwarding re-encrypts; it never relays ciphertext onward.

A sealed message is encrypted to the keys of the people it was addressed to.
Handing those bytes to somebody new gives them a file they cannot open — a
forward that reports success and is unreadable. So the whole process is
repeated: read, unseal HERE with this agent's own key, then seal the result to
every new recipient's published key.

Operator decision, 2026-08-16: "when forwarding an email chain that happened
before between two or more parties to a third-party, the entire process should
be followed, meaning encryption using all pub keys and sending over."
"""

from __future__ import annotations

import argparse
import inspect
import io
from contextlib import redirect_stdout

from agentbus_client import cli


class FakeBus:
    agent = "me"

    def __init__(self) -> None:
        self.sent: dict | None = None

    def read(self, _delivery_id):
        # `read` unseals on the way through, so this is the plaintext this
        # agent was entitled to.
        return {
            "text_body": "the original secret",
            "sender_display": "alice",
            "subject": "budget",
            "created_at": "2026-08-16T00:00:00Z",
            "message_id": "msg_1",
        }

    def send(self, **kwargs):
        self.sent = kwargs
        return {"id": "msg_2", "recipients": kwargs["to"]}


def _forward(to, body=None):
    args = argparse.Namespace(
        delivery_id="del_1", to=to, cc=None, body=body, priority=None, json=False, agent=None
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_forward(args)
    return rc, buf.getvalue()


def test_forwarding_sends_plaintext_through_the_sealing_send_path(monkeypatch):
    """The plaintext is handed to `send`, which seals it to the NEW recipients.
    Reimplementing the sealing here would be a second copy of the rule that
    matters most."""
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    rc, _out = _forward(["carol"])
    assert rc == 0
    assert bus.sent is not None
    assert bus.sent["to"] == ["carol"]
    assert "the original secret" in bus.sent["text"]


def test_the_original_ciphertext_is_never_relayed(monkeypatch):
    """THE POINT. If a forward copied the sealed bytes, the third party would
    hold something only the ORIGINAL recipients can open."""
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _forward(["carol"])
    assert "BEGIN AGE ENCRYPTED FILE" not in bus.sent["text"]


def test_the_forward_is_attributed(monkeypatch):
    """A quoted block with no attribution is how a forwarded claim becomes the
    forwarder's own."""
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _forward(["carol"])
    text = bus.sent["text"]
    assert "Forwarded message" in text
    assert "alice" in text
    assert "msg_1" in text


def test_a_note_can_be_added_above_the_quote(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _forward(["carol"], body="see below")
    assert bus.sent["text"].startswith("see below")


def test_an_unreadable_message_is_refused_not_forwarded_empty(monkeypatch):
    """If this agent cannot unseal it, there is nothing to re-seal. Sending an
    empty forward would look like a delivered message with no content."""
    bus = FakeBus()
    bus.read = lambda _d: {"text_body": "", "subject": "x"}
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    rc, _ = _forward(["carol"])
    assert rc == 1
    assert bus.sent is None


def test_a_recipient_without_a_key_is_refused_by_the_send_path():
    """Not re-checked here on purpose: the send path already refuses, and a
    second copy of that rule would drift. This asserts we did not bypass it."""
    src = inspect.getsource(cli.cmd_forward)
    assert "bus.send(" in src
    assert "seal_for" not in src, "forward must not seal by hand; use the send path"


def test_the_subject_is_marked_as_a_forward(monkeypatch):
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _forward(["carol"])
    assert bus.sent["subject"].startswith("Fwd: ")


def test_an_already_forwarded_subject_is_not_double_prefixed(monkeypatch):
    bus = FakeBus()
    bus.read = lambda _d: {"text_body": "x", "subject": "Fwd: budget", "sender_display": "a"}
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _forward(["carol"])
    assert bus.sent["subject"] == "Fwd: budget"
