"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
from typing import Any

from . import _common
from ._common import _accept_common_flags_after_subcommand, _print, _print_qr


def cmd_whoami(args: argparse.Namespace) -> int:
    result = _common._bus(args).whoami()
    if args.json:
        _print(result, True)
    else:
        workspace = result["workspace"]["slug"]
        agent = (result.get("agent") or {}).get("name", "(no acting agent)")
        print(f"workspace: {workspace}")
        print(f"agent:     {agent}")
        if result.get("address"):
            print(f"address:   {result['address']}")
            # THE QR ENCODES A mailto:, NOT THE BARE ADDRESS.
            #
            # The point of scanning it is to open a mail app already addressed to
            # this agent. A bare address scans as text, which most phones render
            # as something you then have to copy by hand — the manual step the
            # QR existed to remove.
            if getattr(args, "qr", False):
                print()
                # Only caption a QR that actually rendered. Printing "scan to
                # mail X directly" under an absent QR tells the reader to scan
                # nothing — the caption is the only evidence a QR was meant to
                # be there, so it must follow the render, not the intent.
                if _print_qr(f"mailto:{result['address']}"):
                    print(f"  scan to mail {agent} directly")
        # #149: an agent checking who it is should see what it WEARS — the tags
        # peers will find it by. Same parity rule as unread below.
        tags = _format_tags((result.get("agent") or {}).get("labels"), limit=60)
        if tags:
            print(f"tags:      {tags}")
        # Persona (POLICY): the server-assigned lane. Displayed when the
        # server returns it; absent silently when it does not (old server
        # before the column migration). Forward-compatible.
        persona = (result.get("agent") or {}).get("persona")
        if persona:
            print(f"persona:   {persona}")
        # The API returns `unread` and this printer dropped it — the exact
        # "fixed on one surface, left the other" shape this whole episode was
        # about. An agent running `agentbus whoami` to check its identity should
        # be told it has mail waiting, not have to think of asking separately.
        # #48: an agent that has forgotten it is deaf to a peer will read a
        # silence as "they stopped sending". Surfaced on whoami because that is
        # the startup call, so the blocks are seen before the silence is
        # interpreted.
        #
        # NULL IS "UNKNOWN", NOT ZERO. The server wraps this advisory and may
        # return null if the lookup fails; rendering that as "0 blocks" would
        # state the opposite of what is known. Say nothing rather than assert
        # nothing-is-blocked.
        blocks = result.get("blocks")
        if isinstance(blocks, dict) and blocks.get("count"):
            held = blocks.get("suppressed_total")
            held_txt = f", {held} message(s) suppressed" if held else ""
            print(f"blocks:    {blocks['count']} active{held_txt}")
            print("           who: agentbus blocks")

        unread = result.get("unread") or {}
        if unread.get("count"):
            print(
                f"unread:    {unread['count']} message(s) waiting "
                f"(oldest {unread.get('oldest_at', '?')})"
            )
            print(
                "           read them: agentbus inbox   |   be woken: "
                "agentbus watch --agent " + str(agent)
            )
    return 0


def _format_tags(labels: dict[str, Any] | None, limit: int = 40) -> str:
    """Compact roster rendering: bare keys as-is, k=v for valued tags, elided to
    what a roster line affords — values can be whole sentences (<=256
    server-side), and eliding a LIST for display is fine where truncating an
    operator's sentence in a detail view would not be.

    #165: THE ELISION MUST BE LEGIBLE TO A PROGRAM, NOT JUST TO A CAREFUL HUMAN.
    This used to slice the joined string mid-token and append a bare `…`, which
    produced two failures with real victims:

      * the `…` was the ONLY signal that anything was missing, and a reader
        parsing the display consumed it as just another tag. agentbus-frontend
        published a bucket table computed from this output — `team:hive` sat
        past the cutoff, so their evidence said a team existed on 1 agent when
        it was on 5, and a second run an hour later said 0. A display truncates
        BY DESIGN; anything computed from it is a claim about the LISTING, not
        about the data.
      * a character slice lands mid-token, so `role:alice` rendered as `ro…` —
        which reads as a tag named `ro`, not as a marker. On the roster that is
        indistinguishable from real content.

    So: drop WHOLE tags, never part of one, and say HOW MANY were dropped. A
    count is a fact a program can act on; an ellipsis is a rumour. Callers that
    need the data rather than the picture use `--json`, which never elides.
    """
    if not labels:
        return ""
    keys = sorted(labels)
    parts = [k if labels[k] in ("", None) else f"{k}={labels[k]}" for k in keys]
    joined = ",".join(parts)
    if len(joined) <= limit:
        return joined

    # A KEY ALWAYS BEATS A VALUE WHEN SPACE RUNS OUT.
    #
    # The old version spent the whole budget on whichever tag came first
    # alphabetically, values included, then emitted "[+3 more]" — naming NOTHING.
    # So `duty:bus-core=owns the AgentBus server and deploys` (a perfectly good
    # tag) consumed the line and the agent with the most descriptive tags became
    # the one whose tags could not be seen at all. "[duty:bus-core +2 more]" is
    # strictly more useful than "[+3 more]", and the value was never the part
    # you scan a roster for.
    #
    # So: fit as many k=v as afford it, and when one does not fit, retry it as a
    # bare key before giving up on it.
    kept: list[str] = []
    used = 0
    for index, key in enumerate(keys):
        remaining_after = len(keys) - index - 1
        suffix = f" +{remaining_after} more" if remaining_after else ""
        separator = 1 if kept else 0
        for candidate in (parts[index], key):
            cost = len(candidate) + separator
            if used + cost + len(suffix) <= limit:
                kept.append(candidate)
                used += cost
                break
        else:
            break
    dropped = len(keys) - len(kept)
    if not kept:
        # Not even the shortest KEY fits. Say so honestly rather than emit a
        # fragment — but name the count, so the reader knows to ask --json.
        return f"+{len(keys)} more"
    return ",".join(kept) + (f" +{dropped} more" if dropped else "")


def cmd_phonebook(args: argparse.Namespace) -> int:
    agents = _common._bus(args).phonebook(args.query, capability=args.capability, label=args.label)
    if args.json:
        _print(agents, True)
        return 0
    if not agents:
        print("no agents found")
        return 0
    # TAGS ARE A COLUMN, NOT A SUFFIX (#183). They used to be appended after the
    # address and the capability list — both variable-width — so no two rows
    # lined up and the eye could not scan the one field you filter on. Whoever
    # had the longest address decided where everyone else's tags began.
    #
    # Order is by what a reader scans for: who, are they there, what do they do,
    # then the address, which is the longest field and the one you copy rather
    # than compare.
    # EVERY WIDTH COMES FROM THE DATA. Two hardcoded numbers were doing the
    # damage the column was meant to fix: `presence:<7` while "responsive" is
    # TEN characters, so every responsive row pushed the rest three columns
    # right; and a tag cap that trimmed the budget without trimming the cell, so
    # a 42-character cell still shoved the address out of line. Both only showed
    # up rendering the real roster — the unit test fed the formatter directly
    # and never laid two rows beside each other.
    TAG_CAP = 40
    width = max(len(a["name"]) for a in agents)
    presence_width = max(len(a["presence"]) for a in agents)
    # Persona column: only shown when at least one agent HAS one. Forward-
    # compatible — old servers don't return the field, so the column does
    # not appear and the layout is byte-identical to pre-persona output.
    has_persona = any(a.get("persona") for a in agents)
    persona_width = (
        max((len(a.get("persona") or "") for a in agents), default=0) if has_persona else 0
    )
    rendered = [(a, _format_tags(a.get("labels"), limit=TAG_CAP)) for a in agents]
    tag_width = min(max((len(t) for _a, t in rendered), default=0), TAG_CAP)
    elided = 0
    for agent, tags in rendered:
        caps = ",".join(agent.get("capabilities") or [])
        cell = f"[{tags}]" if tags else ""
        line = (
            f"{agent['name']:<{width}}  {agent['presence']:<{presence_width}}  "
            f"{cell:<{tag_width + 2}}  {agent['address']}"
        )
        if has_persona:
            line += f"  {(agent.get('persona') or '-'):<{persona_width}}"
        if caps:
            line += f"  {caps}"
        if "more" in tags:
            elided += 1
        print(line.rstrip())
    if elided:
        # #165: name the loss ONCE, at the bottom, and point at the surface that
        # does not lose anything. A reader who is computing from this output is
        # the person this line exists for.
        print(
            f"\n({elided} row(s) have more tags than fit. This display elides; "
            "`agentbus phonebook --json` does not.)"
            # That command used to FAIL — --json was global-only, so the remedy
            # printed beside the problem landed on a usage error. It works now;
            # see _accept_common_flags_after_subcommand.
        )
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    """`agentbus tag` — this agent's discovery tags (#149).

    NOT the delivery mail labels (`agentbus labels`): tags live on the AGENT
    and answer "who is on team frontend"; labels file one recipient's mail.
    """
    bus = _common._bus(args)
    set_labels: dict[str, str] = {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        set_labels[key] = value
    if not set_labels and not args.remove:
        # No mutation asked: list current tags from whoami's agent record.
        result = bus.whoami(agent=args.agent)
        labels = (result.get("agent") or {}).get("labels") or {}
        if args.json:
            _print(labels, True)
            return 0
        if not labels:
            print("no tags. Add one: agentbus tag team:frontend 'skill:playwright=takes shots'")
            return 0
        for key, value in sorted(labels.items()):
            print(f"{key}\t{value}" if value else key)
        return 0
    result = bus.tag(set_labels, args.remove, agent=args.agent)
    if args.json:
        _print(result, True)
        return 0
    labels = result.get("labels") or {}
    for key, value in sorted(labels.items()):
        print(f"{key}\t{value}" if value else key)
    print(f"({result.get('count')}/{result.get('limit')} tags)")
    return 0


def cmd_busy(args: argparse.Namespace) -> int:
    """Declare (or clear) a busy window. Prints what senders will now see."""
    result = _common._bus(args).busy(args.seconds, reason=args.reason, agent=args.agent)
    if args.json:
        _print(result, True)
        return 0
    if not result.get("busy"):
        print("busy cleared — senders will see you as available")
        return 0
    print(f"busy until {result['busy_until']} ({result.get('seconds')}s)")
    if result.get("busy_reason"):
        print(f"  reason: {result['busy_reason']}")
    print("  senders see this in their send response; it EXPIRES on its own.")
    print("  Messages still arrive — busy is advisory, not a block.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """`agentbus status` — read or declare this agent's availability (#187)."""
    bus = _common._bus(args)
    if args.state is None:
        current = bus.status()
        if args.json:
            _print(current, True)
            return 0
        state = current.get("availability", "online")
        held = current.get("held") or 0
        if state == "online":
            print("online — nothing withheld")
        else:
            until = str(current.get("until") or "")[:19].replace("T", " ")
            reason = current.get("reason")
            holds = current.get("holds_from")
            print(f"{state} until {until}" + (f" — {reason}" if reason else ""))
            if holds:
                print(f"  withholding '{holds}' priority and below")
            print(f"  {held} message(s) held, delivered when this clears")
        return 0

    result = bus.status(
        args.state, seconds=args.seconds, reason=args.reason, hold_below=args.hold_below
    )
    if args.json:
        _print(result, True)
        return 0
    if args.state == "online":
        released = result.get("released") or 0
        print("online" + (f" — released {released} withheld message(s)" if released else ""))
        return 0
    until = str(result.get("until") or "")[:19].replace("T", " ")
    print(f"{result.get('availability')} until {until}")
    holds = result.get("holds_from")
    if holds:
        print(f"  '{holds}' priority and below is HELD — senders are told at send time")
    else:
        # BUSY AND AWAY WITHHOLD NOTHING, and saying so prevents the exact
        # misunderstanding this feature exists to fix: #168's `busy` was
        # advisory and everyone assumed otherwise.
        print("  mail still arrives — this tells senders, it does not hold anything")
        print("  to actually be left alone: agentbus status dnd --for 3600")
    return 0


def cmd_liveness(args: argparse.Namespace) -> int:
    """Show who is genuinely responding, not merely reachable."""
    bus = _common._bus(args)
    agents = bus.phonebook()
    if args.json:
        _print(agents, True)
        return 0
    width = max((len(a["name"]) for a in agents), default=8)
    # #208: THE COLUMN IS "ECHO", NOT "RTT". This never measured a round trip —
    # it is issue-to-echo, dominated by the interval the agent itself chose to
    # poll on. Under the old heading two equally healthy agents on 1s and 60s
    # loops read as 1000 and 60000, and a reader would reasonably call the
    # second one unwell. Reads `echo_delay_ms`, falling back to the deprecated
    # `rtt_ms` so an older server still renders.
    print(f"{'AGENT':<{width}}  {'STATE':<11} {'SEEN':>8} {'PONG':>8} {'ECHO':>8}")
    for a in agents:
        seen = f"{a.get('last_seen_seconds')}s" if a.get("last_seen_seconds") is not None else "-"
        pong = f"{a.get('last_pong_seconds')}s" if a.get("last_pong_seconds") is not None else "-"
        delay = a.get("echo_delay_ms", a.get("rtt_ms"))
        echo = f"{delay}ms" if delay is not None else "-"
        print(f"{a['name']:<{width}}  {a['presence']:<11} {seen:>8} {pong:>8} {echo:>8}")
    print("\nresponsive = echoed a liveness challenge (its loop is turning)")
    print("ECHO       = time from issuing a challenge to its echo. It INCLUDES the")
    print("             agent's own poll wait, so it is not a network round trip —")
    print("             read it as how stale a `responsive` verdict can be.")
    print("reachable  = a key acted as it; with a shared key that may be someone else")
    print("idle       = neither")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("whoami", help="show the acting identity")
    # `-qr` as well as `--qr`: a single-dash multi-character option is unusual,
    # but it is what an operator will actually type, and argparse accepts it when
    # declared explicitly rather than assembled from single-letter flags.
    p.add_argument(
        "-qr", "--qr", action="store_true", help="also print a scannable QR of this agent's address"
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("phonebook", help="discover agents")
    p.add_argument("query", nargs="?", default=None)
    p.add_argument("--capability", default=None)
    p.add_argument(
        "--label",
        action="append",
        default=None,
        help="filter by tag: `team:frontend` (key exists) or `env=prod` (exact); repeat to AND",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_phonebook)

    p = sub.add_parser(
        "tag",
        help="this agent's discovery tags (teams/skills/projects — delivery mail labels are `labels`)",
    )
    p.add_argument(
        "set",
        nargs="*",
        metavar="KEY[=VALUE]",
        help=(
            "tags to set — TWO GRAMMARS, both legal, they mean different things: "
            "`skill:playwright` = wear the NAMESPACED KEY 'skill:playwright' (no value); "
            "`skill=playwright` = wear the KEY 'skill' with the VALUE 'playwright'; "
            "`skill:playwright=takes shots` = namespaced key WITH a value. "
            "Split rule: everything before the FIRST `=` is the key (colons are part of it), "
            "everything after is the value. Matching filters follow the same rule "
            "(see `agentbus phonebook --label`)."
        ),
    )
    p.add_argument("--remove", action="append", default=[], metavar="KEY")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_tag)

    p = sub.add_parser(
        "busy",
        help="tell senders you cannot take new work for N seconds (0 clears it)",
    )
    p.add_argument("seconds", type=int, help="how long; 0 clears. Expires on its own.")
    p.add_argument("--reason", default=None, help="shown to senders, e.g. 'deep in a repro'")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_busy)

    p = sub.add_parser(
        "status",
        help="read or declare availability: online|busy|away|dnd|offline (#187)",
    )
    p.add_argument(
        "state",
        nargs="?",
        default=None,
        choices=("online", "busy", "away", "dnd", "offline"),
        help="omit to READ. dnd and offline WITHHOLD mail; busy and away only tell senders",
    )
    p.add_argument(
        "--for",
        dest="seconds",
        type=int,
        default=None,
        metavar="SECONDS",
        help="how long (capped server-side; every state but online expires)",
    )
    p.add_argument("--reason", default=None)
    p.add_argument(
        "--hold-below",
        dest="hold_below",
        default=None,
        choices=("urgent", "normal", "background"),
        help="override what is withheld (default: dnd holds below urgent)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("liveness", help="who is responsive, not merely reachable")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_liveness)
