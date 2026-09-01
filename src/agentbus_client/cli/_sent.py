"""`agentbus sent` — the outbox (#51, SPECS/0051).

Reported by a platform that had auto-posted ~60 alerts into a counterparty
thread over a day. Its operator asked "what is the command to list or stop
bus postings?" and the honest answer was: there is none. Outbound history
was rebuilt by grepping the daemon's own logs.

The server has answered `GET /v1/sent` for some time; no client surface
exposed it. This verb is a thin listing over that page, with two client-side
filters — thread and since — because the served contract has neither.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any

from ..client import AgentBusError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _parse_duration, _print

# A --thread filter that matches nothing must not walk an unbounded history.
_MAX_PAGES = 50


def _since_instant(value: str | None) -> _dt.datetime | None:
    """`--since` is an ISO-8601 instant or a duration such as `2h` (= now - 2h)."""
    if not value:
        return None
    try:
        delta = _parse_duration(value)
        return _dt.datetime.now(_dt.timezone.utc) - delta
    except (ValueError, TypeError):
        pass
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    instant = _dt.datetime.fromisoformat(text)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=_dt.timezone.utc)
    return instant


def _sent_at(row: dict[str, Any]) -> _dt.datetime | None:
    raw = row.get("sent_at")
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _recipients_line(row: dict[str, Any]) -> str:
    """Who the message went to, from the SIGNED recipients — or say it is not
    recorded. The row has no plain recipients field; `signed_recipients` is a
    JSON string present only when the send was signed. An unsigned send has
    no recorded recipient set, and printing a blank there would read as
    'sent to nobody' (memory: null_is_absence_not_default)."""
    raw = row.get("signed_recipients")
    if not raw:
        return "(recipients not recorded: unsigned send)"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return "(recipients not recorded: unreadable signature envelope)"
    to = list(parsed.get("to") or [])
    cc = list(parsed.get("cc") or [])
    line = ", ".join(to) or "(nobody in to)"
    if cc:
        line += f"  cc: {', '.join(cc)}"
    return line


def _collect(
    bus: Any,
    *,
    limit: int,
    thread: str | None,
    since: _dt.datetime | None,
    agent: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Page /v1/sent until `limit` matching rows are in hand or the server has
    no more. Returns (rows, why_the_listing_is_incomplete_or_None).

    PAGING PAST THE FIRST PAGE DOES NOT WORK ON THE SERVER TODAY (observed
    2026-09-01, reported to agentbus-8dc08d): the page carries a timestamp
    `cursor`, and sending it back is refused with `validation_error: query.
    cursor: Input should be a valid integer`; any integer past 0 answers
    `internal_error`. A client that crashed there would turn a server defect
    into "the outbox is broken". Instead the first page is shown and the
    reader is TOLD the history was not walked — never a quiet partial list.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        try:
            page = bus.sent(limit=limit, cursor=cursor, agent=agent)
        except AgentBusError as exc:
            if cursor is None:
                raise
            return rows, (
                f"the server refused to page past cursor {cursor!r} ({exc.code}); "
                "only the newest page was searched"
            )
        messages = list(page.get("messages") or [])
        for row in messages:
            if thread and row.get("thread_id") != thread:
                continue
            if since is not None:
                at = _sent_at(row)
                if at is None or at < since:
                    continue
            rows.append(row)
            if len(rows) >= limit:
                return rows, None
        cursor = page.get("cursor")
        if not messages or not cursor:
            return rows, None
        if since is not None:
            # Newest first: once a whole page predates --since, so does the rest.
            oldest = _sent_at(messages[-1])
            if oldest is not None and oldest < since:
                return rows, None
    return rows, f"stopped after {_MAX_PAGES} pages without exhausting the history"


def cmd_sent(args: argparse.Namespace) -> int:
    since = _since_instant(getattr(args, "since", None))
    bus = _common._bus(args)
    try:
        rows, incomplete = _collect(
            bus,
            limit=args.limit,
            thread=getattr(args, "thread", None),
            since=since,
            agent=bus.agent,
        )
    except AgentBusError as exc:
        if exc.status in (404, 405, 501):
            print(
                f"the sent listing is not deployed on this server ({exc.code}) — "
                "GET /v1/sent answered not-found.",
                file=sys.stderr,
            )
            return 1
        raise

    if args.json:
        # Parity with `thread --json` (SPECS/0011): open your own bodies here,
        # so a bulk review does not cost one `show --raw | age` per row.
        for row in rows:
            bus.unseal_message(row)
        _print({"sent": rows, "count": len(rows), "incomplete": incomplete}, True)
        return 0

    if not rows:
        scope = f" in thread {args.thread}" if getattr(args, "thread", None) else ""
        print(f"no sent messages{scope}" + (f" since {args.since}" if since else ""))
    else:
        for row in rows:
            subj = row.get("subject") or "(no subject)"
            sealed = "sealed" if row.get("sealed") else "plain"
            print(f"{row.get('sent_at') or '?':<28} {row.get('message_id') or '?'}  {sealed}")
            print(f"    thread  {row.get('thread_id') or '?'}")
            print(f"    to      {_recipients_line(row)}")
            print(f"    subject {subj}")
        print(f"\n{len(rows)} sent message(s)")
    if incomplete:
        sys.stdout.flush()
        # SAY SO, always — with or without rows. An empty result that was
        # only ever the first page is not "you sent nothing in that thread".
        print(f"incomplete: {incomplete} — narrow --since or raise --limit.", file=sys.stderr)
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sent",
        help="mail YOU sent, newest first — the outbox (#51). What did my daemon post?",
    )
    p.add_argument("--limit", type=int, default=50, help="rows to show (default 50)")
    p.add_argument("--thread", default=None, metavar="THREAD_ID", help="only this conversation")
    p.add_argument(
        "--since",
        default=None,
        help="only rows at or after this instant: ISO-8601 (2026-09-01T12:00:00Z) "
        "or a duration back from now (2h, 90m, 3d)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_sent)
