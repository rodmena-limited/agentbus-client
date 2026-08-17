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


class Watcher:
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

    # ------------------------------------------------------------- cursor

    def _load_cursor(self) -> int:
        """Kept for backwards compatibility (existing tests import it).
        Prefer `_load_state()` for the full record."""
        return int(self._load_state().get("cursor", 0) or 0)

    def _load_state(self) -> dict[str, Any]:
        """Full watcher state — cursor + persisted backoff fields (macbook #6).

        Resume where the last run stopped, so a restart does not replay
        everything or — worse — skip what arrived while it was down. Also
        surface `failures` + `last_failure_at` so the reconnect backoff can
        pick up where the previous process crashed, instead of resetting to
        1s every relaunch (which turned an OS-supervisor loop into a 1Hz
        hammer during multi-minute outages).
        """
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                if isinstance(data, dict):
                    return data
            except (ValueError, OSError):
                return {}
        return {}

    def _key_really_revoked(self) -> bool:
        """Second opinion before this client ever gives up for good.

        Only a REST call that actually refuses us counts as revocation. Anything
        else — a timeout, a 5xx, a connection error — means we could not tell,
        and 'could not tell' must never end the watch.

        SEV-2-G (#234): classify by TYPED status + code, not by grepping the
        exception's stringified message. A server rewording that drops the word
        'revoked' or '401' from the body used to turn a real refusal into
        'transient' and the watcher reconnected forever. AgentBusError already
        carries .status and .code; use them. The string check remains as a
        narrow belt-and-braces only for a non-standard exception path where the
        typed fields are absent.
        """
        try:
            self.bus.whoami()
            return False
        except AuthError:
            # AuthError == 401 with any code the server used (invalid_api_key,
            # revoked, key_agent_mismatch under auth). Any 401 is revocation for
            # this watch's purposes — the credential does not authenticate.
            return True
        except AgentBusError as exc:
            # A structured error with an explicit 401 status ALSO counts,
            # regardless of subclass (a bare AgentBusError raised somewhere).
            # 5xx/transport failures are NOT revocation and drop through to the
            # transient path below.
            return exc.status == 401
        except Exception as exc:
            # Last-resort classifier for an exception the server client did not
            # wrap. Deliberately kept because a network stack can raise all
            # kinds of things; the typed path above is what matters in practice.
            text = f"{type(exc).__name__}: {exc}".lower()
            return (
                "401" in text
                or "unauthenticated" in text
                or "invalid_api_key" in text
                or "revoked" in text
            )

    def _save_cursor(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            # Record the workspace so this cursor can never be mistaken for
            # another workspace's. A cursor is a per-delivery sequence INSIDE one
            # workspace; the same agent name routinely exists in several.
            # STAMP THE VERSION THIS PROCESS ACTUALLY LOADED.
            #
            # bob found `doctor --wake` reporting the wake chain PROVEN against a
            # watcher running PRE-UPGRADE code: it checked that a process existed,
            # never which build that process had imported. So upgrading the client
            # and not restarting produced a green diagnostic on the exact
            # configuration the upgrade was meant to fix — my own
            # published-vs-installed lesson, living inside the instrument people
            # trust to tell them the wake path is fine.
            #
            # A running process is the only thing that can answer this honestly:
            # the installed version on disk says nothing about what is in memory.
            tmp.write_text(
                json.dumps(
                    {
                        "cursor": self.cursor,
                        "agent": self.agent,
                        "workspace": self.workspace,
                        "client_version": _client_version(),
                        # WHICH PROCESS STAMPED THAT VERSION.
                        #
                        # agentbus-ui-c760a1 (thread 01M08ZWE0XCTPJG1R0ZBXP8K7P)
                        # spotted that `client_version` alone means
                        # "last writer's version", not "the running watcher's
                        # version" — a short-lived `agentbus watch --once` on a
                        # NEW cli stamps the new version and exits, while the
                        # long-running plugin monitor carries on with OLD code.
                        # watch-status would then compare new-vs-new and report
                        # a match while the actual watcher was stale. That is
                        # the same instrument-lies-about-reality class that
                        # produced this whole incident.
                        #
                        # Recording the PID lets a reader verify the stamp
                        # belongs to the process it is actually asking about,
                        # and say "cannot confirm" instead of asserting a
                        # match it has not earned.
                        "pid": os.getpid(),
                        # SEV-1 fix #6 (macbook): persist so the backoff step
                        # survives a process restart. Without it an OS
                        # supervisor's restart-on-crash loop reset the ladder
                        # to 1s on every relaunch during a multi-minute
                        # outage — 1Hz hammer against the recovering server.
                        "failures": self._failures,
                        "last_failure_at": self._last_failure_at,
                    }
                )
            )
            tmp.replace(self.state_path)
        except OSError:
            pass  # a watcher must not die because it cannot checkpoint

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

        self._drain_thread = threading.Thread(
            target=_run, name=f"agentbus-drain-{self.agent}", daemon=True
        )
        self._drain_thread.start()

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
            for message in batch:
                self.cursor = max(self.cursor, message.seq)
                seen += 1
                try:
                    self.on_message(message.raw)
                except Exception as exc:
                    print(f"agentbus watch: handler failed: {exc}", file=sys.stderr)
            self._save_cursor()

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
        with (
            httpx.Client(timeout=httpx.Timeout(STREAM_READ_DEADLINE, connect=15.0)) as client,
            client.stream("GET", f"{self.bus.base_url}/v1/stream", headers=headers) as response,
        ):
            response.raise_for_status()
            # Successful reconnect: clear the persisted backoff position too,
            # so a subsequent process restart resumes from a clean baseline
            # rather than reading a stale `failures=6` and starting at 60s.
            if self._failures != 0 or self._last_failure_at:
                self._failures = 0
                self._last_failure_at = 0.0
                self._save_cursor()
            for line in response.iter_lines():
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
                        raise PermissionError("API key was revoked")
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
        except Exception as exc:  # noqa: BLE001 — startup drain MUST NOT block launch
            # The reconnect envelope in the while loop below handles this
            # exact class of failure. Defer to it — announcement is one
            # stderr line so the operator can see the deferral happened.
            tag = str(exc) or f"({type(exc).__name__})"
            print(
                f"agentbus watch: startup drain deferred ({tag}); "
                "entering reconnect loop",
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
            except PermissionError as exc:
                print(f"agentbus watch: stopping — {exc}", file=sys.stderr)
                return 0
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
        try:
            with self._drain_lock:
                try:
                    self._drain()
                except DeadWakeSocket:
                    # The socket died while we were disconnected — the session
                    # is gone. Do NOT sleep and retry a wake target that
                    # cannot come back. Re-raise (finally still runs) so
                    # run() sees it.
                    raise
                except BaseException as exc:  # noqa: BLE001 — deliberate: reconnect handler MUST be total
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
            # Sleep ALWAYS happens — otherwise a failing drain converted an
            # N-second backoff into a 0-second one, hammering the server at
            # 1Hz during multi-minute outages (macbook's secondary defect b).
            time.sleep(delay)


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
