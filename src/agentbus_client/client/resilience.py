"""Resilience layer shared by the sync and async clients: retry, breaker, bulkhead.

Design (REG-7, round-3 audit), inside-out around every SDK request:

    bulkman  <-  resilient_circuit.SafetyNet(CircuitProtector, RetryWithBackoff)  <-  httpx call

- RetryWithBackoff is INNERMOST: it decides "was this attempt transient? retry".
- CircuitProtector wraps the retry, so it sees POST-RETRY outcomes: a whole failing
  retry-sequence counts as ONE failure. NOTE WELL (review #23, issuedb #24): because
  SafetyNet applies policies in reverse, the breaker never sees the transport error
  itself — it sees `RetryLimitReached`, whose `__cause__` is the real error. The
  breaker's classifier therefore unwraps to the root cause. Before that fix the breaker
  recorded every exhausted sequence as a SUCCESS and never opened.
- bulkman sits OUTSIDE the retry so a whole send-with-3-retries holds ONE slot.
  bulkman's own breaker is ALWAYS OFF (house rule): resilient_circuit is the single
  breaker authority on this path.

Two further properties the review made explicit (issuedb #26):

- THE OUTER DEADLINE CANCELS WORK. `future.result(timeout=)` used to return control
  while the retry sequence kept running on a non-daemon pool thread for ~110 s,
  filling the 8-slot bulkhead so calls against a HEALTHY bus then failed, and blocking
  interpreter exit. Now every sequence carries a cancel flag set when its caller's
  deadline fires; the next attempt raises `_Abandoned` instead of touching the
  network, and each attempt's own httpx timeout is bounded by the remaining budget.
- EXIT IS NOT HELD HOSTAGE. A threading-shutdown hook abandons every in-flight
  sequence and cancels queued work before the executor is joined.

Everything is lazy-instantiated and a process-wide singleton, created under a lock
(issuedb #31: sixteen concurrent first callers used to get fifteen thread pools).
"""

from __future__ import annotations

import atexit
import concurrent.futures as _cf
import contextlib
import logging
import os
import sys
import threading
import time
import weakref
from fractions import Fraction
from typing import Any

import httpx

from .attachments import _encode_attachments  # noqa: F401  (re-export: historical import site)
from .errors import AgentBusError, ServiceUnavailable, TransportError

_ConcurrentFuturesTimeout = _cf.TimeoutError
_SDK_BULKHEAD: Any = None
_SDK_SAFETY_NET: Any = None
_ASYNC_CIRCUIT_BREAKER: _AsyncCircuitBreaker | None = None
_SDK_INIT_LOCK = threading.Lock()
_INFLIGHT: set[threading.Event] = set()
_INFLIGHT_LOCK = threading.Lock()
_SYNC_CLIENTS: weakref.WeakSet = weakref.WeakSet()
_CURRENT_CANCEL = threading.local()


class _Abandoned(Exception):
    """Raised inside a retry sequence whose caller has already given up.

    Never retried (the retry classifier refuses it) but COUNTED by the breaker:
    from the caller's point of view the bus did not answer in time.
    """


def _sdk_cb_limits() -> tuple[int, int]:
    """Breaker burst thresholds from env — SHARED by the sync and async breakers (A1)."""
    return (
        max(1, int(os.environ.get("AGENTBUS_SDK_CB_FAILURE_LIMIT", "5"))),
        max(1, int(os.environ.get("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "2"))),
    )


def _cb_window(n: int) -> Fraction:
    """A resilient_circuit threshold meaning "n consecutive outcomes".

    resilient_circuit sizes its window by the Fraction's DENOMINATOR, and
    `Fraction(n, n)` reduces to `1/1` — a one-slot window that trips on any single
    failure (issuedb #24; bulkman's own source carries the same warning). `(n-1)/n`
    keeps an n-slot window and trips when at least n-1 of the last n agree.
    """
    return Fraction(n - 1, n) if n >= 2 else Fraction(1, 1)


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap `RetryLimitReached` to the error the last attempt actually raised.

    Safe to call before resilient_circuit has ever been imported: if its
    exceptions module is not loaded, nothing can be a RetryLimitReached.
    """
    mod = sys.modules.get("resilient_circuit.exceptions")
    rlr = getattr(mod, "RetryLimitReached", None) if mod is not None else None
    hops = 0
    while rlr is not None and isinstance(exc, rlr) and exc.__cause__ is not None and hops < 8:
        exc = exc.__cause__
        hops += 1
    return exc


class _NonIdempotent(Exception):
    """Marker wrapper: this call must not be retried on a 5xx (#45).

    A 5xx says the server failed, NOT that it did nothing. For a call that
    creates something and carries no idempotency key, a retry after a partial
    write is a second half-created object — and for `register` the object is an
    IDENTITY, the one thing on this bus that must never be silently duplicated.
    Observed: one register call produced FOUR 500s with four distinct server
    error ids, invisible to the caller because only the last one surfaced.
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.__cause__ = cause
        self.original: BaseException = cause


def _is_transient_sdk_error(exc: BaseException) -> bool:
    """RETRY classifier: what an attempt may be retried on.

    Transport failures and 5xx are transient (rolling deploy, gateway 500/502/504,
    upstream 503). Every other typed AgentBusError is DEFINITIVE (401, 403, 404,
    409/413/422, 429) and passes through unchanged. An abandoned sequence is never
    retried. The 5xx branch is a STATUS check because only 503 maps to
    ServiceUnavailable; 500/502/504 surface as a bare AgentBusError.
    """
    if isinstance(exc, _NonIdempotent):
        return False
    exc = _root_cause(exc)
    if isinstance(exc, (_Abandoned, _NonIdempotent)):
        return False
    if isinstance(exc, (TransportError, ServiceUnavailable)):
        return True
    if isinstance(exc, AgentBusError) and 500 <= exc.status <= 599:
        return True
    return isinstance(exc, httpx.HTTPError)


def _breaker_should_handle(exc: BaseException) -> bool:
    """BREAKER classifier: what counts as "the bus failed us".

    Sees post-retry outcomes, so it unwraps RetryLimitReached first. An abandoned
    sequence counts as a failure — the caller's deadline fired without an answer.
    """
    exc = _root_cause(exc)
    if isinstance(exc, _Abandoned):
        return True
    return _is_transient_sdk_error(exc)


def _cancellable_backoff(min_delay: Any, max_delay: Any, factor: int, jitter: float) -> Any:
    """An ExponentialDelay whose wait ENDS THE MOMENT the sequence is abandoned.

    resilient_circuit calls `sleep(backoff.for_attempt(n))` between attempts — a
    blind time.sleep of up to 8s that an abandoned sequence used to serve in full
    before noticing its caller had gone (issuedb #26). This subclass performs the
    wait itself on the sequence's cancel flag (set by the outer deadline or the
    shutdown hook) and returns 0, so the library's own sleep is a no-op.
    """
    import resilient_circuit as rc

    class _CancellableDelay(rc.ExponentialDelay):
        def for_attempt(self, attempt: int) -> float:
            delay = super().for_attempt(attempt)
            flag = getattr(_CURRENT_CANCEL, "flag", None)
            if flag is None:
                return delay
            flag.wait(delay)
            return 0.0

    return _CancellableDelay(min_delay=min_delay, max_delay=max_delay, factor=factor, jitter=jitter)


def _daemon_executor(max_workers: int, prefix: str) -> Any:
    """A ThreadPoolExecutor whose workers are DAEMON threads and are not joined at exit.

    A stock executor's workers are non-daemon and registered for a join in
    concurrent.futures' shutdown hook, so a worker blocked in socket.recv (Ctrl-C one
    second into a 30s stall) held the interpreter open for the whole read timeout —
    closing the httpx client does not wake a blocked read (issuedb #26). Daemon
    workers are abandoned at exit instead; the SDK's own shutdown hook has already
    flagged their sequences as abandoned.

    Relies on the private `_worker` entry point, which has had the same four-argument
    shape from 3.7 through 3.13. The shape is CHECKED: if it ever changes, the stock
    executor is used (correct, merely slower to exit) rather than spawning workers
    that would fail on their first task.
    """
    import inspect
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import thread as _cft

    worker = getattr(_cft, "_worker", None)
    try:
        shape_ok = worker is not None and len(inspect.signature(worker).parameters) == 4
    except (TypeError, ValueError):
        shape_ok = False
    if not shape_ok:
        return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=prefix)

    class _DaemonExecutor(ThreadPoolExecutor):
        def _adjust_thread_count(self) -> None:
            if self._idle_semaphore.acquire(timeout=0):
                return
            queue = self._work_queue

            def _weakref_cb(_: Any, q: Any = queue) -> None:
                q.put(None)

            num_threads = len(self._threads)
            if num_threads < self._max_workers:
                t = threading.Thread(
                    name=f"{self._thread_name_prefix}_{num_threads}",
                    target=worker,
                    args=(weakref.ref(self, _weakref_cb), queue, self._initializer, self._initargs),
                    daemon=True,
                )
                t.start()
                self._threads.add(t)  # type: ignore[attr-defined]

    return _DaemonExecutor(max_workers=max_workers, thread_name_prefix=prefix)


def _sdk_bulkhead() -> Any:
    """Lazy singleton — one concurrency lane for the whole SDK per process.

    circuit_breaker_enabled is HARD-CODED False (house rule): bulkman is the
    concurrency lane, resilient_circuit is the breaker.
    """
    global _SDK_BULKHEAD
    if _SDK_BULKHEAD is None:
        with _SDK_INIT_LOCK:
            if _SDK_BULKHEAD is None:
                import bulkman

                workers = int(os.environ.get("AGENTBUS_SDK_MAX_CONCURRENT", "8"))
                bulkhead = bulkman.BulkheadThreading(
                    bulkman.BulkheadConfig(
                        name="agentbus-sdk",
                        max_concurrent_calls=workers,
                        max_queue_size=int(os.environ.get("AGENTBUS_SDK_MAX_QUEUE", "100")),
                        circuit_breaker_enabled=False,  # NEVER True — see module docstring
                    )
                )
                stock = bulkhead._executor
                bulkhead._executor = _daemon_executor(workers, "Bulkhead-agentbus-sdk")
                stock.shutdown(wait=False)
                _SDK_BULKHEAD = bulkhead
    return _SDK_BULKHEAD


def _sdk_safety_net() -> Any:
    """Lazy singleton — one retry+breaker policy shared across every request."""
    global _SDK_SAFETY_NET
    if _SDK_SAFETY_NET is None:
        with _SDK_INIT_LOCK:
            if _SDK_SAFETY_NET is None:
                import datetime as _dt

                import resilient_circuit as rc
                from resilient_circuit.storage import InMemoryStorage

                # resilient_circuit logs a WARNING every time a late in-flight
                # result re-marks an already-OPEN breaker ("state write refused
                # ... adopting stored state") — a no-op that would otherwise reach
                # stderr via the lastResort handler on every outage.
                logging.getLogger("resilient_circuit.circuit_breaker").setLevel(logging.ERROR)
                break_fail, close_success = _sdk_cb_limits()
                _SDK_SAFETY_NET = rc.SafetyNet(
                    policies=(
                        rc.CircuitProtectorPolicy(
                            resource_key="agentbus-sdk",
                            storage=InMemoryStorage(),
                            failure_limit=_cb_window(break_fail),
                            success_limit=_cb_window(close_success),
                            cooldown=_dt.timedelta(
                                seconds=int(os.environ.get("AGENTBUS_SDK_CB_COOLDOWN", "30"))
                            ),
                            should_handle=_breaker_should_handle,
                        ),
                        rc.RetryWithBackoffPolicy(
                            max_retries=int(os.environ.get("AGENTBUS_SDK_MAX_RETRIES", "3")),
                            backoff=_cancellable_backoff(
                                min_delay=_dt.timedelta(milliseconds=500),
                                max_delay=_dt.timedelta(seconds=8),
                                factor=2,
                                jitter=0.2,
                            ),
                            should_handle=_is_transient_sdk_error,
                        ),
                    )
                )
    return _SDK_SAFETY_NET


def _abandon_inflight_on_exit() -> None:
    """Interpreter shutdown: abandon every sequence so the executor join is short."""
    with _INFLIGHT_LOCK:
        pending = list(_INFLIGHT)
    for flag in pending:
        flag.set()
    for client in list(_SYNC_CLIENTS):
        with contextlib.suppress(Exception):
            client.close()
    bulkhead = _SDK_BULKHEAD
    if bulkhead is not None:
        with contextlib.suppress(Exception):
            bulkhead._executor.shutdown(wait=False, cancel_futures=True)


# threading's own atexit list runs BEFORE concurrent.futures joins its workers and
# before the regular atexit handlers; later registrations run first. Private but
# stable since 3.9; fall back to atexit where it is missing.
try:
    threading._register_atexit(_abandon_inflight_on_exit)  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback for unusual interpreters
    atexit.register(_abandon_inflight_on_exit)


def _run_with_resilience(fn: Any, timeout: float | None = None, *, idempotent: bool = True) -> Any:
    """Run a sync callable through the retry/breaker/bulkhead stack.

    Every library-specific outcome is translated at this boundary so callers only
    ever see AgentBusError subclasses:

    - retries exhausted         -> the ORIGINAL error (TransportError, 503, ...)
    - breaker OPEN              -> TransportError naming the breaker (issuedb #25)
    - outer deadline fired      -> TransportError (CFT preserved as __cause__), and
                                   the sequence is ABANDONED (issuedb #26)
    - bulkhead full             -> TransportError naming the knobs
    """
    import bulkman
    from resilient_circuit.exceptions import (
        ProtectedCallError,
        ProtectionException,
        RetryLimitReached,
    )

    cancel = threading.Event()
    with _INFLIGHT_LOCK:
        _INFLIGHT.add(cancel)

    def _attempt() -> Any:
        if cancel.is_set():
            raise _Abandoned("the caller's deadline already fired; not issuing another attempt")
        if idempotent:
            return fn()
        # #45: a 5xx means the server failed, not that it did nothing. Wrap it
        # so the retry classifier refuses it; the breaker still counts it, and
        # the ORIGINAL error is what the caller sees (__cause__ is unwrapped by
        # _root_cause at the boundary).
        try:
            return fn()
        except AgentBusError as exc:
            if 500 <= exc.status <= 599:
                raise _NonIdempotent(exc) from exc
            raise

    guarded = _sdk_safety_net()(_attempt)

    def _wrapped() -> Any:
        _CURRENT_CANCEL.flag = cancel
        try:
            return guarded()
        except _NonIdempotent as exc:
            # #45: the marker exists only to stop the RETRY classifier. The
            # caller must still see the real error — a wrapper leaking out
            # would turn a plain 500 into an exception type nobody handles.
            raise exc.original from None
        except RetryLimitReached as exc:
            cause = _root_cause(exc)
            if cause is not exc:
                raise cause  # noqa: B904 - deliberate: propagate the original
            raise TransportError("agentbus SDK: retries exhausted") from exc
        except ProtectedCallError as exc:
            raise TransportError(
                "agentbus SDK circuit breaker is OPEN: recent calls to the bus failed, so the "
                "client is failing fast for the cooldown (AGENTBUS_SDK_CB_COOLDOWN, default 30s) "
                "instead of hammering it. Retry after the cooldown, or set "
                "AGENTBUS_SDK_RESILIENCE=0 to bypass the resilience layer."
            ) from exc
        except ProtectionException as exc:
            protection_cause = exc.__cause__ or exc.__context__
            if protection_cause is not None:
                raise protection_cause  # noqa: B904 - deliberate: propagate the original
            raise TransportError(
                f"agentbus SDK resilience layer refused the call ({type(exc).__name__})"
            ) from exc
        finally:
            _CURRENT_CANCEL.flag = None
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(cancel)

    try:
        future = _sdk_bulkhead().execute(_wrapped)
    except bulkman.BulkheadFullError as exc:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(cancel)
        raise TransportError(
            f"agentbus SDK bulkhead full ({exc}); raise AGENTBUS_SDK_MAX_CONCURRENT "
            "or AGENTBUS_SDK_MAX_QUEUE if this is a legitimate high-fan-out caller."
        ) from exc
    try:
        result = future.result(timeout=timeout)
    except _ConcurrentFuturesTimeout as exc:
        # The caller's budget is spent. Abandon the sequence (the next attempt raises
        # _Abandoned, the in-flight one is already bounded by the remaining budget) and
        # translate at the boundary: on Python 3.10 CFT is NOT an OSError, so every
        # downstream `except (AgentBusError, OSError, httpx.HTTPError)` guard missed it
        # and the watcher died during the exact outage its reconnect loop existed for.
        cancel.set()
        with contextlib.suppress(Exception):
            future.cancel()
        raise TransportError(
            f"agentbus SDK call did not complete within {timeout}s "
            f"({type(exc).__name__} — likely a transient network stall). "
            "The reconnect loop treats this as retryable."
        ) from exc
    if result.success:
        return result.result
    assert result.error is not None
    # bulkman wraps every non-BulkheadError as BulkheadError(...) with __cause__ set
    # to the original; unwrap so callers see the typed error they branch on.
    err = result.error
    if isinstance(err, bulkman.BulkheadError) and err.__cause__ is not None:
        raise err.__cause__
    raise err


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


def _async_circuit_breaker() -> _AsyncCircuitBreaker:
    """Lazy per-process singleton — one async breaker for every client."""
    global _ASYNC_CIRCUIT_BREAKER
    if _ASYNC_CIRCUIT_BREAKER is None:
        with _SDK_INIT_LOCK:
            if _ASYNC_CIRCUIT_BREAKER is None:
                fail_n, succ_n = _sdk_cb_limits()
                _ASYNC_CIRCUIT_BREAKER = _AsyncCircuitBreaker(
                    failure_limit=fail_n,
                    success_limit=succ_n,
                    cooldown=float(os.environ.get("AGENTBUS_SDK_CB_COOLDOWN", "30")),
                )
    return _ASYNC_CIRCUIT_BREAKER
