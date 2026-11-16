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

import sys
from typing import TYPE_CHECKING, Any

from agentbus_client.client import AgentBusError, IncompleteListing, ValidationError
from agentbus_client.client._reminder_paging import API_MAX_LIMIT

from . import _common
from ._common import _parse_duration, _print

if TYPE_CHECKING:
    import argparse

    from agentbus_client.client import AgentBus


def _render(row: dict[str, Any]) -> str:
    """One reminder as a line. Shows WHO and WHEN, which is what a list is for."""
    # #50: THE SERVER SAYS WHO THIS IS FOR; DO NOT INFER IT.
    #
    # This was `row.get("target") or "(you)"`, which is an inference that is
    # right in the common case and wrong in the two that matter. Before the
    # server carried `target`, EVERY reminder rendered "-> (you)", including
    # ones addressed to somebody else (#316).
    #
    # And the `or` fallback is the defect the field exists to remove: a null
    # target means the recipient AGENT NO LONGER EXISTS, so claiming the
    # reminder is yours is a confident wrong answer where "I cannot tell" is the
    # true one. Same rule as blocks: null is unknown, never a default.
    #
    # `self_addressed` is computed server-side on purpose — comparing `target`
    # against whoami would put an identity lookup on the hot path of every row
    # and get it wrong exactly once.
    if row.get("self_addressed"):
        who = "(you)"
    elif row.get("target"):
        who = str(row["target"])
    else:
        who = "(recipient gone)"
    when = str(row.get("due_at") or "")[:19].replace("T", " ")
    state = row.get("state", "?")
    repeat = f"  repeat:{row['repeat']}" if row.get("repeat") else ""
    expires = f"  expires:{str(row['expires_at'])[:10]}" if row.get("expires_at") else ""
    return f"{row.get('id', '?')}  {when}  -> {who:<20} [{state}]{repeat}{expires}"


def _flags_refused(args: argparse.Namespace) -> bool:
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
        return True
    if getattr(args, "delay", None) and getattr(args, "at", None):
        print("--delay and --at say the same thing two ways; pick one", file=sys.stderr)
        return True
    # THE SDK RAISES ValueError FOR THIS; THE CLI MUST NOT LET IT ESCAPE. An
    # unsupported flag is a usage error, and a usage error that arrives as a
    # Python traceback tells the reader their install is broken rather than
    # their command is wrong. Reported by bikeroom-freebsd-operato-b124c2, who
    # hit the raw stack trace while testing the documented refusal.
    if getattr(args, "repeat_until", None):
        print(
            "--repeat-until does not exist, and will not: --expire IS the end "
            "date for a recurrence.\n"
            "  use:  agentbus remind --repeat daily --expire 30d\n"
            "  after --expire the recurrence stops firing entirely (the upstream "
            "schedule is cancelled, not merely withheld).",
            file=sys.stderr,
        )
        return True
    return False


def _print_scheduled(args: argparse.Namespace, result: dict[str, Any]) -> None:
    who = args.target or "you"
    when = str(result.get("due_at") or "")[:19].replace("T", " ")
    print(f"reminder {result['id']} -> {who}")
    print(f"  first fires: {when or '(as scheduled)'}")
    if result.get("repeat"):
        until = str(result.get("repeat_until") or "")[:10]
        print(f"  repeats:     {result['repeat']}" + (f" until {until}" if until else " (no end)"))
    if result.get("expires_at"):
        print(
            f"  expires:     {str(result['expires_at'])[:19].replace('T', ' ')}"
            " (not delivered after this)"
        )
    print(f"  cancel:      agentbus remind --cancel {result['id']}")


def cmd_remind(args: argparse.Namespace) -> int:
    if _flags_refused(args):
        return 2

    bus = _common._bus(args)

    if getattr(args, "cancel", None):
        result = bus.cancel_remind(args.cancel)
        _print(result, as_json=True) if args.json else print(f"cancelled {args.cancel}")
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
        limit = exc.body.get("limit") if exc.code == "limit_reached" else None
        if isinstance(limit, int):
            print(f"  this agent's plan allows {limit} live reminders", file=sys.stderr)
        return 3

    if args.json:
        _print(result, as_json=True)
    else:
        _print_scheduled(args, result)
    return 0


def _page_flags_refused(args: argparse.Namespace) -> bool:
    limit = getattr(args, "limit", None)
    if limit is not None and not 1 <= limit <= API_MAX_LIMIT:
        print(f"--limit takes 1 to {API_MAX_LIMIT}; got {limit}", file=sys.stderr)
        return True
    return False


def _fetch_listing(args: argparse.Namespace, bus: AgentBus, show_all: bool) -> dict[str, Any]:  # noqa: FBT001
    limit = getattr(args, "limit", None) or API_MAX_LIMIT
    cursor = getattr(args, "cursor", None)
    if getattr(args, "all_pages", False):
        return bus.reminds_every_page(all=show_all, limit=limit, cursor=cursor)
    return bus.reminds_page(all=show_all, limit=limit, cursor=cursor)


def _next_page_command(args: argparse.Namespace, show_all: bool, nxt: str) -> str:  # noqa: FBT001
    parts = ["agentbus reminds"]
    if show_all:
        parts.append("--all")
    if getattr(args, "limit", None):
        parts.append(f"--limit {args.limit}")
    parts.append(f"--cursor {nxt}")
    return " ".join(parts)


def _truncation_footer(args: argparse.Namespace, page: dict[str, Any], show_all: bool) -> None:  # noqa: FBT001
    if not page.get("has_more"):
        return
    nxt = page.get("next_cursor")
    if isinstance(nxt, str) and nxt:
        print(
            f"\nSHOWING {page.get('count')} OF {page.get('total')} — more rows exist.\n"
            f"  next page:  {_next_page_command(args, show_all, nxt)}\n"
            f"  every page: agentbus reminds{' --all' if show_all else ''} --all-pages"
        )
        return
    print(
        f"\nSHOWING {page.get('count')} OF {page.get('total')} — this listing is "
        f"TRUNCATED at {page.get('limit')} rows and the server sent no cursor for "
        "the next page."
    )


def _finished_count(bus: AgentBus, page: dict[str, Any]) -> int | None:
    try:
        total_all = bus.reminds_page(all=True, limit=1).get("total")
    except AgentBusError:
        return None
    live_total = page.get("total")
    if not isinstance(total_all, int):
        return None
    if not isinstance(live_total, int):
        live_total = len(page["reminders"])
    return total_all - live_total


def _render_listing(
    args: argparse.Namespace,
    page: dict[str, Any],
    shown: list[dict[str, Any]],
    finished: int | None,
) -> None:
    show_all = getattr(args, "all", False)
    if not shown:
        if finished and finished > 0 and not show_all:
            print(f"no live reminders ({finished} finished — see them with --all)")
        else:
            print("no reminders")
        return
    for row in sorted(shown, key=lambda r: (not r.get("repeat"), r.get("due_at") or "")):
        print(_render(row))
    hidden = 0 if show_all else (finished or 0)
    if hidden > 0:
        print(f"\n({hidden} finished reminder(s) hidden — `agentbus reminds --all`)")
    _truncation_footer(args, page, show_all)
    if any(r.get("repeat") for r in shown):
        print("cancel a recurring one: agentbus remind --cancel <id>")


def cmd_reminds(args: argparse.Namespace) -> int:
    """List reminders, live ones by default; --all adds fired and cancelled rows."""
    if _page_flags_refused(args):
        return 2
    show_all = getattr(args, "all", False)
    bus = _common._bus(args)
    try:
        page = _fetch_listing(args, bus, show_all)
    except IncompleteListing as exc:
        print(f"could not list every reminder: {exc.detail}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"could not list reminders: {exc.detail}", file=sys.stderr)
        return 2
    if getattr(args, "envelope", False):
        _print(page, as_json=True)
        return 0
    rows = page["reminders"]
    shown = rows if show_all else [r for r in rows if r.get("state") == "scheduled"]
    if args.json:
        _print(shown, as_json=True)
        return 0
    finished = None if show_all else _finished_count(bus, page)
    if finished is None and not show_all:
        finished = len(rows) - len(shown)
    _render_listing(args, page, shown, finished)
    return 0
