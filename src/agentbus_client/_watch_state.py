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


class WatcherStateMixin(_WatcherBase):
    """Methods of Watcher carved out for the file-size cap (review #23).

    Mixed back into Watcher; relies on the attributes its __init__ sets."""

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

    def _credential_refusal(self) -> int | None:
        """The HTTP status of a REST-CONFIRMED definitive refusal, or None.

        Only a REST call that actually refuses us counts. Anything else — a
        timeout, a 5xx, a connection error — means we could not tell, and
        'could not tell' must never end the watch.

        401 (revoked/invalid key), 403 (key bound to another agent, suspended
        workspace) and 410 (agent RETIRED) are all DEFINITIVE: retrying them at
        the backoff cadence forever while watch-status says RUNNING was peer
        review C4. Classified by TYPED status, never by grepping the message
        (SEV-2-G); an exception the SDK did not type is "could not tell" (S6).
        """
        try:
            self.bus.whoami()
            return None
        except AuthError as exc:
            return int(exc.status or 401)
        except AgentBusError as exc:
            return exc.status if exc.status in (401, 403, 410) else None
        except Exception:
            return None

    def _key_really_revoked(self) -> bool:
        """Second opinion before this client ever gives up for good.

        The single gate every terminal path consults (tests stub it); the
        confirmed HTTP status is kept on `_last_refusal` for the error text.
        """
        status = self._credential_refusal()
        self._last_refusal = status
        return status is not None

    @staticmethod
    def _terminal_auth_error(status: int, where: str) -> AuthError:
        code = {401: "revoked", 403: "forbidden", 410: "retired"}.get(status, "refused")
        return AuthError(
            f"credential definitively refused by the bus (HTTP {status}, {where}); "
            "not retrying — fix the key or the agent registration",
            code=code,
            status=status,
        )

    def _refused_status(self, fallback: int) -> int:
        return int(getattr(self, "_last_refusal", None) or fallback)

    def _save_cursor(self) -> None:
        if not self.state_path:
            return
        with self._state_lock:
            self._save_cursor_locked()

    def _save_cursor_locked(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # Unique per writer (review #23, S10): the drain thread and the main
            # thread both checkpoint, and a shared ".tmp" name raced.
            tmp = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
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
