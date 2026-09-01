"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..client import AgentBusError, NotFoundError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _print


def _render_thread(result: dict[str, Any], highlight_message_id: str | None = None) -> None:
    """Print a whole conversation, oldest first.

    #216. The previous rendering printed sender, timestamp and prose, and that
    was not enough to ACT on:

      * no message ids, so an agent that had just read the chain could not cite
        any of it. Citing the delivery id it happens to hold is the mistake this
        client already warns about elsewhere — delivery ids are per-recipient and
        do not resolve for the person you are talking to.
      * no attachment or payload line, so a message whose whole point was the
        thing it carried read as an empty remark. #212 settled that for a single
        delivery; a thread is just as capable of hiding it.
      * nothing marking WHICH message you arrived from, which is the one thing
        the reader already knows and wants anchored.
    """
    thread = result["thread"]
    messages = result.get("messages") or []
    print(f"# {thread['subject']}  [{thread['state']}]")
    print(f"  thread {thread['id']}   {len(messages)} message(s)")
    for position, message in enumerate(messages, start=1):
        # POSITION IN THE CONVERSATION, counted here — NOT m.thread_seq.
        # thread_seq counts each SENDER's own messages in the thread, so
        # rendering it as the bracketed number printed "[1] [2] [1]" for a
        # three-message exchange: it reads as a position, and it is not one.
        mark = "  <-- the one you opened" if message.get("id") == highlight_message_id else ""
        print(
            f"\n--- [{position}/{len(messages)}] "
            f"{message['sender_display'] or message['sender_address']} "
            f"({message['created_at']}){mark}"
        )
        # THE ID GOES ON ITS OWN LINE, ALWAYS. A reader quoting a conversation
        # back to a peer needs the message id, and printing it only sometimes is
        # how it gets left out of the one message that mattered.
        print(f"    message {message['id']}")
        count = message.get("attachment_count") or 0
        if count:
            print(f"    {count} attachment(s) — fetch with: agentbus attachment <delivery-id>")
        if message.get("payload") is not None:
            ref = message.get("payload_schema_ref")
            print(f"    carries a structured payload{f' ({ref})' if ref else ''}")
        print()
        # A READER THAT CANNOT DECRYPT MUST SAY SO — the rule
        # `unseal_message` states in its own docstring, and which `show`
        # already honours (client.py checks `sealed_unreadable` before
        # rendering a body). This sibling did not: it printed
        # `text_body` unconditionally, which on an un-openable message is
        # the raw age armor. An operator reading a thread got a wall of
        # base64 with no explanation — the exact "returning ciphertext as
        # if it were content" failure that docstring exists to prevent.
        #
        # Reachable in ordinary use: a thread you are a participant in can
        # contain messages sealed only to OTHER recipients (you see the
        # envelope, you cannot open the body). Found by reading a live
        # 5-message thread where 3 opened and 2 did not.
        unreadable = message.get("sealed_unreadable")
        if unreadable:
            print(f"(sealed — cannot read on this machine: {unreadable})")
        else:
            print(message.get("text_body") or "(no text body)")


def cmd_thread(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    try:
        result = bus.thread(args.thread_id)
    except NotFoundError as exc:
        # #54: the mirror of `show`'s fallback — a DELIVERY id pasted here
        # resolves to its thread, once, on the failure path only.
        try:
            delivery = bus.read(args.thread_id)
        except AgentBusError:
            print(
                f"not_found: {args.thread_id} is neither a thread you are in nor a "
                "delivery of yours. `thread` takes a THREAD id; `show` takes a "
                "DELIVERY id (from your inbox). They look alike and are not the same.",
                file=sys.stderr,
            )
            raise exc from None
        thread_id = delivery.get("thread_id")
        if not thread_id:
            raise exc from None
        print(
            f"note: {args.thread_id} is a DELIVERY id; its thread is {thread_id}.",
            file=sys.stderr,
        )
        result = bus.thread(thread_id)
    if args.json:
        _print(result, True)
        return 0
    _render_thread(result)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Catch up on a room joined mid-conversation (#170).

    A room is a conversation. An agent that joins one halfway otherwise sees
    every future message and none of the context that makes them mean anything.
    """
    bus = _common._bus(args)
    result = bus.room_history(args.room, limit=args.limit, since=args.since)
    if args.json:
        _print(result, True)
        return 0
    messages = result.get("messages") or []
    if not messages:
        print(f"room:{args.room} — nothing before you joined")
        return 0
    print(f"room:{args.room} — {len(messages)} earlier message(s)")
    for m in messages:
        # `created_at` and `sender` — the field names this endpoint ACTUALLY
        # returns. My first version guessed `sent_at`/`sender_display` from the
        # inbox payload and printed a blank timestamp for every row: a display
        # that silently renders nothing where a value belongs, which is the same
        # shape as the columns that were written and never selected.
        when = str(m.get("created_at") or "")[:19].replace("T", " ")
        sender = m.get("sender") or "?"
        print(f"  {when}  {sender}: {(m.get('subject') or '(no subject)')[:70]}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Read, declare or clear a room's payload contract (#169).

    Readable by any member on purpose: a producer must be able to see what it is
    expected to send BEFORE being refused for getting it wrong. A contract you
    can only discover by violating it is not a contract.
    """
    import json as _json

    bus = _common._bus(args)
    if args.set is None and not args.clear:
        result = bus.room_schema(args.room)
        if args.json:
            _print(result, True)
            return 0
        schema = result.get("schema")
        if schema is None:
            print(f"room:{args.room} declares no payload schema — any payload is accepted")
        else:
            print(f"room:{args.room} schema (version {result.get('version', '?')}):")
            print(_json.dumps(schema, indent=2))
        return 0

    if args.clear:
        schema = None
    else:
        raw = _common._read_body(args.set)
        try:
            schema = _json.loads(raw or "")
        except ValueError as exc:
            print(f"--set is not valid JSON: {exc}", file=sys.stderr)
            return 2
    result = bus.set_room_schema(args.room, schema)
    _print(
        result
        if args.json
        else (
            f"room:{args.room} schema {'cleared' if schema is None else 'declared'} "
            f"(version {result.get('version', '?')}) — it applies to messages sent AFTER now"
        ),
        args.json,
    )
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    usage = _common._bus(args).usage()
    if args.json:
        _print(usage, True)
        return 0
    for policy in usage["policies"]:
        used = policy["used"] if policy["used"] is not None else "-"
        window = (policy.get("window") or {}).get("reset_at", "")
        print(
            f"{policy['name']:<36} {used}/{policy['limit']}  remaining={policy['remaining']}  {window}"
        )

    # Key-cap pressure, shown HERE because this is the command an operator runs
    # before deciding whether to clean up. It was invisible during the incident
    # that motivated it: the workspace sat at its ceiling, the only symptom was a
    # mint failing, and the response to that is a sweep — which destroyed a live
    # credential. A near-full line here is the warning that makes sweeping a
    # choice rather than a reflex. Tolerates an older server that omits it.
    keys = usage.get("keys") or {}
    for name, label in (("bound_send", "keys (bound send)"), ("operator", "keys (operator)")):
        entry = keys.get(name)
        if not entry:
            continue
        used, limit = entry["used"], entry["limit"]
        near = "  <- near the cap" if limit and used >= limit * 0.8 else ""
        print(f"{label:<36} {used}/{limit}  remaining={limit - used}{near}")
    return 0


def cmd_reminders(args: argparse.Namespace) -> int:
    """`agentbus reminders` — ack-tracking visibility (SPECS/0022).

    Two views, mirroring the phrase that names them:
      --owing  what I sent and am still waiting to be acked
      --owed   what was sent TO me that I still owe an ack on

    Both list only UNRESOLVED rows (acked/replied/expired drop off), oldest
    first. Reads only, scoped to the caller's own agent.

    Forward-compatible: against a server without the reminders endpoints,
    the verb prints "not enabled on this server yet" and exits 1 rather
    than a traceback.
    """
    bus = _common._bus(args)
    try:
        if getattr(args, "owed", False):
            rows = bus.reminders_owed()
            kind = "owed"
        else:
            rows = bus.reminders_owing()
            kind = "owing"
    except AgentBusError as exc:
        if exc.status in (404, 405, 501):
            print(
                f"ack-tracking not enabled on this server yet ({exc.code}) — "
                "the delivery_reminders endpoints are not deployed here.",
                file=sys.stderr,
            )
            return 1
        raise

    if args.json:
        _print({kind: rows, "count": len(rows)}, True)
        return 0

    if not rows:
        print(f"no {kind} reminders — {len(rows)}")
        return 0

    # Header + rows. The ROW shape differs slightly between the two views:
    #   owing -> recipient_name (who I sent it to and am waiting on)
    #   owed  -> sender_name    (who sent it to me)
    peer_key = "recipient_name" if not getattr(args, "owed", False) else "sender_name"
    for row in rows:
        subj = row.get("subject") or "(no subject)"
        required = row.get("required_by") or "?"
        attempts = row.get("attempts_so_far") or 0
        next_at = row.get("next_attempt_at") or "-"
        peer = row.get(peer_key) or "?"
        print(
            f"{kind:<6} {peer:<24} {subj[:40]:<40} "
            f"required_by={required} attempts={attempts} next={next_at}"
        )
    print(f"\n{len(rows)} reminder(s)")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("thread", help="show a whole conversation")
    p.add_argument("thread_id")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("history", help="what was said in a room before you joined (#170)")
    p.add_argument("room", help="room name, without the room: prefix")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--since", default=None, metavar="ISO8601")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("schema", help="read, declare or clear a room's payload contract (#169)")
    p.add_argument("room")
    p.add_argument(
        "--set", default=None, metavar="JSON", help="literal JSON, @file, or @- for stdin"
    )
    p.add_argument("--clear", action="store_true", help="remove the contract")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("usage", help="show quota usage")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser(
        "reminders",
        help="ack-tracking visibility (SPECS/0022). Defaults to --owing: what "
        "you sent and are still waiting on. --owed shows what was sent TO you "
        "that you owe an ack on. Reads only, scoped to your own agent.",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--owed",
        action="store_true",
        help="show messages TO me that I owe an ack on (the recipient view)",
    )
    grp.add_argument(
        "--owing",
        action="store_true",
        help="show messages I sent that I'm still waiting to be acked "
        "(the sender view; the default)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_reminders)
