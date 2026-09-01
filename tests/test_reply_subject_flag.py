"""#52 (SPECS/0052): `agentbus reply -s` — the CLI exposes the subject the SDK
and server already carry. Verified live 2026-09-01: the server stores a
per-message subject on a reply and the thread view shows it."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

from agentbus_client import cli


class FakeBus:
    agent = "me"

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def read(self, ident, **_):
        raise cli.AgentBusError("not a delivery", code="not_found", status=404)

    def reply(self, message_id, text, **kwargs):
        self.kwargs = kwargs
        return {"id": "out_1", "recipients": ["peer"], "cc": []}


def _run(monkeypatch, **flags):
    bus = FakeBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(
        message_id="msg_1",
        body="hello",
        reply_all=False,
        cc=[],
        priority=None,
        attach=[],
        subject=flags.get("subject"),
        to_self=False,
        json=False,
        agent=None,
    )
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.cmd_reply(args)
    return rc, out.getvalue(), bus


def test_subject_is_passed_through_and_echoed(monkeypatch):
    rc, out, bus = _run(monkeypatch, subject="Sentinel 503 [sev-2]")
    assert rc == 0
    assert bus.kwargs["subject"] == "Sentinel 503 [sev-2]"
    assert 'subject "Sentinel 503 [sev-2]"' in out


def test_no_subject_means_none_so_the_server_derives_re(monkeypatch):
    """An empty string would be stored as an empty subject; None keeps 'Re:'."""
    rc, out, bus = _run(monkeypatch, subject=None)
    assert rc == 0
    assert bus.kwargs["subject"] is None
    assert "subject" not in out
    rc, out, bus = _run(monkeypatch, subject="")
    assert bus.kwargs["subject"] is None


def test_parser_accepts_short_and_long_forms():
    args = cli.build_parser().parse_args(["reply", "msg_1", "-s", "new", "-b", "x"])
    assert args.subject == "new"
    args = cli.build_parser().parse_args(["reply", "msg_1", "--subject", "new", "-b", "x"])
    assert args.subject == "new"
    args = cli.build_parser().parse_args(["reply", "msg_1", "-b", "x"])
    assert args.subject is None and args.to_self is False
