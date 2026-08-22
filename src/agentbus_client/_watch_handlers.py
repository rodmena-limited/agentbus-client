"""`agentbus watch` — the session-side subscriber.

No server-side feature can wake a session that is not running. The bus can push
(SSE delivers in real time), but something on the agent's machine has to be
listening and do something about it. This is that something.

It holds an SSE connection, answers liveness challenges so the agent reads as
`responsive` rather than merely `reachable`, and on each new message runs
whatever you asked for: a command, a file write, or a line on stdout.

Designed to be boring and survivable — it reconnects with backoff, resumes from
the cursor it last saw so nothing is missed across a drop, and never exits on a
transient error, because a watcher that dies quietly is worse than none at all.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

RECONNECT_BACKOFF = (1, 2, 5, 10, 30, 60)

# SEV-1 follow-up (macbook-admin-bd8e86 fix #6, backend endorsed):
# The old backoff had two related flaws — (a) `_failures` reset to 0 on every
# successful stream open AND on every process restart, so an OS-supervised
# watcher whose parent died during an outage came back and started reconnecting
# at 1s, hammering the server; (b) no jitter, so N watchers on N boxes coming
# back after a shared bus restart all reconnected in lockstep and started
# tripping the bulkhead's 30 QPS shed. Both fixes here.
#
# `_FAILURES_TTL_SECONDS` — if the persisted `last_failure_at` is older than
# this, `_failures` resets to 0 on load. Otherwise a watcher up for hours
# after one early transient would start its NEXT reconnect at some high step
# for no reason.
_FAILURES_TTL_SECONDS = 900

# Jitter — plus/minus this fraction of the current backoff step. Backend
# recommended >= +/-10%; using 15% for a bit of headroom since the cost is
# only latency variance.
_BACKOFF_JITTER_FRACTION = 0.15

# How long the reconnect handler will wait for an in-flight background drain
# before giving up on the opportunistic drain and backing off anyway.
#
# The recovery path must never wait forever on the failing path. A background
# drain can legitimately run for minutes (bus.inbox under SDK retries, then up
# to 100 messages x on_message, where notify_command allows
# AGENTBUS_EXEC_TIMEOUT seconds each). An unbounded acquire here stalled the
# reconnect for that whole time with no backoff sleep and no log line. The
# drain is opportunistic — skipping it costs a little latency; blocking on it
# costs the reconnect.
_DRAIN_LOCK_TIMEOUT_SECONDS = 10.0

# The exit code for a watcher whose session socket is gone. DISTINCT from every
# existing code so a supervisor, a monitor, or `watch-status` can tell "the wake
# target died" from "the stream dropped" or "the key was revoked".
EXIT_DEAD_WAKE_SOCKET = 7


# ------------------------------------------------------------------ handlers


def notify_command(template: str) -> Callable[[dict[str, Any]], None]:
    """Run a shell command per message.

    Placeholders {subject} {sender} {delivery_id} {message_id} {thread_id}
    {agent_seq} {direction} are substituted and shell-quoted, so a hostile subject cannot
    inject a command.

    A coalesced envelope (issuedb #9) exposes two extra placeholders,
    `{envelope_count}` and `{envelope_kind}`, so a template can render
    "N new messages" without knowing the internal shape. For a single
    message envelope_count=1 and envelope_kind="" — backwards compatible.

    An UNKNOWN placeholder raises KeyError per message rather than passing the
    literal through, so a template typo would break every delivery. `agent_seq`
    was missing while print_line used it, which meant a caller mirroring the
    default output format could not.
    """

    def handler(message: dict[str, Any]) -> None:
        command = template.format(
            subject=shlex.quote(str(message.get("subject") or "")),
            sender=shlex.quote(
                str(message.get("sender_display") or message.get("sender_address") or "")
            ),
            delivery_id=shlex.quote(str(message.get("delivery_id") or "")),
            message_id=shlex.quote(str(message.get("message_id") or "")),
            thread_id=shlex.quote(str(message.get("thread_id") or "")),
            agent_seq=shlex.quote(str(message.get("agent_seq") or "")),
            # WHERE THE MESSAGE CAME FROM. Already on every delivery row and
            # never surfaced, which is why the injected envelope had to guess —
            # and guessed the most alarming option every time.
            direction=shlex.quote(str(message.get("direction") or "")),
            # HOW an ingress message arrived: "" for SMTP, "hook:<label>" for a
            # signed inbound HTTPS POST. `direction` alone cannot separate them
            # and the envelope was calling both of them email.
            inbound_source=shlex.quote(str(message.get("inbound_source") or "")),
            # Coalesced-envelope placeholders (issuedb #9). For a single
            # message, count=1 and kind="" — the template author can gate
            # on kind to render burst summaries differently from singletons.
            envelope_count=shlex.quote(str(message.get("count") or 1)),
            envelope_kind=shlex.quote(str(message.get("kind") or "")),
            # Persona lanes (SPECS/0021, SEV-2 fix). TWO distinct fields:
            #   {lane}    = the SENDER's persona, enriched by backend #267
            #               ("who sent this")
            #   {my_lane} = the ACTING AGENT's own persona, injected by
            #               cmd_watch ("who am I / what is my lane")
            # They are different things and must not be conflated. 0.9.34
            # stamped my_lane onto lane, clobbering the sender's — that is
            # the SEV-2 this separation fixes. Empty string when unset.
            lane=shlex.quote(str(message.get("lane") or "")),
            my_lane=shlex.quote(str(message.get("my_lane") or "")),
        )
        # Justified in place: `command` is the OPERATOR'S OWN shell template, passed
        # to `agentbus watch --exec`. shell=True is the feature. Every value
        # interpolated into it above is shlex.quote()d, so a sender cannot
        # break out through a subject or display name. S602 stays ARMED
        # everywhere else so a NEW shell=True is caught.
        #
        # SEV-3 (#234): default 5s (was 60s). A stuck --exec (broken notify-send,
        # unresponsive D-Bus, screen locked) used to block the wake path for a
        # full minute PER message — 30 stacked messages = 30 minutes of no
        # arrival being announced. 5s is a snappy budget for "was this notified";
        # override with AGENTBUS_EXEC_TIMEOUT for genuinely slow commands.
        _timeout = float(os.environ.get("AGENTBUS_EXEC_TIMEOUT", "5"))
        try:
            subprocess.run(command, shell=True, check=False, timeout=_timeout)
        except subprocess.TimeoutExpired:
            print(
                f"agentbus watch: --exec timed out after {_timeout}s (command was: {command[:80]}...);"
                " arrival was still recorded to the wake file. Raise AGENTBUS_EXEC_TIMEOUT if this is genuine.",
                file=sys.stderr,
            )

    return handler


def append_file(path: Path) -> Callable[[dict[str, Any]], None]:
    """Append one JSON object per line — a wake-file another process can tail."""

    def handler(message: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, default=str) + "\n")

    return handler


def print_line(message: dict[str, Any]) -> None:
    sender = message.get("sender_display") or message.get("sender_address", "?")
    print(f"[{message.get('agent_seq')}] {sender}: {message.get('subject', '')}", flush=True)
