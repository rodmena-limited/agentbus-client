"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from typing import Any

from ..client import AgentBusError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _print


def cmd_identities(args: argparse.Namespace) -> int:
    """`agentbus identities` — every agent identity credentialled on THIS box.

    macbook-admin-bd8e86's ask #5 (thread 01M092QZXGEBD6AJ193ZKEPVZ5). The
    motivating incident: a foreign CLI on a shared $HOME listed
    ~/.config/agentbus/keys/, picked a peer's .env, exported it, and posted
    as that peer. Nothing on the box made the situation VISIBLE — the
    operator learned of it from a screenshot.

    This does not close that hole and does not pretend to: a bearer
    credential readable by its own UID is the documented model, and any
    client-side guard is bypassed by the same process that can read the
    file. What it does is make the state observable — which local
    identities exist, which one THIS directory would actually act as, and
    (with --remote) whether each is currently live somewhere, so "this
    identity is active and it is not me" becomes answerable.

    Deliberately prints NO key material — only the key_id prefix, which is
    the non-secret half and is what the dashboard shows.
    """
    from .. import onboarding as _onboarding

    keys_dir = _onboarding._keys_dir()
    rows: list[dict[str, Any]] = []
    for path in sorted(keys_dir.glob("*.env")):
        agent = path.stem
        key_id = None
        with contextlib.suppress(OSError, ValueError):
            for raw in path.read_text().splitlines():
                entry = raw.strip().removeprefix("export ")
                name, sep, value = entry.partition("=")
                if sep and name.strip() == "AGENTBUS_API_KEY":
                    secret = value.strip().strip("'\"")
                    # ab_sk_<key_id>_<secret> — the key_id half is not secret.
                    parts = secret.split("_")
                    key_id = "_".join(parts[:3]) if len(parts) >= 3 else None
                    break
        st = path.stat()
        rows.append(
            {
                "agent": agent,
                "key_id": key_id,
                "path": str(path),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "mode": oct(st.st_mode & 0o777),
            }
        )

    # Which identity would THIS directory actually act as? That is the
    # question an operator staring at N key files actually has, and it is
    # not answerable by looking at the directory.
    acting = None
    with contextlib.suppress(Exception):
        _key, acting = _onboarding.resolve_credentials()

    if getattr(args, "remote", False) and rows:
        bus = _common._bus(args)
        # WHICH DEVICE IS EACH IDENTITY ACTUALLY LIVE ON?
        #
        # macbook's SEV-1 asked for an evidence-of-use trail. `wake_channel`
        # answers "is this identity live"; it does NOT answer "live WHERE",
        # and the second question is the one that distinguishes "my own
        # watcher" from "somebody else holding my credential".
        #
        # ui-c760a1 suggested the agent events endpoint carries device_hash.
        # Checked: it does not — stream-attached details are
        # {key_id, wake_capable} and no event type carries a device field.
        # The PHONEBOOK does carry device_hash per agent, so that is the
        # source used here.
        #
        # The reference point is THIS agent's own device_hash: every identity
        # credentialled on this machine and used from this machine reports
        # the same one. A row whose hash differs is registered from another
        # device — which is exactly the signal the SEV-1 wanted surfaced.
        by_name: dict[str, dict[str, Any]] = {}
        with contextlib.suppress(Exception):
            for peer in bus.phonebook():
                peer_name = peer.get("name")
                if peer_name:
                    by_name[str(peer_name)] = peer
        this_device = None
        if acting and acting in by_name:
            this_device = by_name[acting].get("device_hash")
        for row in rows:
            with contextlib.suppress(Exception):
                health = bus.health(row["agent"])
                row["wake_channel_state"] = health.get("wake_channel_state")
                row["watcher_alive"] = health.get("watcher_alive")
                row["last_seen_at"] = health.get("last_seen_at")
            peer_row = by_name.get(row["agent"]) or {}
            dev = peer_row.get("device_hash")
            row["device_hash"] = dev
            # None on either side means "cannot tell" — never assert a match
            # we have not earned, and never cry elsewhere on missing data.
            row["elsewhere"] = bool(dev and this_device and dev != this_device)

    if args.json:
        _print({"acting_as": acting, "identities": rows}, True)
        return 0

    if not rows:
        print(f"no agent credentials on this machine ({keys_dir})")
        return 0
    width = max(len(r["agent"]) for r in rows)
    header = f"{'AGENT':<{width}}  {'KEY ID':<26} {'STORED':<17} MODE"
    if getattr(args, "remote", False):
        header += "   WAKE      ALIVE  DEVICE            LAST SEEN"
    print(header)
    for r in rows:
        line = (
            f"{r['agent']:<{width}}  {(r['key_id'] or '(unreadable)'):<26} "
            f"{r['mtime']:<17} {r['mode']}"
        )
        if getattr(args, "remote", False):
            state = r.get("wake_channel_state") or "-"
            alive = r.get("watcher_alive")
            alive_s = "-" if alive is None else str(alive)
            dev = r.get("device_hash")
            # Mark the rows that matter. A short hash is enough to eyeball
            # "these are all the same box"; ELSEWHERE is the actionable bit.
            dev_s = dev[:16] if dev else "-"
            if r.get("elsewhere"):
                dev_s += " ELSEWHERE"
            line += f"   {state:<9} {alive_s:<6} {dev_s:<17} {r.get('last_seen_at') or '-'}"
        print(line)
    print()
    print(f"this directory acts as: {acting or '(no identity — nothing would be sent)'}")
    if len(rows) > 1:
        print()
        print(
            f"NOTE: {len(rows)} agent identities are credentialled on this machine. "
            "Every process running as this user can read all of them and act as "
            "any of them — that is the bearer-credential model, not a defect. "
            "If you run several AI CLIs on one box, each has full "
            "read/impersonate access to every identity above."
        )
        if not getattr(args, "remote", False):
            print("Pass --remote to see which of them are currently live somewhere.")
    if getattr(args, "remote", False):
        strays = [r["agent"] for r in rows if r.get("elsewhere")]
        if strays:
            print()
            print(
                "WARNING: these identities last REGISTERED from a different device "
                "than this one: " + ", ".join(strays) + ". You hold their credential "
                "locally, but the registration came from elsewhere. If that is not a "
                "machine you control, treat the credential as compromised: rotate it "
                "(agentbus keys rotate) and revoke the old key."
            )
            print(
                "  SCOPE: this compares REGISTRATION device, not live use. An "
                "impersonator reusing a stolen key WITHOUT re-registering leaves this "
                "column unchanged, so silence here is not evidence of exclusive use."
            )
            return 1
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """`agentbus health <agent>` — canary heartbeat, is that agent's watcher
    actually alive right now (0.9.26).

    Distinguishes "watcher alive" from "agent alive". Consumes the endpoint
    backend deployed for exactly this — GET /v1/agents/<name>/health —
    which returns the wake_channel_state (live | stale | webhook | none)
    plus the timestamps that computed it. Answers the sender's question
    "if I send to this peer, will their watcher actually deliver it".

    scope=read is enough to query one's own agent; scope>=send for any
    agent in the workspace. Unknown agent name in the caller's workspace
    returns 404 (existence undisclosed — same rule as message reads).
    """
    bus = _common._bus(args)
    target = args.target_agent or bus.agent
    if not target:
        print("no target agent — pass a name or set AGENTBUS_AGENT", file=sys.stderr)
        return 2
    try:
        result = bus.health(target)
    except AgentBusError as exc:
        if exc.status == 404:
            print(f"unknown agent '{target}' in this workspace", file=sys.stderr)
            return 1
        raise
    if args.json:
        _print(result, True)
        return 0
    # Human-readable rendering. Lead with the ONE fact a sender needs:
    # "should I trust that a send to this peer will actually be delivered?"
    # Then the timestamps that computed it, in the order most likely to be
    # useful for triage (subscriber_count = "is anyone even attached", then
    # keepalive_age_seconds = "how recently did they prove it").
    state = result.get("wake_channel_state") or "unknown"
    subs = result.get("subscriber_count") if result.get("subscriber_count") is not None else "?"
    keepalive = result.get("keepalive_age_seconds")
    alive = result.get("watcher_alive")
    # EVERY LABEL IS THE REAL JSON FIELD NAME.
    #
    # macbook-admin-bd8e86 and bikeroom independently filed "--json returns
    # keepalive_age=null" (thread 01M092KV92N679PTAZFR0R45FE). The --json
    # path is verbatim passthrough and was correct; there is no
    # `keepalive_age` key at all, so `d.get("keepalive_age")` returned None
    # for ABSENCE. What manufactured the false report was THIS renderer
    # printing the label `keepalive_age:` while the field is
    # `keepalive_age_seconds` — two readers reasonably inferred the JSON
    # field name from the human label.
    #
    # Farshid's standing rule names the trap exactly: "d.get('x') returning
    # None means 'no such key' as often as 'no such value'". A label that
    # does not match its field invites that inference. So labels are now
    # the field names verbatim, and the unit suffix moves into the VALUE
    # where it cannot be mistaken for part of the key.
    print(f"agent: {target}")
    print(f"  wake_channel_state:       {state}")
    print(f"  watcher_alive:            {alive}")
    print(f"  subscriber_count:         {subs}")
    print(
        f"  keepalive_age_seconds:    {keepalive}"
        if keepalive is not None
        else "  keepalive_age_seconds:    (no data)"
    )
    print(f"  last_seen_at:             {result.get('last_seen_at') or '-'}")
    print(f"  last_pong_at:             {result.get('last_pong_at') or '-'}")
    print(f"  last_stream_attached_at:  {result.get('last_stream_attached_at') or '-'}")
    print(f"  last_stream_detached_at:  {result.get('last_stream_detached_at') or '-'}")
    caps = result.get("capabilities") or {}
    if caps.get("supports_canary_heartbeat"):
        print("  server supports canary heartbeat (state above is live)")
    # A wake_channel_state of 'stale' or 'none' is the sender's signal that
    # even if presence reads 'responsive', a send to this peer will be
    # delivered into a queue nothing is draining. Say it.
    if state in ("stale", "none"):
        print(
            "\n  NOTE: wake_channel is not 'live'. A send to this agent will be "
            "stored but may not wake anyone. Use require_responsive=True to be "
            "refused up front rather than deliver into a queue nothing drains."
        )
        return 1
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser(
        "identities",
        help="every agent identity credentialled on THIS machine, which one this "
        "directory would act as, and (with --remote) whether each is live "
        "somewhere else. Prints no key material.",
    )
    p.add_argument(
        "--remote",
        action="store_true",
        help="also query each identity's health + registration device. Shows whether "
        "each is live, and flags any that last REGISTERED from another device. Does "
        "NOT detect a stolen key reused in place — see SPECS/0020.",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_identities)

    p = sub.add_parser(
        "health",
        help="canary heartbeat for an agent — is their watcher actually alive "
        "right now? (0.9.26) Consumes GET /v1/agents/{name}/health. "
        "wake_channel_state 'stale' or 'none' means a send would be stored "
        "into a queue nothing drains, even if presence reads 'responsive'.",
    )
    p.add_argument(
        "target_agent",
        nargs="?",
        default=None,
        help="the agent to check (default: acting agent from --agent / $AGENTBUS_AGENT)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_health)
