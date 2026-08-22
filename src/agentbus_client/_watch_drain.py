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

import contextlib
import json
import os
import random
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

# ruff: noqa: F401
# Split into sibling modules (review #23, file-size cap); every name is re-exported
# here so `watch.<name>` keeps resolving. Tests patch helpers on the defining module.
from . import _watch_common, _watch_handlers
from ._watch_common import (
    STREAM_HEALTHY_SECONDS,
    STREAM_KEEPALIVE_SECONDS,
    STREAM_READ_DEADLINE,
    STREAM_RECONCILE_SECONDS,
    DeadWakeSocket,
    WatchTerminated,
    _client_version,
    _dead_wake_socket_reason,
)
from ._watch_handlers import (
    append_file,
    notify_command,
    print_line,
)
from .client import AgentBus, AgentBusError, AuthError

if TYPE_CHECKING:  # #48
    from ._watch_base import WatcherBase as _WatcherBase
else:  # runtime: no new base, no MRO change
    _WatcherBase = object


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


class WatcherDrainMixin(_WatcherBase):
    """Methods of Watcher carved out for the file-size cap (review #23).

    Mixed back into Watcher; relies on the attributes its __init__ sets."""

    # ------------------------------------------------------------- draining

    def _drain_async(self) -> None:
        """Kick off a drain on a background thread if one is not already running.

        SEV-2-I (#234): the SSE iter_lines() loop MUST keep consuming keepalives
        while a drain runs. When the drain ran inline, a deep backlog (100 msgs +
        slow bus) could hold up the loop past the 60s read deadline, causing a
        spurious reconnect and often a reconnect storm on backlogged inboxes.
        Running the drain off-thread is the fix.

        REG-3 (round-3 audit): the lock is now held for the DURATION of the
        drain, not just its LAUNCH. The previous version protected only the
        is_alive check; a main-thread caller (_backoff_and_drain, run() startup)
        could call _drain() concurrently with the background thread, so the
        cursor advanced twice and on_message ran twice per delivery — the
        duplicate-wake incident (#88) would have reappeared exactly here. Now
        _drain_async acquires non-blocking (if a drain is in flight, skip and
        rely on _last_drain to arm the next reconcile tick), and every
        main-thread caller wraps its _drain() in the lock — so at most ONE
        drain runs at a time across the whole watcher.

        The background handler (on_message) may run subprocess.run or write to
        files; both are thread-safe. The stamped-before-work _last_drain rule
        still holds inside _drain().
        """
        # Non-blocking: if another drain is running, this call is a no-op. The
        # SSE loop's next reconcile tick or arrival will re-invoke us; the
        # in-flight drain will surface backlog in the meantime.
        if not self._drain_lock.acquire(blocking=False):
            return

        def _run() -> None:
            try:
                self._drain()
            except DeadWakeSocket:
                # DeadWakeSocket must reach the run() loop to trigger exit; the
                # design re-checks _dead_wake_socket_reason() at every
                # _stream_once start, so a background failure surfaces on the
                # next reconnect.
                print(
                    "agentbus watch: background drain reported dead wake socket; "
                    "will exit on next reconnect",
                    file=sys.stderr,
                )
            except Exception as exc:
                # SEV-1 diagnostic fix (macbook-admin-bd8e86, thread
                # 01M08ZBXDD8PQ9J70MM4VDBZR0): str(exc) is '' for some
                # network-shaped errors — notably concurrent.futures.TimeoutError().
                # macbook's log showed `agentbus watch: background drain failed:`
                # with NOTHING after the colon, removing the one signal that fired
                # during the outage. When str is empty, name the type instead.
                tag = str(exc) or f"({type(exc).__name__})"
                print(f"agentbus watch: background drain failed: {tag}", file=sys.stderr)
            finally:
                # REG-3: release AFTER _drain returns, so a concurrent
                # _backoff_and_drain() blocks until we finish.
                self._drain_lock.release()

        # THE LOCK IS ALREADY HELD (line above). If the thread never starts,
        # `_run` never runs, its `finally` never fires, and the lock is held
        # forever by nobody — after which `_drain_async` is a permanent no-op
        # and `_backoff_and_drain`'s acquire blocks for the life of the
        # process. Watcher alive, answering nothing, exiting nothing: the
        # silent total wake-death this module exists to prevent, and the shape
        # `watch-status` still reports as RUNNING.
        #
        # `Thread.start()` raises RuntimeError("can't start new thread") under
        # thread/FD exhaustion and MemoryError under pressure — both plausible
        # on a loaded box running many watchers. Release on any failure so the
        # next arrival or reconcile tick can retry.
        try:
            self._drain_thread = threading.Thread(
                target=_run, name=f"agentbus-drain-{self.agent}", daemon=True
            )
            self._drain_thread.start()
        except BaseException as exc:  # must not strand the lock
            self._drain_lock.release()
            tag = str(exc) or f"({type(exc).__name__})"
            print(
                f"agentbus watch: could not start background drain thread: {tag}; "
                "will retry on the next arrival",
                file=sys.stderr,
                flush=True,
            )

    def _drain(self) -> int:
        """Deliver everything after the cursor. Used at startup and after every
        wake, so a missed SSE event still surfaces."""
        # THE SOCKET CAN DIE MID-STREAM, not just at startup. The 2026-08-11
        # zombie started when its session socket was alive and only lost it
        # later, when that session ended. Every drain re-validates the inject
        # target so a watcher cannot keep consuming arrivals into a dead channel
        # for hours after its session is gone.
        reason = _dead_wake_socket_reason()
        if reason:
            raise DeadWakeSocket(reason)

        seen = 0
        # Stamped BEFORE the work, so a slow drain cannot immediately re-arm the
        # reconcile timer and busy-loop against the server.
        self._last_drain = time.monotonic()
        while True:
            batch = self.bus.inbox(self.cursor, limit=100, agent=self.agent)
            if not batch:
                return seen
            before = self.cursor
            for message in batch:
                self.cursor = max(self.cursor, message.seq)
                seen += 1
                try:
                    self.on_message(message.raw)
                except Exception as exc:
                    print(f"agentbus watch: handler failed: {exc}", file=sys.stderr)
            self._save_cursor()
            if self.cursor <= before:
                # PROGRESS GUARD (review #23, issuedb #29): a page that does not
                # advance the cursor would be fetched again forever at full speed,
                # holding the drain lock — a watcher RUNNING and deaf. Stop this
                # drain; the next reconcile tick retries.
                print(
                    f"agentbus watch: inbox page of {len(batch)} did not advance the "
                    f"cursor past {before}; stopping this drain (server seq anomaly?)",
                    file=sys.stderr,
                    flush=True,
                )
                return seen
