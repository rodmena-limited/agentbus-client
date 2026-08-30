"""`agentbus block` / `unblock` / `blocks` — refusing a peer's mail (#48).

Operator's request: "agents need to block spammers (even if trusted in
workspace), sometimes zombie agents annoy others."

The blocking is done by the SERVER, because the cost being complained about is
the WAKE. A client-side filter would drop the message after it had already
interrupted the session, which removes the annoyance from the transcript and
none of the expense.
"""

from __future__ import annotations

import argparse
import sys

from .._timefmt import _looks_like_duration
from . import _common
from ._common import _accept_common_flags_after_subcommand, _print


def _resolve_self(args: argparse.Namespace) -> str | None:
    """This agent's own name, for the self-block refusal, without a round-trip."""
    import os

    return getattr(args, "agent", None) or os.environ.get("AGENTBUS_AGENT")


def cmd_block(args: argparse.Namespace) -> int:
    # Validate BEFORE _bus(): a self-block is a mistake about WHO, and reporting
    # it as a credential error sends the reader hunting for a key problem they
    # do not have (the lesson from `remind`'s flag validation).
    me = _resolve_self(args)
    if me and args.name == me:
        print(
            f"refusing to block yourself ({me}). A self-block makes you "
            "unreachable by the peers who would tell you something is wrong, "
            "and nothing you send yourself is the noise you are trying to stop.",
            file=sys.stderr,
        )
        return 2

    # `--for` PROMISES A DURATION, so a typo is refused here rather than sent.
    # `_as_instant` deliberately passes any string through ("server validates"),
    # which is right for `remind --at`, where an ISO instant is what the caller
    # means. It is wrong for a duration flag: `--for tomorrow` would travel to
    # the server as expires_at="tomorrow", and the operator would get a schema
    # error about a field they never typed, for a word they did.
    if args.for_ is not None and not _looks_like_duration(args.for_):
        print(
            f"--for takes a duration like 2h, 90m or 3d — not {args.for_!r}. "
            "For an absolute end date, block without --for and unblock when done.",
            file=sys.stderr,
        )
        return 2

    bus = _common._bus(args)
    result = bus.block(args.name, reason=args.reason, for_=args.for_)
    if args.json:
        _print(result, True)
        return 0
    until = result.get("expires_at")
    print(f"blocked {args.name}")
    print("  their mail no longer reaches your inbox and no longer wakes you")
    if until:
        print(f"  expires:  {until}   (delivery resumes on its own)")
    else:
        # Say the quiet part: an unbounded block is the one that rots.
        print("  expires:  never — `agentbus blocks` shows what it has suppressed")
    print(f"  undo:     agentbus unblock {args.name}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    result = bus.unblock(args.name)
    if args.json:
        _print(result, True)
        return 0
    print(f"unblocked {args.name} — delivery resumes")
    held = result.get("suppressed_count")
    if held:
        # What you missed is the reason to know a block existed.
        print(f"  {held} message(s) were suppressed while it was in force")
    return 0


def cmd_blocks(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    rows = bus.blocks()
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        print("no blocks — every agent in the workspace can reach you")
        return 0
    # LAPSED BLOCKS ARE LISTED, NOT HIDDEN — the server's decision, and the
    # right one: a block that quietly expired is how a recipient finds out weeks
    # later that it has been reachable all along by someone it believed it had
    # stopped. But listing them is only half the job: rendering a lapsed row the
    # same as a live one makes the reader do date arithmetic to discover they
    # are unprotected. `active` is a field; say it.
    live = [r for r in rows if r.get("active", True)]
    lapsed = [r for r in rows if not r.get("active", True)]

    print(f"{len(live)} active block(s):" if live else "no ACTIVE blocks:")
    for row in live:
        name = row.get("agent") or "?"
        held = row.get("suppressed_count") or 0
        until = row.get("expires_at")
        reason = row.get("reason")
        window = f"until {until}" if until else "no expiry"
        print(f"  {name:32} {held:>5} suppressed   {window}")
        if reason:
            print(f"  {'':32} reason: {reason}")

    if lapsed:
        print(f"\n{len(lapsed)} EXPIRED — these peers can reach you again:")
        for row in lapsed:
            name = row.get("agent") or "?"
            held = row.get("suppressed_count") or 0
            print(f"  {name:32} {held:>5} suppressed before it lapsed")
        print("  re-block with `agentbus block <agent>` if the peer is still a problem.")
    # The counter is the whole accountability mechanism. Suppressed mail is
    # REFUSED at the server, not stored — so this number is the only thing that
    # separates "I am blocking a live peer" from "that peer went quiet", and a
    # block nobody re-reads is how you lose a colleague without noticing.
    print("\n  suppressed counts climbing = that peer is alive and being refused.")
    print("  static = they stopped sending. Their mail is refused, never stored,")
    print("  so this counter is the only record that the block is doing anything.")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "block",
        help="stop a peer's mail reaching you — even one trusted by the workspace",
    )
    p.add_argument("name", help="the agent to block")
    p.add_argument("--reason", default=None, help="why, for your own later reference")
    p.add_argument(
        "--for",
        dest="for_",
        default=None,
        metavar="DURATION",
        help="expire the block automatically (2h, 3d). RECOMMENDED for a zombie: "
        "the process gets restarted and a permanent block then silently drops "
        "legitimate mail from the same name",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("unblock", help="resume delivery from a blocked peer")
    p.add_argument("name")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_unblock)

    p = sub.add_parser("blocks", help="who you are blocking, and what it has suppressed")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_blocks)
