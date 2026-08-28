"""`agentbus memory` — the agent's own notebook (server #341).

ONE VERB WITH SUBACTIONS, not five top-level verbs. `memory`, `memory fetch`,
`memory rm 7`, `memory truncate --first 10`, `memory reseal`. The bare form
WRITES, because that is the thing an agent does forty times for every once it
does anything else, and making the common case the shortest is the whole point
of a notebook.

WHAT THE OUTPUT MUST NOT DO, and both were live defects in adjacent features
here before they were rules:

  * present an entry it could not decrypt as if it were content. The client
    marks those `opened: false`; this renders them as an explicit line naming
    the seq and the reason, never as ciphertext dressed up as text.
  * print `position` where `seq` belongs. The display column runs 1..N and the
    ADDRESS does not — after any delete they differ, and `rm 2` meaning "the
    second row" instead of "seq 2" would delete the wrong note. Both are shown,
    seq is what every command takes.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Any

from ..client import AgentBusError
from ._common import _accept_common_flags_after_subcommand, _bus, _print, _read_body


def _render(result: dict[str, Any]) -> None:
    entries = result.get("memory") or []
    used = result.get("bytes_used", 0)
    limit = result.get("bytes_limit", 0)
    count = result.get("entries", len(entries))
    entry_limit = result.get("entries_limit", 0)

    if not entries:
        print("memory is empty")
    for entry in entries:
        seq = entry.get("seq")
        text = entry.get("text", "")
        if entry.get("opened") is False:
            # NEVER the ciphertext, and never silence. A reader who sees armor
            # concludes the feature is broken; a reader who sees nothing
            # concludes the note was lost. Say which key is missing and where.
            print(f"{entry.get('position'):>3}. [seq {seq}]  << SEALED, NOT READABLE HERE >>")
            print(f"      {entry.get('open_error', 'no key on this machine opens it')}")
            continue
        first, *rest = text.splitlines() or [""]
        print(f"{entry.get('position'):>3}. [seq {seq}]  {first}")
        for line in rest:
            print(f"      {line}")

    pct = (used * 100 // limit) if limit else 0
    print()
    print(f"  {count}/{entry_limit} entries · {used}/{limit} stored bytes ({pct}%)")
    if result.get("unopened_seqs"):
        print(
            f"  {len(result['unopened_seqs'])} entr"
            f"{'y' if len(result['unopened_seqs']) == 1 else 'ies'} could not be opened on this "
            f"machine (seqs {result['unopened_seqs']}). If you rotated keys elsewhere, the "
            f"superseded private key is on that host. `agentbus memory reseal` there, then "
            f"they open anywhere."
        )
    # THE OVERHEAD WARNING, printed where it can change behaviour rather than
    # buried in docs. Each entry pays a fixed ~355-byte age header on an
    # encrypted workspace, so a notebook of one-liners costs several times what
    # the same words cost as paragraphs.
    if count and limit and used > limit * 0.8:
        print(
            "  over 80% full. Short entries are expensive (a ~355-byte seal header each): "
            "consolidating one-liners into paragraphs reclaims more than deleting a few."
        )


def cmd_memory(args: argparse.Namespace) -> int:
    bus = _bus(args)
    action = args.action

    try:
        if action is None or action == "add":
            text = _read_body(args.text) if args.text else None
            if not text or not text.strip():
                print(
                    "nothing to remember: pass the text, or @file, or @- for stdin",
                    file=sys.stderr,
                )
                return 2
            result = bus.memory_add(text, agent=args.agent)
            if args.json:
                _print(result, True)
            else:
                print(
                    f"remembered as seq {result['seq']} "
                    f"({result['bytes']} stored bytes; "
                    f"{result['bytes_used']}/{result['bytes_limit']} used, "
                    f"{result['entries']}/{result['entries_limit']} entries)"
                )
            return 0

        if action == "fetch":
            result = bus.memory_fetch(agent=args.agent)
            if args.json:
                _print(result, True)
            else:
                _render(result)
            return 0

        if action == "rm":
            result = bus.memory_delete(args.seq, agent=args.agent)
            if args.json:
                _print(result, True)
            else:
                print(
                    f"removed seq {args.seq} "
                    f"({result['bytes_used']}/{result['bytes_limit']} used, "
                    f"{result['entries']} entries left). Survivors keep their numbers."
                )
            return 0

        if action == "truncate":
            result = bus.memory_truncate(first=args.first, agent=args.agent)
            removed = result.get("removed", [])
            if args.json:
                _print(result, True)
            else:
                # WHAT WAS ACTUALLY REMOVED, not what was asked for. `--first 10`
                # against a store of 3 removes 3, and a caller told "10" carries
                # a wrong idea of its own state.
                print(
                    f"removed {len(removed)} of the {args.first} oldest "
                    f"(seqs {removed or '—'}); "
                    f"{result['bytes_used']}/{result['bytes_limit']} used, "
                    f"{result['entries']} entries left"
                )
            return 0

        if action == "reseal":
            result = bus.memory_reseal(agent=args.agent)
            if args.json:
                _print(result, True)
                return 0
            resealed = result.get("resealed", [])
            stuck = result.get("unrecoverable", [])
            if not resealed and not stuck:
                print(result.get("detail", "nothing to re-seal"))
            else:
                print(
                    f"re-sealed {len(resealed)} entr{'y' if len(resealed) == 1 else 'ies'} "
                    f"to your current key, keeping every seq ({resealed})"
                )
            if stuck:
                # A NON-ZERO EXIT, because this is the case the command exists
                # to surface and a green run would bury it.
                print(
                    f"  {len(stuck)} could NOT be opened on this machine and were LEFT "
                    f"UNTOUCHED (seqs {stuck}). They were not re-sealed to the new key on "
                    f"purpose: sealing ciphertext again would make them permanently "
                    f"unreadable. Find the superseded key file, or run reseal on the host "
                    f"that has it.",
                    file=sys.stderr,
                )
                return 1
            return 0

    except AgentBusError as exc:
        # `memory_full` and `memory_entry_too_large` have OPPOSITE remedies, and
        # the server sends the numbers needed to act on each. Printing the raw
        # error would make the agent guess, and the common guess (truncate) is
        # exactly wrong for the second one.
        code = getattr(exc, "code", "")
        extra = getattr(exc, "payload", None) or {}
        if code == "memory_full":
            oldest = extra.get("oldest_seq")
            print(f"memory full: {exc.detail}", file=sys.stderr)
            print(
                "  free space with:  agentbus memory truncate --first 10"
                + (f"   (oldest is seq {oldest})" if oldest is not None else ""),
                file=sys.stderr,
            )
            return 4
        if code == "memory_entry_too_large":
            print(f"entry too large: {exc.detail}", file=sys.stderr)
            print(
                "  truncating will NOT help — the limit is PER ENTRY. Write a shorter note.",
                file=sys.stderr,
            )
            return 4
        raise

    print(f"unknown memory action '{action}'", file=sys.stderr)
    return 2


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire `memory` into the shared subparser."""
    p = sub.add_parser(
        "memory",
        help="your own notebook: remember something, or read it all back",
        description=(
            "agentbus memory 'always quote the staging DSN'   remember it\n"
            "agentbus memory fetch                            read it all back\n"
            "agentbus memory rm 7                             forget one entry (by seq)\n"
            "agentbus memory truncate --first 10              forget the 10 OLDEST\n"
            "agentbus memory reseal                           re-seal to your current key"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "action",
        nargs="?",
        default=None,
        help="fetch | rm | truncate | reseal; omit it to REMEMBER the text that follows",
    )
    p.add_argument(
        "text",
        nargs="?",
        default=None,
        help="what to remember (also @file, or @- for stdin)",
    )
    p.add_argument("--seq", type=int, default=None, help="entry to remove (with rm)")
    p.add_argument("--first", type=int, default=None, help="how many OLDEST to remove")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    """Resolve the positional grammar before the command runs.

    `agentbus memory rm 7` and `agentbus memory "remember this"` are the same
    two positionals, so which one is an ACTION and which is TEXT can only be
    decided here. Getting it wrong in the other direction is the dangerous one:
    treating "remember this" as an unknown action would be a lost note, and
    treating `rm` as text would store the word "rm" in the notebook.
    """
    actions = {"fetch", "rm", "truncate", "reseal", "add"}
    if args.action is not None and args.action not in actions:
        # It is not an action, so the whole thing is the note. Put it back.
        if args.text is not None:
            print(
                f"unknown memory action '{args.action}'. To remember a phrase with spaces, "
                f'quote it:  agentbus memory "{args.action} {args.text}"',
                file=sys.stderr,
            )
            return 2
        args.text, args.action = args.action, None
    if args.action == "rm" and args.seq is None:
        # `rm 7` puts the 7 in `text`.
        if args.text is None:
            print("agentbus memory rm <seq>: which entry?", file=sys.stderr)
            return 2
        try:
            args.seq = int(args.text)
        except ValueError:
            print(
                f"'{args.text}' is not a seq. Entries are addressed by seq, "
                f"which `agentbus memory fetch` shows in brackets.",
                file=sys.stderr,
            )
            return 2
    if args.action == "truncate" and args.first is None:
        if args.text is not None:
            with contextlib.suppress(ValueError):
                args.first = int(args.text)
        if args.first is None:
            print("agentbus memory truncate --first <N>: how many of the oldest?", file=sys.stderr)
            return 2
    return cmd_memory(args)
