"""`agentbus remind` — schedule a message into an agent's inbox.

THE SELF-NOTE IS THE COMMON CASE, so `--target` is optional and defaults to this
agent. `agentbus remind -m "check the deploy" --delay 2h` reminds you; adding
`--target alice` reminds her.

THE BODY IS SEALED BEFORE IT LEAVES THIS MACHINE on an encrypted workspace. That
happens in the SDK (`AgentBus.remind`), not here, but it is the reason this
command exists in the client rather than being a server-side scheduler with a
web form: only the machine holding the key can seal, and a reminder that sits at
rest until it is due is exactly the thing you do not want stored in the clear.

THIS FILE KNOWS NOTHING ABOUT THE SCHEDULER. AgentBus's backend holds one
credential for it; no scheduler key ever reaches a user's machine (operator
ruling, 2026-08-21). The client posts to its own backend and stops there.
"""

from __future__ import annotations

import argparse
import sys

from ..client import AgentBusError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _parse_duration, _print


def _render(row: dict) -> str:
    """One reminder as a line. Shows WHO and WHEN, which is what a list is for."""
    who = row.get("target") or "(you)"
    when = str(row.get("due_at") or "")[:19].replace("T", " ")
    state = row.get("state", "?")
    repeat = f"  repeat:{row['repeat']}" if row.get("repeat") else ""
    expires = f"  expires:{str(row['expires_at'])[:10]}" if row.get("expires_at") else ""
    return f"{row.get('id', '?')}  {when}  -> {who:<20} [{state}]{repeat}{expires}"


def cmd_remind(args: argparse.Namespace) -> int:
    # ARGUMENT VALIDATION BEFORE THE CLIENT IS BUILT. `_bus()` resolves a
    # credential and opens a connection; doing that first means a plain typo in
    # the flags fails with a credential error on a machine that is not signed
    # in, which names the wrong problem entirely. Nothing below needs the
    # network to know the arguments are wrong.
    if args.repeat and (getattr(args, "delay", None) or getattr(args, "at", None)):
        other = "--delay" if args.delay else "--at"
        print(
            f"a recurring reminder takes its first fire from the cron itself — "
            f"drop {other}, or drop --repeat to schedule a single message",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "delay", None) and getattr(args, "at", None):
        print("--delay and --at say the same thing two ways; pick one", file=sys.stderr)
        return 2

    bus = _common._bus(args)

    if getattr(args, "cancel", None):
        result = bus.cancel_remind(args.cancel)
        _print(result, True) if args.json else print(f"cancelled {args.cancel}")
        return 0

    text = _common._read_body(args.message)
    if not text:
        print("nothing to remind about: pass -m/--message (text, @file, or @-)", file=sys.stderr)
        return 2

    # A reminder needs a WHEN. Without one this is just `send`, and silently
    # delivering now would be a surprising reading of a command named "remind".
    if not args.delay and not args.at and not args.repeat:
        print(
            "when? pass --delay 2h, --at '2026-08-22 09:00', or --repeat daily",
            file=sys.stderr,
        )
        return 2

    try:
        result = bus.remind(
            text,
            target=args.target,
            subject=args.subject or "",
            delay=_parse_duration(args.delay) if args.delay else None,
            at=args.at,
            expire=_parse_duration(args.expire) if args.expire else None,
            repeat=args.repeat,
            repeat_until=args.repeat_until,
            timezone=args.timezone,
        )
    except AgentBusError as exc:
        # NAME THE LIKELY CAUSE RATHER THAN ECHO THE STATUS. The two refusals a
        # caller will actually hit are a target with no published key on an
        # encrypted workspace, and the scheduler's capacity cap. Both are
        # actionable; "429" and "422" alone are not.
        print(f"could not schedule: {exc}", file=sys.stderr)
        return 3

    if args.json:
        _print(result, True)
        return 0

    who = args.target or "you"
    when = str(result.get("due_at") or "")[:19].replace("T", " ")
    print(f"reminder {result['id']} -> {who}")
    print(f"  first fires: {when or '(as scheduled)'}")
    if result.get("repeat"):
        until = str(result.get("repeat_until") or "")[:10]
        print(f"  repeats:     {result['repeat']}" + (f" until {until}" if until else " (no end)"))
    if result.get("expires_at"):
        print(f"  expires:     {str(result['expires_at'])[:19].replace('T', ' ')}"
              " (not delivered after this)")
    print(f"  cancel:      agentbus remind --cancel {result['id']}")
    return 0


def cmd_reminds(args: argparse.Namespace) -> int:
    """List reminders — LIVE ONES BY DEFAULT.

    THE POINT OF THIS COMMAND IS TO FIND SOMETHING TO CANCEL, and a list where
    every fired and cancelled reminder is mixed in with the two that are still
    pending does not serve that. After a handful of one-shots the live entries
    are a minority of the output, and a recurring reminder — the thing you most
    need to find, because it fires forever until someone stops it — is buried
    among dead rows that read almost identically.

    So the default is live: `scheduled` only. `--all` shows history, and the
    footer says how many were hidden so the filtering is never silent.
    """
    rows = _common._bus(args).reminds(all=getattr(args, "all", False))
    show_all = getattr(args, "all", False)
    live = [r for r in rows if r.get("state") == "scheduled"]
    shown = rows if show_all else live

    if args.json:
        # --json is the machine surface and must not lose data to a display
        # choice: it returns whatever the server sent for the requested scope.
        _print(shown, True)
        return 0

    if not shown:
        if rows and not show_all:
            print(f"no live reminders ({len(rows)} finished — see them with --all)")
        else:
            print("no reminders")
        return 0

    # Recurring first: they are the ones that keep firing until cancelled, so
    # they are what a reader is most often here to find.
    for row in sorted(shown, key=lambda r: (not r.get("repeat"), r.get("due_at") or "")):
        print(_render(row))

    hidden = len(rows) - len(shown)
    if hidden:
        print(f"\n({hidden} finished reminder(s) hidden — `agentbus reminds --all`)")
    if any(r.get("repeat") for r in shown):
        print("cancel a recurring one: agentbus remind --cancel <id>")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser(
        "remind",
        help="schedule a message into an agent's inbox (yours, unless --target)",
        description=(
            "Schedule a reminder. With no --target this is a NOTE TO YOURSELF, which "
            "is the common case. The body is sealed on this machine before it is "
            "uploaded, so it sits encrypted until it is due."
        ),
    )
    p.add_argument("-m", "--message", default=None, help="text, @file, or @- for stdin")
    p.add_argument(
        "--target",
        default=None,
        help="who to remind (default: YOU). Naming someone else schedules a message "
        "into their inbox, so say something they will understand out of context.",
    )
    p.add_argument("-s", "--subject", default=None)
    p.add_argument(
        "--delay",
        default=None,
        metavar="DURATION",
        help="fire after this long: 90m, 2h, 3d, or bare seconds",
    )
    p.add_argument(
        "--at",
        default=None,
        metavar="WHEN",
        help="fire at an absolute time (ISO-8601). Mutually exclusive with --delay",
    )
    p.add_argument(
        "--expire",
        default=None,
        metavar="DURATION",
        help="do NOT deliver if it would fire later than this — a stale reminder is "
        "worse than none. Same duration format as --delay.",
    )
    p.add_argument(
        "--repeat",
        default=None,
        metavar="RULE",
        help="recurring: daily, weekly, monthly, or a 5-field cron expression",
    )
    p.add_argument(
        "--repeat-until",
        dest="repeat_until",
        default=None,
        metavar="WHEN",
        help="NOT YET SUPPORTED SERVER-SIDE — the create is refused if you pass "
        "it. A recurrence currently has no end date; cancel it explicitly with "
        "--cancel. Kept in the interface because a recurrence with no end is a "
        "commitment nobody remembers making, and this should come back.",
    )
    p.add_argument(
        "--timezone",
        default=None,
        metavar="IANA",
        help="zone for --repeat (e.g. Europe/London). Needed so 'daily at 9' means "
        "9 where you are; a UTC offset like +01:00 is not accepted and would be "
        "wrong across a DST boundary anyway.",
    )
    p.add_argument("--cancel", default=None, metavar="ID", help="cancel a scheduled reminder")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_remind)

    p = sub.add_parser(
        "reminds",
        help="list scheduled reminders (agent tags are `tag`; ack-chasing is `reminders`)",
        description=(
            "Reminders not yet delivered. NOT `agentbus reminders`, which is "
            "ack-tracking — that chases messages already sent; this lists messages "
            "not yet sent."
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="include FINISHED reminders (fired and cancelled). The default "
        "shows only live ones, because this command exists to find something "
        "to cancel and dead rows crowd out the ones you can still act on.",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_reminds)
