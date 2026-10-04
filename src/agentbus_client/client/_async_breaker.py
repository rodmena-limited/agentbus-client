from __future__ import annotations

import time


class _AsyncCircuitBreaker:
    """Minimal per-process breaker for the async client (REG-7 follow-up).

    resilient_circuit is sync-only, so the async path hand-rolls one with the same
    semantics: it sees POST-RETRY outcomes (a whole failing sequence is ONE
    failure); after `failure_limit` consecutive failures it opens for `cooldown`
    seconds and calls fail fast; when the cooldown lapses it is half-open and admits
    ONE probe at a time (review #23, S9) — a failing probe re-opens it immediately,
    `success_limit` clean probes close it. State is plain counters, so it has no
    event-loop affinity and is shared by every AsyncAgentBus in the process.
    """

    def __init__(self, failure_limit: int = 5, success_limit: int = 2, cooldown: float = 30.0):
        self.failure_limit = failure_limit
        self.success_limit = success_limit
        self.cooldown = cooldown
        self._open_until = 0.0
        self._half_open = False
        self._failures = 0
        self._successes = 0
        self._probe_inflight = False
        self._last_error: BaseException | None = None

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def last_error(self) -> BaseException | None:
        return self._last_error

    def admit(self) -> bool:
        """May a sequence start now? False while open, or while a half-open probe flies."""
        if self.is_open():
            return False
        if self._half_open:
            if self._probe_inflight:
                return False
            self._probe_inflight = True
        return True

    def release_probe(self) -> None:
        """A sequence ended without a verdict (cancelled): free the probe slot."""
        self._probe_inflight = False

    def on_success(self) -> None:
        self._probe_inflight = False
        self._failures = 0
        self._successes += 1
        if self._successes >= self.success_limit:
            self._open_until = 0.0
            self._half_open = False
            self._successes = 0

    def on_failure(self, exc: BaseException) -> None:
        self._probe_inflight = False
        self._successes = 0
        self._last_error = exc
        if self._half_open:
            # A failed half-open probe re-opens immediately.
            self._open_until = time.monotonic() + self.cooldown
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= self.failure_limit:
            self._open_until = time.monotonic() + self.cooldown
            self._half_open = True
            self._failures = 0
