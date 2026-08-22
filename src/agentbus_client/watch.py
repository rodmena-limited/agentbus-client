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
from typing import Any

import httpx

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
from ._watch_drain import WatcherDrainMixin
from ._watch_handlers import (
    append_file,
    notify_command,
    print_line,
)
from ._watch_state import WatcherStateMixin
from .client import AgentBus, AgentBusError, AuthError

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


class Watcher(WatcherStateMixin, WatcherDrainMixin):
    def __init__(
        self,
        bus: AgentBus,
        agent: str,
        *,
        on_message: Callable[[dict[str, Any]], None] | None = None,
        cursor: int = 0,
        state_path: Path | None = None,
        workspace: str | None = None,
        wake_capable: bool = True,
    ) -> None:
        self.bus = bus
        self.agent = agent
        self.on_message = on_message or (lambda _m: None)
        self.state_path = state_path
        # Stamped into the state file so a cursor can never be read back in a
        # workspace it did not come from. The caller resolves it; the client does
        # not carry one, and a getattr() default would have written null forever
        # while looking like it recorded something.
        self.workspace = workspace
        state = self._load_state()
        self.cursor = cursor or int(state.get("cursor", 0) or 0)
        # SEV-1 fix #6 (macbook): persist _failures + last_failure_at across
        # restarts so an OS-supervisor loop cannot reset the backoff to 1s
        # every ~second during an outage. Reset only if the persisted failure
        # is stale (older than _FAILURES_TTL_SECONDS) — otherwise a watcher
        # that was up for hours would start its next reconnect at some high
        # step for no reason.
        persisted_failures = int(state.get("failures", 0) or 0)
        persisted_last_failure = float(state.get("last_failure_at", 0) or 0)
        if persisted_last_failure and time.time() - persisted_last_failure < _FAILURES_TTL_SECONDS:
            self._failures = persisted_failures
        else:
            self._failures = 0
        self._last_failure_at = persisted_last_failure
        self._last_drain = time.monotonic()
        # SEV-2-I (#234): background thread for reconcile drains. When _drain
        # runs inline in the SSE iter_lines loop and the drain is deep (100
        # messages x ~30 ms + slow bus), the ReadTimeout deadline races the
        # drain and the client goes into a reconnect storm. Offloading it lets
        # the SSE loop keep consuming keepalives.
        self._drain_thread: threading.Thread | None = None
        self._drain_lock = threading.Lock()
        # Serialises state-file publishes from the drain thread and the main
        # thread (peer review C2 / review #23 S10): each writer also uses its own
        # tmp name, so a reader only ever sees a complete file.
        self._state_lock = threading.Lock()
        # Does this watcher's handler START A TURN, or only record?
        #
        # `--append` writes arrivals to a file and `print_line` writes them to a
        # terminal nobody is reading. Both consume, both advance the cursor, both
        # answer liveness challenges, and NEITHER can wake a session. david ran a
        # recorder for two days reading `wake_channel: true` the entire time.
        #
        # Defaults to True because a library caller passing its own on_message
        # usually IS doing something with it, and because claiming less than the
        # truth would make working setups look broken.
        self.wake_capable = wake_capable

    # ------------------------------------------------------------- stream

    def _stream_once(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.bus.api_key}",
            "X-AgentBus-Agent": self.agent,
            "Accept": "text/event-stream",
            "Last-Event-ID": str(self.cursor),
            # The server cannot otherwise tell a waker from a recorder;
            # it counted sockets and called that a wake channel.
            "X-AgentBus-Wake-Capable": "1" if self.wake_capable else "0",
        }
        # httpx wants str values; a None header is "not set", not an empty one.
        stream_headers = {k: v for k, v in headers.items() if v is not None}
        client = httpx.Client(timeout=httpx.Timeout(STREAM_READ_DEADLINE, connect=15.0))
        with (
            client,
            client.stream(
                "GET", f"{self.bus.base_url}/v1/stream", headers=stream_headers
            ) as response,
        ):
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # GET /v1/stream returned 401/403 — the SAME revocation signal
                # as an `event: unauthorized` frame. Confirm against REST before
                # believing it — only a confirmed revocation ends the watch (via
                # AuthError -> cli.py exit 8); a blip stays transient.
                if exc.response.status_code in (401, 403, 410) and self._key_really_revoked():
                    raise self._terminal_auth_error(
                        self._refused_status(int(exc.response.status_code)), "stream"
                    ) from exc
                raise
            # The ladder resets only once the stream has PROVED healthy for
            # STREAM_HEALTHY_SECONDS (issuedb #27), not on the bare 200.
            opened_at = time.monotonic()
            for line in response.iter_lines():
                # THE WAKE TARGET CAN DIE WHILE THE STREAM STAYS HEALTHY (peer
                # review C3): the drain thread's DeadWakeSocket was swallowed and
                # a keepalive-only stream never reconnected, so a watcher whose
                # session had ended kept the subscription and kept reporting
                # wake_capable. Re-validate on every frame, keepalives included.
                reason = _dead_wake_socket_reason()
                if reason:
                    raise DeadWakeSocket(reason)
                if (self._failures or self._last_failure_at) and (
                    time.monotonic() - opened_at >= STREAM_HEALTHY_SECONDS
                ):
                    self._failures = 0
                    self._last_failure_at = 0.0
                    self._save_cursor()
                if line.startswith("event: unauthorized"):
                    # DO NOT take this as final on the strength of one frame.
                    #
                    # Exiting here is the only path in this client that gives
                    # up permanently, and it used to fire on the server's
                    # word alone. The server could emit `unauthorized` when
                    # it merely FAILED TO DETERMINE the key's state — a
                    # database blip, a pool torn down mid-deploy — so a
                    # watcher with a perfectly valid credential would exit,
                    # print one line to a terminal nobody was attached to,
                    # and never reconnect. A remote peer went silent for an
                    # hour exactly that way.
                    #
                    # Confirm against REST before believing it. If the key is
                    # genuinely revoked, this raises and we stop. If it
                    # answers, the stream was wrong and we reconnect.
                    if self._key_really_revoked():
                        raise self._terminal_auth_error(
                            self._refused_status(401), "stream said unauthorized"
                        )
                    print(
                        "agentbus watch: stream said unauthorized but the key "
                        "still works; treating as transient and reconnecting",
                        file=sys.stderr,
                    )
                    break
                if line.startswith(("data: ", "id: ")):
                    # Any signal at all means something changed; drain
                    # authoritatively from the cursor rather than trusting
                    # the event body, so a malformed or partial frame cannot
                    # lose a message. #234 SEV-2-I: off-thread, so keepalives
                    # keep being read while the drain works through backlog.
                    self._drain_async()
                elif time.monotonic() - self._last_drain >= STREAM_RECONCILE_SECONDS:
                    # A KEEPALIVE IS ALSO A TICK, AND WITHOUT THIS THE WAKE
                    # PATH CAN DIE WHILE EVERY SIGNAL READS HEALTHY.
                    #
                    # bob caught this live on david: monitor RUNNING, cursor
                    # frozen at 484, mail arriving, nothing surfaced, and
                    # presence still `responsive` because the process
                    # answering liveness was a different watcher that cannot
                    # start a turn. The loop above drains only on data:/id:,
                    # so if the server ever fails to push to THIS stream, the
                    # client had no second way of finding out:
                    #
                    #   keepalive arrives every 20s  -> read deadline (60s)
                    #                                   never fires
                    #   keepalive is a comment frame -> no drain
                    #   => silent until someone restarts the process
                    #
                    # The 20s keepalive we added to fix the HALF-OPEN link is
                    # what made a missed push permanent: before it, a quiet
                    # stream eventually hit the deadline and drained. One
                    # fix's cure became the next fix's disease, so the
                    # reconcile is not belt-and-braces, it is the thing that
                    # stops push from being the ONLY way to learn of mail.
                    # #234 SEV-2-I: off-thread — a slow reconcile drain must
                    # not race the SSE read deadline.
                    self._drain_async()

    def run(self, once: bool = False) -> int:
        # CHECK THE WAKE TARGET BEFORE ANYTHING ELSE. The 2026-08-11 outage was
        # a watcher from a dead session still streaming: it drained, it answered
        # liveness, it advanced its cursor — and every --exec injected into a
        # socket that no longer existed, so the operator's email was delivered
        # and never woke anyone. A watcher whose inject target is gone must stop
        # LOUDLY, not drain silently: silence here is byte-identical to an empty
        # inbox, which is the exact failure this client exists to prevent.
        reason = _dead_wake_socket_reason()
        if reason:
            raise DeadWakeSocket(reason)

        # SEV-1 follow-up (macbook-admin-bd8e86, blackhole test on 0.9.24
        # against 203.0.113.1): the startup drain is a NETWORK call that
        # used to sit OUTSIDE the reconnect envelope, so a watcher starting
        # against a down network exited immediately (cleaner in 0.9.24 —
        # TransportError instead of raw CFT — but still exited). The whole
        # requirement is "a watcher must be able to start with the network
        # down and sit in backoff until it returns", which this fixes.
        #
        # Also stamps client_version to disk RIGHT NOW so `doctor --wake`
        # does not report a stale version on a quiet inbox (macbook's
        # instrument-lag observation: state file was previously only
        # rewritten when the cursor advanced, so the version field lagged
        # arbitrarily on a healthy watcher with nothing arriving yet).
        self._save_cursor()
        # REG-3 (round-3 audit): startup drain must serialize with any
        # background drain _drain_async might spawn later. The lock is idle
        # here; acquiring it is instant and keeps the invariant honest.
        delivered = 0
        try:
            with self._drain_lock:
                delivered = self._drain()
        except DeadWakeSocket:
            raise
        except AuthError:
            # Startup on a revoked credential is TERMINAL, not a reason to
            # defer into the reconnect loop — the reconnect loop would just
            # hammer the bus with a key that will never work. Re-raise so
            # cmd_watch returns 8 and the monitor stops.
            raise
        except AgentBusError as exc:
            # 403/410 on the startup drain are just as terminal as 401 once
            # REST confirms them (peer review C4); anything else is deferred.
            if exc.status in (403, 410) and self._key_really_revoked():
                raise self._terminal_auth_error(
                    self._refused_status(exc.status), "startup drain"
                ) from exc
            tag = str(exc) or f"({type(exc).__name__})"
            print(
                f"agentbus watch: startup drain deferred ({tag}); entering reconnect loop",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:  # startup drain MUST NOT block launch
            # The reconnect envelope in the while loop below handles this
            # exact class of failure. Defer to it — announcement is one
            # stderr line so the operator can see the deferral happened.
            tag = str(exc) or f"({type(exc).__name__})"
            print(
                f"agentbus watch: startup drain deferred ({tag}); entering reconnect loop",
                file=sys.stderr,
                flush=True,
            )
        if delivered:
            print(f"agentbus watch: {delivered} message(s) waiting at startup", file=sys.stderr)
        if once:
            return 0

        while True:
            try:
                self._stream_once()
            except DeadWakeSocket:
                raise
            except AuthError:
                # A REST-confirmed revoked credential is TERMINAL. The generic
                # handler below would back off and retry forever, hammering the
                # bus with a key that will never work — the revocation
                # asymmetry the audit flagged. Re-raise so cmd_watch returns 8.
                raise
            except KeyboardInterrupt:
                return 0
            except httpx.ReadTimeout:
                # NOT a transport error to shrug at: this is the wake being dead
                # while the socket still looks alive, and it is the only notice
                # anyone gets. It goes to STDOUT deliberately — Claude Code
                # delivers stdout to the session and DISCARDS stderr, so putting
                # it with the other diagnostics below would make the one event
                # worth seeing the one event nobody sees.
                self._backoff_and_drain(
                    f"no data for {STREAM_READ_DEADLINE:.0f}s though the server "
                    f"sends a keepalive every {STREAM_KEEPALIVE_SECONDS:.0f}s — "
                    "treating the link as dead and reconnecting; mail may have "
                    "been waiting during that window",
                    stream=sys.stdout,
                )
            except Exception as exc:
                self._backoff_and_drain(f"stream dropped ({exc})")
            else:
                # A CLEAN EOF IS A DROP TOO (review #23, issuedb #27). The server
                # (or a proxy in front of it) closed the response without an error;
                # looping straight back produced 217 reconnects in 12s with no log
                # line. Back off and log exactly like any other drop.
                self._backoff_and_drain("stream ended (closed cleanly by the far end)")

    def _backoff_and_drain(self, reason: str, stream: Any = sys.stderr) -> None:
        """Announce the failure, opportunistically drain HTTP, THEN back off.

        SEV-1 (macbook-admin-bd8e86 thread 01M08ZBXDD8PQ9J70MM4VDBZR0): this
        method is the reconnect handler, and it USED to crash during exactly
        the condition it was written to handle. Two independent defects:

          (1) The inner drain calls bus.inbox() — a NETWORK call — while the
              network is down. It raised concurrent.futures.TimeoutError,
              which on Python 3.10 is NOT an OSError subclass, so the
              hand-written suppress(AgentBusError, OSError, httpx.HTTPError,
              ValueError, KeyError) let it escape. The traceback propagated
              out of run() and killed the watcher process.

          (2) time.sleep(delay) sat AFTER the drain, unguarded. Any exception
              inside the try skipped the sleep too, so even a would-be
              catchable failure meant zero backoff.

        Fix: catch BaseException from the drain (deliberate — the whole
        point is that NOTHING here may escape upward except DeadWakeSocket,
        which is the one signal that means "the wake target is gone, do not
        retry"). Move the sleep into `finally` so backoff always happens.
        Fix #1 at _run_with_resilience translates CFT to TransportError
        upstream, so this catch-all is defense in depth, not the primary
        gate.
        """
        base_delay = RECONNECT_BACKOFF[min(self._failures, len(RECONNECT_BACKOFF) - 1)]
        # SEV-1 fix #6 (macbook, backend endorsed >= +/-10% recommended):
        # jitter the sleep by +/-_BACKOFF_JITTER_FRACTION so N watchers coming
        # back after a shared bus restart do not reconnect in lockstep and
        # trip the server's 30 QPS bulkhead. The jitter is per-call so tests
        # can seed random themselves; here we just call random.uniform.
        jitter = base_delay * _BACKOFF_JITTER_FRACTION
        delay = max(0.0, base_delay + random.uniform(-jitter, jitter))
        self._failures += 1
        self._last_failure_at = time.time()
        # Persist the new backoff position IMMEDIATELY so an OS-supervisor
        # crash-and-restart during THIS backoff does not reset us to 1s and
        # start hammering (macbook secondary defect c: every log line at
        # "retrying in 1s" across a whole outage). _save_cursor is
        # misnamed — it writes the whole state including cursor + backoff
        # fields — but preserved for API compatibility with existing tests.
        self._save_cursor()
        print(
            f"agentbus watch: {reason}; retrying in {delay:.1f}s "
            f"(base {base_delay}s, failures={self._failures})",
            file=stream,
            flush=True,
        )
        # Drain over HTTP while disconnected so a long outage does not mean a
        # long silence. REG-3 (round-3 audit): serialize with any in-flight
        # background drain via _drain_lock — otherwise both threads advance
        # the cursor and fire on_message twice per delivery.
        # BOUNDED ACQUIRE — the recovery path must never wait forever on the
        # failing path. A background drain can legitimately run for minutes
        # (bus.inbox under SDK retries, then up to 100 messages x on_message,
        # where notify_command allows AGENTBUS_EXEC_TIMEOUT seconds each), and
        # an unbounded `with self._drain_lock:` here stalled the reconnect for
        # that entire time with no backoff sleep and no log line explaining
        # the gap. The drain below is OPPORTUNISTIC — its whole purpose is to
        # surface mail early during an outage — so skipping it costs nothing
        # but a little latency, while blocking on it costs the reconnect.
        got_lock = self._drain_lock.acquire(timeout=_DRAIN_LOCK_TIMEOUT_SECONDS)
        try:
            if not got_lock:
                print(
                    f"agentbus watch: a drain is still in flight after "
                    f"{_DRAIN_LOCK_TIMEOUT_SECONDS:.0f}s; skipping the opportunistic "
                    "drain and backing off anyway",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                try:
                    self._drain()
                except (DeadWakeSocket, WatchTerminated, KeyboardInterrupt):
                    # The session is gone, or we were told to stop: do NOT sleep
                    # and retry. Re-raise (finally still runs) so run() sees it.
                    raise
                except BaseException as exc:  # deliberate: reconnect handler MUST be total
                    # Empty message diagnostic: some exceptions (notably
                    # concurrent.futures.TimeoutError()) stringify to '',
                    # which produced logs like `handler failed:` with nothing
                    # after the colon. Name the type when the message is empty.
                    tag = str(exc) or f"({type(exc).__name__})"
                    print(
                        f"agentbus watch: drain during backoff failed: {tag}; "
                        f"stream will be re-opened on the next attempt",
                        file=sys.stderr,
                        flush=True,
                    )
        finally:
            # Release only what we actually took — a bounded acquire that
            # timed out holds nothing, and releasing an unheld lock raises.
            if got_lock:
                self._drain_lock.release()
            # Sleep ALWAYS happens — otherwise a failing drain converted an
            # N-second backoff into a 0-second one, hammering the server at
            # 1Hz during multi-minute outages (macbook's secondary defect b).
            time.sleep(delay)
