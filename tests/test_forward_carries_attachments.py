"""#223 and #231: a forward that drops files, and a VERIFIED that overclaims.

#223 — `agentbus forward` sent the quoted text and NOTHING ELSE. A message that
arrived with files was passed on without them and nothing said so. To the
recipient that is indistinguishable from a sender who forgot to attach anything;
to the sender it looks like a completed forward. Silent partial delivery is
worse than either a refusal or a visible failure.

They cannot be relayed as-is, for the same reason the body cannot: on an
encrypted workspace each blob is sealed to the ORIGINAL recipients' keys, so
handing those bytes to somebody new gives them a file they cannot open. They are
opened here and re-sealed by the ordinary send path — the same round trip the
body already made.

VERIFIED END TO END on the live deployment before this test was written: a file
containing FORWARD-ATTACHMENT-MARKER-8812 was sent, forwarded, and fetched back
from the forwarded copy with the marker intact and the original filename.

#231 — `verify-sender` printed a bare "VERIFIED — signed by X". agentbus-sig-v1
covers sender, recipients, subject, priority and the body hash; it does NOT
cover html, attachments or the structured payload. That is published in
`signed_fields`, which nobody reads, so a bare VERIFIED beside a message
carrying an attachment invites exactly the assumption the protocol does not
support.

This is #220 pointed the other way. There the tool claimed a FAILURE it had not
earned; here it claimed COVERAGE it had not earned. Both are a verifier saying
more than it checked.
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
from typing import Any

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBusError


class _Bus:
    """A message carrying two attachments, one of which may refuse to open."""

    agent = "me"

    def __init__(self, *, failing_index: int | None = None) -> None:
        self.failing_index = failing_index
        self.sent: dict[str, Any] = {}
        self.fetched: list[int] = []

    def read(self, _delivery_id: str) -> dict[str, Any]:
        return {
            "text_body": "the original body",
            "sender_display": "peer",
            "subject": "quarterly",
            "created_at": "2026-08-16",
            "message_id": "m1",
            "attachments": [{"filename": "report.pdf"}, {"filename": "data.csv"}],
        }

    def attachment(self, _delivery_id: str, index: int = 0) -> bytes:
        self.fetched.append(index)
        if index == self.failing_index:
            raise AgentBusError("sealed to keys this machine does not hold")
        return f"CONTENT-{index}".encode()

    def send(self, **kw: Any) -> dict[str, Any]:
        self.sent = kw
        return {"id": "fwd1", "recipients": ["third-party"]}


def test_a_forward_carries_every_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE BUG: the files used to be left behind, silently."""
    bus = _Bus()
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    monkeypatch.setattr(cli._common, "_read_body", lambda _b: "")

    args = argparse.Namespace(
        delivery_id="d1", to=["third-party"], cc=None, body=None, json=False, priority=None
    )
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_forward(args) == 0

    assert bus.fetched == [0, 1], "not every attachment was read from the original"
    paths = bus.sent.get("attachments") or []
    assert len(paths) == 2, "the forward went out without its attachments"

    # the ORIGINAL FILENAMES survive — a mangled name reads as sender sloppiness
    assert [p.rsplit("/", 1)[-1] for p in paths] == ["report.pdf", "data.csv"]


def test_an_unreadable_attachment_refuses_instead_of_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half that matters more than carrying them.

    If one blob cannot be opened, forwarding the text alone would deliver less
    than the sender believes they sent — and say nothing. Refusing names the
    real problem and sends nothing.
    """
    bus = _Bus(failing_index=1)
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    monkeypatch.setattr(cli._common, "_read_body", lambda _b: "")

    args = argparse.Namespace(
        delivery_id="d1", to=["third-party"], cc=None, body=None, json=False, priority=None
    )
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = cli.cmd_forward(args)

    assert code == 1, "a forward with an unreadable attachment reported success"
    assert not bus.sent, "it sent the text anyway, silently dropping a file"
    message = err.getvalue()
    assert "cannot forward" in message
    assert "data.csv" in message, "the failure does not name WHICH attachment"


def test_a_message_with_no_attachments_still_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. A fix that broke the ordinary case would not be a fix."""

    class _Plain(_Bus):
        def read(self, _delivery_id: str) -> dict[str, Any]:
            data = super().read(_delivery_id)
            data["attachments"] = []
            return data

    bus = _Plain()
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    monkeypatch.setattr(cli._common, "_read_body", lambda _b: "")

    args = argparse.Namespace(
        delivery_id="d1", to=["third-party"], cc=None, body=None, json=False, priority=None
    )
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.cmd_forward(args) == 0
    assert bus.fetched == []
    assert not bus.sent.get("attachments")


def test_verify_sender_states_what_the_signature_covers() -> None:
    """#231. A bare VERIFIED beside an attachment invites the wrong conclusion."""
    src = inspect.getsource(cli.cmd_verify_signature)
    assert "NOT covered" in src, (
        "verify-sender prints a bare VERIFIED and never says that html, "
        "attachments and payload are outside agentbus-sig-v1"
    )
    assert "covers:" in src
