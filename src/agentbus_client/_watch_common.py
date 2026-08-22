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

import os
from pathlib import Path

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


def _dead_wake_socket_reason() -> str | None:
    """Is the session socket this watcher injects into still alive?

    `agentbus watch --exec "agentbus-hook inject ..."` writes to
    CLAUDE_CODE_MESSAGING_SOCKET, which the watcher inherits from the session
    that spawned it. When that session ends — a crash, a SIGKILL, a force-quit —
    the socket file disappears but the watcher keeps streaming, and every
    arrival is injected into a channel nothing reads. Delivered, recorded,
    never woken: the 2026-08-11 outage, in which an operator's email sat in the
    inbox for an hour while a watcher from a dead session swallowed the wake.

    Returns the reason (a sentence to print) when the socket is dead, else None.
    `CLAUDE_CODE_MESSAGING_SOCKET` unset is NOT a defect: a watcher started from
    a plain terminal has no session socket and that is a supported
    configuration. Only a socket that WAS set and no longer exists is a dead
    wake channel.
    """
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not sock:
        return None
    if Path(sock).exists():
        return None
    return (
        f"the session socket this watcher injects into is GONE: {sock} — "
        "the session that spawned this watcher has ended, so every arrival "
        "is being injected into a dead channel. Stopping instead of "
        "swallowing the wake. A fresh session's monitor will re-arm it."
    )


class DeadWakeSocket(RuntimeError):
    """The session socket this watcher injects into no longer exists."""


class WatchTerminated(BaseException):
    """SIGTERM arrived. A BaseException so the reconnect handler's total
    `except BaseException` cannot swallow it; cmd_watch's SIGTERM handler raises
    it so `finally` blocks (coalescer flush, pidfile removal) run (review #23, S3)."""


# THE ONLY WAY THIS CLIENT CAN OBSERVE A LINK THAT DIED WITHOUT A FIN.
#
# A clean outage — restart, deploy, refused connection — sends FIN, something
# raises, and the backoff loop below runs. That path was verified and it loses
# nothing. But when the far end simply STOPS (a box moved, a blackholed route, a
# NAT dropping state — the shape a database migration produces) no FIN ever
# arrives. With no read deadline, iter_lines() blocks forever on a socket that
# still reads ESTABLISHED, the backoff loop is never entered, and mail piles up
# undelivered until someone restarts the process. Observed: two isolated runs,
# nothing delivered in 240s while fresh connections to the same service returned
# 200 throughout.
#
# The server already emits `: keepalive` every 20s on an idle stream
# (api/routes_messages.py), so silence is genuinely diagnostic — we simply had no
# deadline by which to notice it missing.
#
# The deadline must be BOTH:
#   * finite   — or a dead link is never noticed at all;
#   * safely LONGER than the keepalive — or a healthy quiet link reconnect-storms,
#     which is the same bug wearing the opposite sign. Three missed keepalives.
STREAM_KEEPALIVE_SECONDS = 20.0
STREAM_READ_DEADLINE = 60.0

# How long a stream must stay open before the reconnect ladder resets. A reset on
# the bare HTTP 200 let a bus that accepts-then-drops (an overloaded proxy, a
# draining worker) defeat the backoff entirely (review #23, issuedb #27).
STREAM_HEALTHY_SECONDS = 30.0

# The longest this client will go without asking the server directly, no matter
# how healthy the stream looks. Push is an OPTIMISATION here, never the only way
# mail is found — see the reconcile branch in `_stream_once` for the live failure
# that made this necessary. One cheap inbox GET per agent per minute buys the
# guarantee that a wake path cannot die silently while its socket stays warm.
STREAM_RECONCILE_SECONDS = 60.0


def _client_version() -> str:
    """The version THIS process imported, never what is installed on disk."""
    try:
        from . import __version__

        return str(__version__)
    except Exception:
        return "unknown"
