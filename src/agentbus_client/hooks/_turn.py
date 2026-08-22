#!/usr/bin/env python3
"""Claude Code hooks for AgentBus.

Two jobs, deliberately separate:

  session-start   surface anything already waiting when a session opens, so an
                  agent never begins work unaware that a peer is blocked on it
  notify          called by `agentbus watch --exec`, writes a wake file the
                  session picks up on its next turn

Why both: a hook only fires on session lifecycle events, so on its own it cannot
notice a message that arrives mid-session. `agentbus watch` runs outside the
turn and can. Neither is sufficient alone, which is the whole reason idle agents
were missing messages.

Install BOTH hooks (project or user settings.json) — session-start without
pending means mid-session arrivals surface only on the next restart:

    {
      "hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
          "command": "agentbus-hook session-start"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command",
          "command": "agentbus-hook pending"}]}]
      }
    }

Both need `AGENTBUS_API_KEY` and `AGENTBUS_AGENT` in the environment. Put them
in per-project env (a `.envrc`, or the project's own settings), NEVER inline in
the hook command: an inlined key outlives every rotation, and an inlined —
or guessed — agent name makes the hook act as someone who does not exist.

AGENTBUS_AGENT IS THE KILL SWITCH. These hooks are installed globally and run
in every project on the machine. A project that declares no identity — no
`AGENTBUS_AGENT`, no `.agentbus/agent` — gets NOTHING: no output, no network
call, no files touched, exit 0. Not a warning, not a suggestion to run setup.
Silence is the correct behaviour for a project that never asked for a bus.

A watcher is NOT part of this setup. Its one remaining job is real-time
`--exec` side effects (e.g. notify-send to a human):

    agentbus watch --agent <name> \\
      --exec 'agentbus-hook notify --subject {subject} --sender {sender} --delivery {delivery_id}'

Every failure path here is silent-and-zero. A hook that breaks a session because
the bus is unreachable is worse than one that says nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from . import _session
from ._identity import _resolve_agent
from ._state import (
    EXIT_NOT_WIRED,
    _hook_warn,
    _is_harness_notification,
    _wake_file,
    _warn_if_shadow_queue,
    clear_notify_failure,
    record_notify_failure,
)


def notify(args: argparse.Namespace) -> int:
    """Record an arrival for the session to pick up on its next turn."""
    try:
        agent = _resolve_agent()
        if agent is None:
            # STAYS 0, unlike the others. `notify` runs inside the watcher's
            # --exec; a non-zero rc there can take down the wake path this whole
            # module exists to provide, which is a far worse outcome than a
            # misconfiguration nobody notices. The stderr warning still fires.
            return 0
        wake = _wake_file(agent)
        wake.parent.mkdir(parents=True, exist_ok=True)
        with wake.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "subject": args.subject,
                        "sender": args.sender,
                        "delivery_id": args.delivery,
                    }
                )
                + "\n"
            )
        # Only after the wake is actually on disk. Clearing earlier would let a
        # path that did no work erase evidence of one that failed.
        clear_notify_failure(agent)
    except Exception as exc:
        # A failed capture is a LOST wake, not a no-op: the arrival that
        # triggered this hook is now recorded nowhere.
        _hook_warn("record an arrival (this wake is lost)", exc)
        with contextlib.suppress(Exception):
            resolved = os.environ.get("AGENTBUS_AGENT")
            if resolved:
                record_notify_failure(resolved, f"{type(exc).__name__}: {exc}")
        return 0
    return 0


def pending(_: argparse.Namespace) -> int:
    """Surface mail that arrived since the last turn. For a UserPromptSubmit hook.

    ASKS THE SERVER, and treats the local wake file as a bonus rather than the
    source of truth. It used to read ONLY that file, which made a background
    watcher mandatory for mid-session awareness — and that single dependency
    caused every expensive failure we have had:

      * the watcher is a process, so it dies (twice in one afternoon for one
        peer, once with exit 144 from the harness reaping it), and a dead
        watcher's empty file is indistinguishable from an empty inbox;
      * it needed supervision, so it needed systemd or launchd, which is a lot
        of apparatus for a hook that could simply ask;
      * it wrote to `wake-unknown.jsonl` when misconfigured, so writes and reads
        silently used different queues.

    `session_start` already hit the API directly and never needed a watcher.
    This makes `pending` consistent with it. A Claude Code session now needs NO
    background process at all: two hooks, both server-backed.

    The watcher keeps ONE genuine job — real-time `--exec` side effects, most
    usefully `notify-send` to wake a HUMAN, which no turn-boundary poll can do.
    If you are not using that, you do not need a watcher, and you certainly do
    not need a service manager for one.

    NEVER RUN ON HARNESS-INJECTED NOTIFICATIONS (#91, 2026-08-11). Claude Code
    delivers a plugin monitor's stdout lines and lifecycle events ("stream
    ended") as task-notifications, and those notifications are fed back through
    UserPromptSubmit as though they were prompts. This hook then ran on its
    own monitor's noise and the harness blocked the "prompt" — the operator
    saw "operation blocked by hook" for a message they never typed. A
    UserPromptSubmit hook must distinguish a real human prompt from a
    harness-injected <task-notification> and be a no-op for the latter: no
    bus call, no stdout, exit 0. The monitor is then free to exit 0 cleanly
    when it has no credential, because nothing downstream will block on the
    "stream ended" announcement.
    """
    # The prompt payload arrives on stdin. Read it once; if it is a
    # harness-injected notification (not a human prompt), allow it through
    # without touching the bus.
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if _is_harness_notification(raw):
        # #35: exit silently. Claude Code APPENDS this hook's stdout to the
        # prompt context, so echoing the payload injects session_id,
        # transcript_path, cwd and the user's own prompt back into the model's
        # context — every turn, in every session with the hook installed.
        return 0

    agent = _resolve_agent()
    if agent is None:
        return EXIT_NOT_WIRED
    _warn_if_shadow_queue()

    unread: list[str] = []
    count = 0
    server_ok = False
    try:
        from ..client import AgentBus

        bus = AgentBus(agent=agent)
        # `whoami` is the authoritative count; the unread listing is the
        # preview. Paging from cursor 0 and filtering locally is NOT a way to
        # find unread mail: that window goes blind once history outgrows it,
        # which is how this hook's sibling shipped permanently silent.
        count = int(((bus.whoami().get("unread") or {}).get("count")) or 0)
        server_ok = True
        if count:
            for message in bus.inbox(limit=25, unread=True):
                unread.append(
                    f"  {message.sender}: {message.subject}  (agentbus show {message.delivery_id})"
                )
    except Exception as exc:
        # and a reachability failure here is not evidence of an empty inbox.
        # Fall through to the wake file rather than reporting silence — but SAY
        # the server could not be asked, or a locally-empty wake file reads as
        # an authoritative "nothing waiting".
        _hook_warn("reach the bus (falling back to locally captured wakes)", exc)

    total = max(count, len(unread))
    printed_warning = False
    if total:
        print(f"AgentBus: {total} unread message(s) waiting:")
        for line in unread[:10]:
            print(line)
        if not unread:
            print("  read them: agentbus inbox --unread")
        elif total > 10:
            print(f"  ... and {total - 10} more")
            # #205, same reasoning as the session-start notifier above: name
            # which ten, and name the verb that clears them.
            print("  (the oldest ten; the list does not change until they are read)")
            print("  mark read without opening: agentbus ack <delivery-id> [<id> ...]")
        if unread:
            print("  reply to one: agentbus reply <delivery-id> -b '...'")
        printed_warning = True

    wake = _wake_file(agent)
    if server_ok:
        # The server answered, and the server is authoritative — including when
        # its answer is ZERO. The wake file is a stale capture bonus: clear it
        # WITHOUT trusting it. The old code consulted it exactly when the
        # server said zero, printed already-acked mail with no read_at check,
        # then deleted the evidence — two calls, two answers, and a Stop-hook
        # consumer could eat a UserPromptSubmit consumer's queue (ticket #27).
        with contextlib.suppress(OSError):
            wake.unlink(missing_ok=True)
    elif wake.exists():
        # Server unreachable: the capture file is the only signal there is.
        # Say what it is — possibly stale — and PRESERVE it, so the claim can
        # be reconciled against the server on the next successful run instead
        # of being destroyed by this one.
        stale: list[str] = []
        with contextlib.suppress(Exception):
            for line in wake.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    stale.append(
                        f"  {entry.get('sender')}: {entry.get('subject')}"
                        f"  (agentbus show {entry.get('delivery_id')})"
                    )
        if stale:
            print(
                f"AgentBus: server unreachable; {len(stale)} captured arrival(s) "
                "in the local wake file (may already be read):"
            )
            for line in stale[:10]:
                print(line)
            printed_warning = True

    if printed_warning:
        print()
    # #35: the stdin payload is NOT echoed. Whatever this hook prints becomes
    # part of the prompt, so stdout is reserved for the unread-mail notice —
    # the only thing a reader of that turn should see from us.

    return 0


def _session_id_from_stdin() -> str | None:
    """Claude Code hands every hook a JSON payload on stdin carrying session_id.

    Read as a fallback for CLAUDE_CODE_SESSION_ID, and never fatal: a hook with
    no stdin, empty stdin, or a payload shaped differently must degrade to "I do
    not know which session I am", which the caller treats as "reap nothing".
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        raw = sys.stdin.read()
        if not raw.strip():
            return None
        value = json.loads(raw).get("session_id")
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


def session_end(_: argparse.Namespace) -> int:
    """Reap this agent's stream when the session goes away.

    Orphaned SSE subscribers have bitten this platform five times: a watcher, a
    peer's watcher, our own API workers, the plugin monitor's child, and a
    supervised watcher. Each time the fix was a client-side trap in the thing
    that leaked, and each time a different thing leaked next. `SessionEnd` fires
    at the moment the session ends, which is the one place that generalises.

    Why it matters beyond tidiness: a leaked subscriber makes the platform report
    a LIVE stream for a session that is gone, so `wake_channel` becomes a lie in
    the direction that matters — a coordinator waits for an agent that will never
    answer. The server-side backstop for that is #49; this is the fast path.

    Scoped to THIS agent's monitor by its own state file, never by `--agent`: a
    supervised watcher carries the same flag and killing one would take out a
    capture path the operator deliberately runs.
    """
    agent = _resolve_agent()
    if agent is None:
        return EXIT_NOT_WIRED

    # REAP ONLY THIS SESSION'S MONITOR. Every session on one checkout is the
    # same agent, so an agent-scoped reap crosses sessions — and a headless
    # `claude -p`, which spawns no monitor at all, would still kill the
    # interactive session's. Taking without giving.
    #
    # IF THE SESSION CANNOT NAME ITSELF, REAP NOTHING. Leaking one subscriber is
    # recoverable and visible; deafening somebody else's live session is neither.
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or _session_id_from_stdin()
    if not session:
        return 0
    try:
        import signal

        from ..onboarding import _monitor_pids

        for pid in _monitor_pids(agent, session=session):
            with contextlib.suppress(OSError, ValueError, ProcessLookupError):
                os.kill(int(pid), signal.SIGTERM)

        # AND REMOVE THIS SESSION'S CURSOR FILE. Session-scoping the state file
        # fixed the cross-session reap and created a leak in its place: one file
        # per session, forever, in a directory nothing else prunes. SessionEnd is
        # the only place that knows both the agent and the session, so it is the
        # only place that can delete exactly the right one. Scoped by the full
        # session id — never a glob over the agent, which would delete a LIVE
        # session's cursor and is the same cross-session mistake one layer down.
        # REG-8c: sanitize BOTH agent and session before interpolating into a
        # filename. bikeroom flagged this one as "probable not confirmed live";
        # confirmed live on my box — a traversal payload in `agent` yielded
        # /tmp/PWNED before normpath even had to think about it. state.unlink
        # is an arbitrary-path DELETE primitive keyed on the two interpolated
        # values, reachable from a hostile `.agentbus/agent`; session comes
        # from the harness env var, less obviously attacker-controlled but
        # sanitizing both makes the invariant local to this line.
        from .. import sealing

        state = (
            Path(os.environ.get("AGENTBUS_CONFIG_DIR") or (Path.home() / ".config" / "agentbus"))
            / f"monitor-{sealing.agent_slug(agent)}-{sealing.agent_slug(session)}.json"
        )
        with contextlib.suppress(OSError):
            state.unlink(missing_ok=True)

        # AND RELEASE THE IDENTITY CLAIM — the other half of #126.
        #
        # Nothing ever removed it, so every session that ended left a record
        # asserting it held this agent, and the next session to start read that
        # record and announced a collision with a session that no longer existed.
        # The liveness check added in `_warn_if_identity_shared` stops the false
        # alarm; this stops the residue that caused it, so the two together mean
        # a clean handover leaves nothing behind to misread.
        #
        # ONLY IF IT IS OURS. Deleting another session's live claim would hand
        # the identity to a newcomer silently and suppress a TRUE warning — the
        # same cross-session mistake the cursor unlink above is scoped to avoid,
        # and the reason this reads the file before removing it rather than
        # unlinking by name.
        with contextlib.suppress(OSError, ValueError):
            claim = _session._identity_claim_path(agent)
            if claim.exists() and str(json.loads(claim.read_text()).get("session")) == session:
                claim.unlink(missing_ok=True)
    except Exception as exc:
        _hook_warn("reap this session's stream (it may be left subscribed)", exc)
    return 0
