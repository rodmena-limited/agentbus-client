"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..client import AgentBusError, SelfReplyError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _as_message_id, _parse_duration, _print


def cmd_send_batch(args: argparse.Namespace) -> int:
    """F12 (issuedb #10, SPECS/0010): read JSONL from stdin and send in bulk.

    Per-invocation `agentbus send` pays ~600 ms of process startup +
    config load + key open + sealing setup before it hits the socket, so
    a bash loop tops out at ~1.6 sends/s no matter how much burst budget
    the server has left. This subcommand pays that setup ONCE and reuses
    the same sealing context, the same auth resolution, and the same
    httpx keep-alive across every send in the batch — so throughput
    becomes bounded by network + server (~20+ sends/s under the 40-burst
    server cap), not by fork+exec.

    Input format: one JSON object per line on stdin. Fields match
    `bus.send()` keyword args (to, subject, text, cc, priority, html,
    attachments, payload, guarantee, derived_from, thread_id). `to` may
    be a string or a list.

    Output format: one JSON line per input line, in input order:
      {"index": N, "ok": true,  "result": <server response>}
      {"index": N, "ok": false, "error": {"type": "...", "message": "..."}}

    A single failed send does NOT stop the batch — the point is bulk
    throughput; pass --stop-on-error to fail fast on the first error.
    Exit code: 0 if every send succeeded, 1 if any failed.
    """
    bus = _common._bus(args)
    import json as _json

    stream = sys.stdin
    lines = list(stream) if not stream.isatty() else []
    if not lines:
        print(
            "agentbus send-batch: no input on stdin. Pipe one JSON object per "
            'line: {"to": [...], "subject": "...", "text": "..."}',
            file=sys.stderr,
        )
        return 2

    any_error = False
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue  # blank line separator, tolerated
        try:
            item = _json.loads(line)
        except ValueError as exc:
            _print_batch_error(index, "input_parse_error", str(exc))
            any_error = True
            if args.stop_on_error:
                return 1
            continue
        if not isinstance(item, dict):
            _print_batch_error(index, "input_shape_error", "each line must be a JSON object")
            any_error = True
            if args.stop_on_error:
                return 1
            continue

        to = item.get("to")
        if to is None:
            _print_batch_error(index, "missing_to", "each line must include a 'to' field")
            any_error = True
            if args.stop_on_error:
                return 1
            continue

        try:
            result = bus.send(
                to,
                subject=item.get("subject", ""),
                text=item.get("text"),
                cc=item.get("cc"),
                priority=item.get("priority"),
                html=item.get("html"),
                thread_id=item.get("thread_id"),
                attachments=item.get("attachments"),
                require_available=bool(item.get("require_available", False)),
                require_responsive=bool(item.get("require_responsive", False)),
                payload=item.get("payload"),
                guarantee=item.get("guarantee"),
                derived_from=item.get("derived_from"),
                # No idempotency_key defaulted — SDK mints one per _request.
                # A caller doing retries should supply idempotency_key per
                # line for stable dedup across attempts.
                idempotency_key=item.get("idempotency_key"),
                # Ack-tracking (SPECS/0022): parity with cmd_send.
                # require_ack per line, ack_window accepts a duration string
                # (parsed by _parse_duration) or seconds.
                require_ack=bool(item.get("require_ack", False)),
                ack_window=(
                    _parse_duration(item["ack_window"]) if item.get("ack_window") else None
                ),
            )
        except AgentBusError as exc:
            _print_batch_error(index, exc.__class__.__name__, str(exc))
            any_error = True
            if args.stop_on_error:
                return 1
            continue
        # F13 shape-normalise for fire_and_forget so consumers get a stable
        # {status, guarantee, ...} instead of {} — same rule as cmd_send.
        if item.get("guarantee") == "fire_and_forget":
            normalised = {"status": "accepted", "guarantee": "fire_and_forget"}
            normalised.update(result or {})
            result = normalised
        print(_json.dumps({"index": index, "ok": True, "result": result}, default=str), flush=True)

    return 1 if any_error else 0


def _print_batch_error(index: int, err_type: str, message: str) -> None:
    """One JSON line for a failed send in `agentbus send-batch`."""
    print(
        json.dumps(
            {
                "index": index,
                "ok": False,
                "error": {"type": err_type, "message": message},
            }
        ),
        flush=True,
    )


def cmd_send(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    payload = None
    if args.payload:
        import json as _json

        raw = _common._read_body(args.payload)
        try:
            payload = _json.loads(raw or "")
        except ValueError as exc:
            print(f"--payload is not valid JSON: {exc}", file=sys.stderr)
            return 2
    # Ack-tracking (SPECS/0022): --require-ack marks a message that carries
    # an ASK the recipient must answer, and the server sends exponential
    # reminders until ack or the window elapses. --ack-window defaults to
    # 24h when --require-ack is set. TO only, never CC.
    ack_window = None
    if getattr(args, "require_ack", False):
        import datetime as _dt

        raw = getattr(args, "ack_window", None)
        ack_window = _parse_duration(raw) if raw else _dt.timedelta(hours=24)
    result = bus.send(
        args.to,
        cc=args.cc or None,
        priority=args.priority,
        subject=args.subject,
        text=_common._read_body(args.body),
        attachments=args.attach,
        require_available=args.require_available,
        payload=payload,
        guarantee=args.guarantee,
        derived_from=args.derived_from or None,
        require_ack=bool(getattr(args, "require_ack", False)),
        ack_window=ack_window,
    )
    # F13 (issuedb #7): a fire_and_forget send has no id, no delivery_count,
    # and — against some server versions — an empty response body. Scripts
    # piping this through jq crash on {}. Normalise: always give the caller
    # a stable {status, guarantee} pair, and preserve every real field the
    # server did return on top. Durable sends are untouched.
    if args.guarantee == "fire_and_forget":
        normalised = {"status": "accepted", "guarantee": "fire_and_forget"}
        normalised.update(result or {})
        result = normalised
    if args.json:
        _print(result, True)
    else:
        # #161: the receipt NAMES THE ACTING IDENTITY. An agent that spent an
        # hour sending as somebody else would have caught it on the first
        # message had the receipt said who "as". One second of reading beats
        # an hour of retractions.
        acting = bus.agent or "(key-bound agent)"
        copied = result.get("cc") or []
        # F13 (issuedb #7): fire_and_forget has no id, no thread, no
        # delivery_count — server does not store it. Print an honest summary
        # instead of KeyError-crashing on absent fields.
        if args.guarantee == "fire_and_forget":
            reached = result.get("reached") or result.get("live_subscribers") or 0
            print(f"fire_and_forget accepted as {acting} — reached {reached} live subscriber(s)")
        else:
            summary = f"{result['delivery_count']} recipient(s)"
            if copied:
                summary += f" ({len(copied)} cc: {', '.join(copied)})"
            print(f"sent {result['id']} as {acting} to {summary}")
            print(f"  thread: {result['thread_id']}")
    return 0


def _suggest_reply_target(bus: Any, exc: SelfReplyError) -> str:
    """#53: when a reply would reach only you, name the message you probably
    meant — the LATEST one in that thread from anyone else. Costs two reads,
    and only on the refusal path. Never falls back to sending anything."""
    try:
        parent = bus.read(exc.message_id)
        own_address = parent.get("sender_address")
        thread_id = parent.get("thread_id")
        if not thread_id:
            return f"  (could not find the thread of {exc.message_id})"
        messages = bus.thread(thread_id).get("messages") or []
    except AgentBusError as lookup:
        return f"  (could not look up the thread: {lookup.code})"
    others = [m for m in messages if m.get("sender_address") != own_address]
    if not others:
        return (
            f"  thread {thread_id} has no message from anyone but you — there is "
            "nobody else's message to answer here"
        )
    latest = others[-1]
    who = latest.get("sender_display") or latest.get("sender_address") or "?"
    return (
        f"  latest message from the other party ({who}):\n"
        f"    agentbus reply {latest.get('id')} -b '...'"
    )


def cmd_reply(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    subject = getattr(args, "subject", None) or None
    try:
        result = bus.reply(
            _as_message_id(bus, args.message_id),
            _common._read_body(args.body) or "",
            reply_all=getattr(args, "reply_all", False),
            cc=args.cc or None,
            priority=getattr(args, "priority", None),
            subject=subject,
            attachments=args.attach,
            allow_self=getattr(args, "to_self", False),
        )
    except SelfReplyError as exc:
        # #53: REFUSED, NOT SENT. Say why, say what to do instead, exit 2 so a
        # daemon's `bus: replied` log line cannot be written for this call.
        print(f"refused: {exc.detail}", file=sys.stderr)
        print(_suggest_reply_target(bus, exc), file=sys.stderr)
        print("  to send to yourself on purpose: add --to-self", file=sys.stderr)
        return 2
    acting = bus.agent or "(key-bound agent)"
    if args.json:
        _print(result, True)
    else:
        # NAME WHO IT REACHED. A reply-all that silently dropped a retired
        # participant must not look like one that reached the room.
        who = ", ".join(result.get("recipients") or []) or "?"
        line = f"replied: {result['id']} as {acting} to {who}"
        if subject:
            line += f'  subject "{subject}"'
        if result.get("cc"):
            line += f" (cc: {', '.join(result['cc'])})"
        if result.get("skipped_retired"):
            line += f"  [skipped, retired: {', '.join(result['skipped_retired'])}]"
        print(line)
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("send", help="send a message")
    p.add_argument("to", nargs="+")
    p.add_argument(
        "-c",
        "--cc",
        action="append",
        default=[],
        help="copy someone: same delivery, but marked 'informed' rather than 'expected to act'",
    )
    p.add_argument(
        "-p",
        "--priority",
        choices=("urgent", "normal", "background"),
        default=None,
        help="urgent jumps the recipient's triage queue; background yields to it "
        "(default normal). Waiting messages age up, so background still arrives.",
    )
    p.add_argument("-s", "--subject", default="")
    p.add_argument("-b", "--body", default=None, help="text, @file, or @- for stdin")
    p.add_argument("-a", "--attach", action="append", default=[])
    p.add_argument(
        "--require-available",
        action="store_true",
        help="refuse rather than queue if the recipient has declared itself busy (#168). "
        "`--require-responsive` asks whether anyone is HOME; this asks whether "
        "anyone is FREE, which only matters when you would rather route elsewhere.",
    )
    p.add_argument(
        "--payload",
        default=None,
        metavar="JSON",
        help="a structured body (#169): literal JSON, @file, or @- for stdin. "
        "If the room declares a schema, this is validated BEFORE the message "
        "is accepted, so a bad payload is your error rather than every consumer's.",
    )
    p.add_argument(
        "--derived-from",
        dest="derived_from",
        action="append",
        default=[],
        metavar="MESSAGE_ID",
        help="declare an input this message was built from (#174). Repeatable. "
        "Recorded as YOUR claim — the bus observes messages, not "
        "transformations, and says so on every read.",
    )
    p.add_argument(
        "--guarantee",
        choices=("durable", "fire_and_forget"),
        default=None,
        help="fire_and_forget trades durability for cost: not stored, not ackable, "
        "never redelivered (#172). Right for a heartbeat, wrong for anything "
        "you would miss. Default durable.",
    )
    p.add_argument(
        "--require-ack",
        action="store_true",
        help="this message carries an ASK the recipient must answer; the server "
        "sends exponential reminders until they ack or the window elapses "
        "(SPECS/0022). TO only, never CC. Use for messages with a decision, "
        "question, or task — NOT for updates, FYIs, or discussions, or reminders "
        "become wallpaper. Forward-compatible: safe against servers that predate "
        "ack-tracking.",
    )
    p.add_argument(
        "--ack-window",
        default=None,
        metavar="DURATION",
        help="how long to keep reminding (default 24h when --require-ack is set). "
        "Accepts 90m, 2h, 3d, or bare seconds. Server caps at 7 days (168h).",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser(
        "send-batch",
        help="pipe JSON lines from stdin and send many messages in one process "
        "(F12, issuedb #10) — reuses sealing context + HTTP keep-alive, so "
        "throughput is bounded by network + server (not by ~600 ms process "
        "startup per invocation).",
    )
    p.add_argument(
        "--stop-on-error",
        dest="stop_on_error",
        action="store_true",
        help="fail fast on the first failed send (default: continue, emit "
        "error lines, exit non-zero at the end)",
    )
    p.add_argument("--agent", help="acting agent (may also precede the subcommand)")
    p.set_defaults(func=cmd_send_batch)

    p = sub.add_parser("reply", help="reply to a message")
    p.add_argument("message_id")
    p.add_argument(
        "--all",
        dest="reply_all",
        action="store_true",
        help="reply to EVERYONE on the parent message (sender + its To, Cc kept as Cc, "
        "you excluded). Off by default — the quiet reply is the safe one.",
    )
    p.add_argument("-c", "--cc", action="append", default=[], help="copy extra recipients")
    p.add_argument(
        "-s",
        "--subject",
        default=None,
        help="give THIS reply its own subject (#52) instead of the server's "
        "'Re: <parent>'. Stored on the message and shown in the thread view — "
        "use it when a long thread's original subject no longer describes what "
        "you are saying, or to carry a severity a reader can see without opening "
        "the body.",
    )
    p.add_argument(
        "--to-self",
        dest="to_self",
        action="store_true",
        help="allow a reply whose ONLY recipient is you (#53). Without this, "
        "replying to your own outbound message id is refused, because 'answer "
        "the sender' would deliver to your own inbox while the other party waits.",
    )
    p.add_argument(
        "-p",
        "--priority",
        choices=("urgent", "normal", "background"),
        default=None,
    )
    p.add_argument("-b", "--body", default=None)
    p.add_argument("-a", "--attach", action="append", default=[])
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_reply)
