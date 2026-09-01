"""#51 (SPECS/0051): `agentbus sent` — the outbox over GET /v1/sent.

Reported by a platform whose daemon had posted ~60 alerts into a counterparty
thread; the operator asked "what is the command to list bus postings?" and the
answer was: grep the daemon's logs. The server answered /v1/sent the whole time.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBusError, NotFoundError


def _row(i: int, thread: str = "th_a", signed: bool = True, sealed: bool = True) -> dict:
    return {
        "message_id": f"msg_{i}",
        "thread_id": thread,
        "subject": f"subject {i}",
        "sent_at": f"2026-09-01T{20 - i:02d}:00:00Z",  # newest first, like the server
        "sealed": sealed,
        "sealed_by": "sender" if sealed else None,
        "signed_recipients": json.dumps({"to": ["peer"], "cc": []}) if signed else None,
        "text_body": "-----BEGIN AGE ENCRYPTED FILE-----\nxx" if sealed else "plain body",
    }


class FakeBus:
    """Pages of /v1/sent, newest first, `page_size` rows per page."""

    def __init__(self, rows: list[dict], page_size: int = 2, fail: AgentBusError | None = None):
        self.rows = rows
        self.page_size = page_size
        self.fail = fail
        self.agent = "me"
        self.calls: list[dict] = []
        self.unsealed: list[str] = []

    def sent(self, *, limit: int, cursor: str | None = None, agent: str | None = None) -> dict:
        self.calls.append({"limit": limit, "cursor": cursor})
        if self.fail:
            raise self.fail
        start = int(cursor) if cursor else 0
        page = self.rows[start : start + self.page_size]
        nxt = start + self.page_size
        return {
            "messages": page,
            "count": len(page),
            "cursor": str(nxt) if nxt < len(self.rows) else None,
        }

    def unseal_message(self, message: dict) -> dict:
        self.unsealed.append(message["message_id"])
        message["text_body"] = "OPENED"
        return message


def _run(monkeypatch, bus: FakeBus, **flags) -> tuple[int, str, str]:
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(
        limit=flags.get("limit", 50),
        thread=flags.get("thread"),
        since=flags.get("since"),
        json=flags.get("json", False),
        agent=None,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.cmd_sent(args)
    return rc, out.getvalue(), err.getvalue()


def test_lists_every_row_with_id_thread_recipients_and_subject(monkeypatch):
    rc, out, _ = _run(monkeypatch, FakeBus([_row(1), _row(2)]))
    assert rc == 0
    assert "msg_1" in out and "msg_2" in out
    assert "thread  th_a" in out
    assert "to      peer" in out
    assert "subject subject 1" in out
    assert "2 sent message(s)" in out


def test_thread_filter_pages_past_non_matching_rows(monkeypatch):
    """The filter is client-side; a match on page 3 must still be found."""
    rows = [_row(1, "th_x"), _row(2, "th_x"), _row(3, "th_x"), _row(4, "th_x"), _row(5, "th_a")]
    bus = FakeBus(rows, page_size=2)
    rc, out, _ = _run(monkeypatch, bus, thread="th_a")
    assert rc == 0
    assert "msg_5" in out and "msg_1" not in out
    assert len(bus.calls) == 3, bus.calls


def test_thread_filter_stops_at_the_page_bound(monkeypatch):
    """A filter that matches nothing must not walk an unbounded history."""
    rows = [_row(i, "th_x") for i in range(1, 400)]
    bus = FakeBus(rows, page_size=1)
    rc, out, err = _run(monkeypatch, bus, thread="th_never")
    assert rc == 0
    assert "no sent messages in thread th_never" in out
    assert len(bus.calls) == cli._sent._MAX_PAGES
    assert "incomplete: stopped after" in err


def test_truncation_is_reported_when_rows_were_shown(monkeypatch):
    rows = [_row(1, "th_a")] + [_row(i, "th_x") for i in range(2, 400)]
    bus = FakeBus(rows, page_size=1)
    rc, out, err = _run(monkeypatch, bus, thread="th_a", limit=5)
    assert rc == 0
    assert "msg_1" in out
    assert "incomplete: stopped after" in err


def test_since_as_duration_and_as_instant(monkeypatch):
    rows = [_row(1), _row(2), _row(3)]  # 19:00, 18:00, 17:00
    # An absolute instant between row 2 and row 3.
    rc, out, _ = _run(monkeypatch, FakeBus(rows), since="2026-09-01T17:30:00Z")
    assert rc == 0
    assert "msg_1" in out and "msg_2" in out and "msg_3" not in out
    # A duration is parsed relative to now; a huge window keeps everything.
    rc, out, _ = _run(monkeypatch, FakeBus(rows), since="36500d")
    assert "msg_3" in out


def test_since_rejects_garbage_locally():
    with pytest.raises(ValueError):
        cli._sent._since_instant("last tuesday")


def test_unsigned_row_says_recipients_are_not_recorded(monkeypatch):
    """A blank there would read as 'sent to nobody' — null is absence, not zero."""
    rc, out, _ = _run(monkeypatch, FakeBus([_row(1, signed=False)]))
    assert rc == 0
    assert "recipients not recorded: unsigned send" in out


def test_json_unseals_each_row_and_reports_truncation_flag(monkeypatch):
    bus = FakeBus([_row(1), _row(2)])
    rc, out, _ = _run(monkeypatch, bus, json=True)
    assert rc == 0
    data = json.loads(out)
    assert data["count"] == 2 and data["incomplete"] is None
    assert bus.unsealed == ["msg_1", "msg_2"]
    assert all(r["text_body"] == "OPENED" for r in data["sent"])


def test_text_listing_does_not_unseal(monkeypatch):
    """Listing is an index, not a body dump — no key work per row."""
    bus = FakeBus([_row(1)])
    _run(monkeypatch, bus)
    assert bus.unsealed == []


def test_missing_endpoint_is_a_sentence_not_a_traceback(monkeypatch):
    bus = FakeBus([], fail=NotFoundError("no", code="not_found", status=404))
    rc, _out, err = _run(monkeypatch, bus)
    assert rc == 1
    assert "not deployed on this server" in err


class CursorRefusingBus(FakeBus):
    """THE REAL SERVER, 2026-09-01: page 1 carries a timestamp cursor; sending it
    back answers `validation_error: query.cursor: Input should be a valid
    integer`. The first fake here accepted string cursors and the test went
    green against a counterparty that does not — pinned so it cannot again."""

    def sent(self, *, limit, cursor=None, agent=None):
        if cursor is not None:
            self.calls.append({"limit": limit, "cursor": cursor})
            raise AgentBusError(
                "query.cursor: Input should be a valid integer", code="validation_error", status=422
            )
        page = super().sent(limit=limit, cursor=None, agent=agent)
        page["cursor"] = "2026-09-01T21:17:19.198014Z"
        return page


def test_server_refusing_the_cursor_yields_the_first_page_and_says_so(monkeypatch):
    rows = [_row(1, "th_x"), _row(2, "th_a"), _row(3, "th_a")]
    bus = CursorRefusingBus(rows, page_size=2)
    rc, out, err = _run(monkeypatch, bus, thread="th_a")
    assert rc == 0
    assert "msg_2" in out and "msg_3" not in out
    assert "refused to page past cursor" in err and "validation_error" in err
    assert len(bus.calls) == 2  # first page, one refused attempt, then stop


def test_server_refusing_the_cursor_with_no_rows_still_says_incomplete(monkeypatch):
    """'no sent messages in thread X' after searching ONE page is not a fact."""
    bus = CursorRefusingBus([_row(1, "th_x"), _row(2, "th_x")], page_size=2)
    rc, out, err = _run(monkeypatch, bus, thread="th_never")
    assert rc == 0
    assert "no sent messages in thread th_never" in out
    assert "incomplete:" in err


def test_a_failure_on_the_first_page_is_not_swallowed(monkeypatch):
    bus = FakeBus([], fail=AgentBusError("boom", code="validation_error", status=422))
    with pytest.raises(AgentBusError):
        _run(monkeypatch, bus)


def test_other_errors_propagate(monkeypatch):
    bus = FakeBus([], fail=AgentBusError("boom", code="server_error", status=500))
    with pytest.raises(AgentBusError):
        _run(monkeypatch, bus)


def test_the_verb_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(
        ["sent", "--thread", "th_1", "--since", "2h", "--limit", "3"]
    )
    assert args.func is cli.cmd_sent
    assert (args.thread, args.since, args.limit) == ("th_1", "2h", 3)


def test_outbox_is_a_hint_to_the_real_verb():
    assert cli._parser._INTENT_HINTS["outbox"].startswith("sent")
    assert cli._parser._INTENT_HINTS["postings"] == "sent"
