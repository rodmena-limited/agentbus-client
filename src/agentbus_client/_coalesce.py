"""Hook-wake coalescing for `agentbus watch` (SPECS/0009, issuedb #9).

Motivation, from peer agentbus-ui-c760a1's 15-message burst test:

  A stream of N nearly-simultaneous deliveries triggered N wake hooks,
  each of which prepends ~30 lines of boilerplate to the session's
  UserPromptSubmit stream. Two peers doing real work made each other's
  sessions unreadable.

The design (approach #3 in the peer's proposal, confirmed by farshid):

  * A LONE delivery still fires the wake hook IMMEDIATELY, so an
    interactive "approve this?" message pays no latency penalty.
  * A BURST of deliveries — arrivals inside a 2500 ms window OR arrivals
    less than 800 ms apart — coalesces into one wake carrying an envelope
    of all pending messages.
  * `urgent` priority always bypasses coalescing, so a "prod down" ping
    never sits behind a quiet timer.

The wake-hook contract:

  * count == 1 : the sink receives the SINGLE message dict, as it always
    did. This is a HARD compatibility contract: installed hook scripts
    that grep the current `subject` / `sender_display` fields must keep
    working with no change and no schema version bump.
  * count >= 2 : the sink receives an envelope dict of the form
        {
          "kind": "coalesced",
          "count": N,
          "messages": [ <full delivery dict>, <full delivery dict>, ... ],
          # For backward-compatible per-message placeholder substitution
          # in `--exec` templates and the like, the FIRST message's
          # top-level fields are also projected onto the envelope:
          "subject":         "3 coalesced messages (first: <first_subj>)",
          "sender_display":  "(coalesced)",
          "delivery_id":     "<first delivery id>",
          "message_id":      "coalesced",
          "thread_id":       "<first thread id>",
          "agent_seq":       "<last agent seq — where cursor should sit>",
          "direction":       "coalesced",
          "inbound_source":  "",
        }
    So a `notify_command` template written for the single-message case
    still substitutes without KeyError; a coalescing-aware consumer reads
    `kind`, `count`, and `messages`.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from typing import Any

# Envelope schema key used as the discriminator (peer-confirmed spelling).
ENVELOPE_KIND = "coalesced"


def is_envelope(msg: dict[str, Any]) -> bool:
    """True when the sink received a coalesced envelope, not a single message."""
    return isinstance(msg, dict) and msg.get("kind") == ENVELOPE_KIND


def _make_envelope(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of messages into the coalesced-envelope shape.

    Projects a handful of first-message fields to the top level so an
    existing `notify_command` template does not KeyError on substitution.
    The `messages[]` list is the authoritative payload; the top-level
    projections are convenience.
    """
    first = messages[0] if messages else {}
    last = messages[-1] if messages else {}
    first_subject = first.get("subject") or "(no subject)"
    subject = f"{len(messages)} coalesced messages (first: {first_subject})"
    return {
        "kind": ENVELOPE_KIND,
        "count": len(messages),
        "messages": messages,
        "subject": subject,
        "sender_display": "(coalesced)",
        "sender_address": "(coalesced)",
        "delivery_id": first.get("delivery_id") or "",
        "message_id": "coalesced",
        "thread_id": first.get("thread_id") or "",
        "agent_seq": last.get("agent_seq") or "",
        "direction": "coalesced",
        "inbound_source": "",
    }


class Coalescer:
    """State-machine gate between the watcher's drain and the wake sink.

    The sink is called exactly once per envelope:
      * once for the FIRST message of a burst (leading-edge, no wait)
      * once for each urgent message (bypasses the window entirely)
      * once for the accumulated tail (>= 1 message) when the window closes

    Threading model:
      * `handle` may be called from the drain thread (main or background).
      * The trailing-window timer fires from a `threading.Timer` daemon.
      * A single `threading.Lock` serialises everything, including the sink
        call itself. The sink is expected to be fast (subprocess.run with
        a 5 s timeout in `notify_command`, a file append, or a print);
        holding the lock across it is safe.

    Never crashes on a sink exception — the wake path must survive a bad
    hook. Errors are swallowed here and expected to surface via the
    per-handler `handler failed` prints in `watch._drain`.
    """

    def __init__(
        self,
        sink: Callable[[dict[str, Any]], None],
        *,
        window_ms: int = 2500,
        quiet_ms: int = 800,
    ) -> None:
        # window_ms is the HARD upper bound on how long a burst can hold
        # its tail — a caller that never falls silent (arrivals every
        # quiet_ms - 1 forever) still flushes within this cap.
        # quiet_ms is the DEBOUNCE — the tail flushes after this much
        # silence, whichever bound is smaller.
        self._sink = sink
        self._window = max(0.0, window_ms / 1000.0)
        self._quiet = max(0.0, quiet_ms / 1000.0)
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._hard_deadline: float | None = None  # monotonic time
        self._quiet_deadline: float | None = None
        self._timer: threading.Timer | None = None
        self._closed = False

    # ---------------------------------------------------------- public

    def handle(self, msg: dict[str, Any]) -> None:
        if self._closed:
            # A late arrival after close: fire directly. Do not resurrect
            # the coalescer; the caller wanted us stopped.
            self._safe_sink(msg)
            return

        priority = (msg.get("priority") or "normal").lower()
        if priority == "urgent":
            # Urgent bypass: whatever is in the buffer must be delivered
            # first (preserve arrival order the peer expects), THEN the
            # urgent message fires immediately without opening a window.
            with self._lock:
                self._flush_locked()
                self._safe_sink(msg)
            return

        with self._lock:
            if self._hard_deadline is None:
                # Leading edge — fire NOW, open the trailing window.
                # The buffer stays empty; only SUBSEQUENT arrivals in
                # this window go into it.
                now = time.monotonic()
                self._hard_deadline = now + self._window
                self._quiet_deadline = now + self._quiet
                self._safe_sink(msg)
                self._arm_timer_locked()
            else:
                # Trailing window is open — accumulate, reset the quiet
                # deadline (still bounded by the hard one).
                self._buffer.append(msg)
                self._quiet_deadline = min(
                    time.monotonic() + self._quiet,
                    self._hard_deadline,
                )
                self._arm_timer_locked()

    def close(self) -> None:
        """Flush any pending buffer synchronously and stop the timer.

        Called at watcher shutdown so a graceful stop does not eat wakes.
        Idempotent; further `handle()` calls after close deliver directly
        (they cannot start a new window).
        """
        with self._lock:
            self._closed = True
            self._flush_locked()

    # ---------------------------------------------------------- internal

    def _arm_timer_locked(self) -> None:
        """(Re)start the trailing-window timer to fire at min(hard, quiet)."""
        if self._timer is not None:
            self._timer.cancel()
        if self._hard_deadline is None:
            return
        now = time.monotonic()
        wait_hard = max(0.0, self._hard_deadline - now)
        wait_quiet = max(0.0, (self._quiet_deadline or self._hard_deadline) - now)
        wait = min(wait_hard, wait_quiet)
        self._timer = threading.Timer(wait, self._on_timer)
        # Daemon so a lingering timer does not prevent process exit.
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self) -> None:
        """Fired by the Timer thread. Flushes if a deadline has actually
        passed; re-arms otherwise (a newer arrival may have extended
        quiet after this Timer was queued)."""
        with self._lock:
            if self._closed or self._hard_deadline is None:
                return
            now = time.monotonic()
            # Extended by a recent arrival while this timer was queued?
            if now < self._hard_deadline and (
                self._quiet_deadline is None or now < self._quiet_deadline
            ):
                self._arm_timer_locked()
                return
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Emit the buffered envelope (if any) and reset window state.

        MUST be called with self._lock held. The sink runs inside the
        lock — the peer's use case is fast (UserPromptSubmit hook that
        writes a file), and holding the lock keeps `count` and ordering
        honest against concurrent arrivals.
        """
        buffered = self._buffer
        if buffered:
            envelope = _make_envelope(buffered)
            self._buffer = []
            self._safe_sink(envelope)
        self._hard_deadline = None
        self._quiet_deadline = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _safe_sink(self, payload: dict[str, Any]) -> None:
        """Call the sink; never propagate its exceptions.

        A wake handler crashing must not take the coalescer down —
        watcher's `_drain` already logs handler exceptions.
        """
        # Swallow: the caller's handler wrapper in _drain also catches and logs.
        # The coalescer's job is to serialise wakes, not to be a second logging layer.
        with contextlib.suppress(Exception):
            self._sink(payload)
