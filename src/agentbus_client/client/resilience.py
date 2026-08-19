
"""Typed sync and async clients for the AgentBus API."""
from __future__ import annotations

import base64
import concurrent.futures as _cf
import logging
import mimetypes
import os
import time

from .models import _max_attachment_bytes, _server_max_attachment_bytes

_ConcurrentFuturesTimeout = _cf.TimeoutError
from collections.abc import Sequence
from typing import Any

import httpx

from .. import sealing

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://agentbus.rodmena.co.uk"


from .errors import (
    AgentBusError,
    ServiceUnavailable,
    TransportError,
)

# ------------------------------------------------------------------ resilience
#
# REG-7 (round-3 audit): the SDK is the largest surface every caller touches,
# and it was the one place in the codebase ignoring the house resilience
# contract — bare httpx calls, no retry, no breaker, no bulkhead. A bus rolling
# deploy (30-60s of 503s) turned every send/reply/inbox into a TransportError,
# and N callers each rolled their own retry loop that hammered the recovering
# bus.
#
# The design puts three layers around every SDK request, in this order (inside-out):
#
#     bulkman  <-  resilient_circuit  <-  actual httpx call
#
# - resilient_circuit.SafetyNet(RetryWithBackoff + CircuitProtector) sits closest
#   to the call: it decides "was this attempt transient? retry" and "have too
#   many recent attempts failed? open the breaker".
# - bulkman.BulkheadThreading sits OUTSIDE the retry, so a whole
#   send-with-3-retries counts as ONE concurrency slot and finishes together —
#   otherwise N failing callers each retrying 3x would multiply the load.
# - bulkman's OWN circuit breaker is ALWAYS OFF (`circuit_breaker_enabled=False`)
#   per the house rule: resilient_circuit is the single breaker authority in the
#   codebase, and two breakers on one path is worse than one.
#
# Everything is lazy-instantiated at first request so importing agentbus_client
# does not pay the wiring cost, and singleton at the module level so all
# AgentBus instances (and long-lived processes like the watcher) share one
# breaker state and one concurrency cap.

_SDK_BULKHEAD: Any = None
_SDK_SAFETY_NET: Any = None


def _is_transient_sdk_error(exc: BaseException) -> bool:
    """Classify what the resilience layer should retry / count for the breaker.

    Transport failures and 5xx (ServiceUnavailable) are transient — retry them.
    Every other typed AgentBusError is DEFINITIVE (401 revoked, 403 forbidden,
    404 not found, 422 malformed, 429 quota/rate) and MUST pass through
    unchanged; retrying them wastes work and can make the underlying state
    worse. Also: httpx.HTTPError catches network-level failures before the SDK
    can wrap them as TransportError, so it counts as transient too.
    """
    if isinstance(exc, (TransportError, ServiceUnavailable)):
        return True
    return isinstance(exc, httpx.HTTPError)


def _sdk_bulkhead() -> Any:
    """Lazy singleton — one concurrency lane for the whole SDK per process.

    circuit_breaker_enabled is HARD-CODED False (Farshid's explicit instruction,
    round-3 audit note): bulkman is the concurrency lane, resilient_circuit is
    the breaker. Two breakers on one call path is worse than one.
    """
    global _SDK_BULKHEAD
    if _SDK_BULKHEAD is None:
        import bulkman

        _SDK_BULKHEAD = bulkman.BulkheadThreading(
            bulkman.BulkheadConfig(
                name="agentbus-sdk",
                max_concurrent_calls=int(os.environ.get("AGENTBUS_SDK_MAX_CONCURRENT", "8")),
                max_queue_size=int(os.environ.get("AGENTBUS_SDK_MAX_QUEUE", "100")),
                # NEVER True. The single breaker authority in the codebase is
                # resilient_circuit; bulkman here is a concurrency isolator only.
                circuit_breaker_enabled=False,
            )
        )
    return _SDK_BULKHEAD


def _sdk_safety_net() -> Any:
    """Lazy singleton — one retry+breaker policy shared across every request.

    Widened breaker (5/5 fail, 2/2 success) and a small exponential retry
    budget. The `should_handle` classifier ONLY matches transient errors, so a
    401 or 404 falls through immediately — retrying a 401 hammers the bus with
    a credential that will never work and turns one clear failure into many.
    """
    global _SDK_SAFETY_NET
    if _SDK_SAFETY_NET is None:
        import datetime as _dt
        from fractions import Fraction

        import resilient_circuit as rc
        from resilient_circuit.storage import InMemoryStorage

        _SDK_SAFETY_NET = rc.SafetyNet(
            policies=(
                rc.CircuitProtectorPolicy(
                    resource_key="agentbus-sdk",
                    storage=InMemoryStorage(),
                    failure_limit=Fraction(5, 5),  # 5 of last 5 attempts failed
                    success_limit=Fraction(2, 2),  # 2 clean attempts to close
                    cooldown=_dt.timedelta(
                        seconds=int(os.environ.get("AGENTBUS_SDK_CB_COOLDOWN", "30"))
                    ),
                    should_handle=_is_transient_sdk_error,
                ),
                rc.RetryWithBackoffPolicy(
                    max_retries=int(os.environ.get("AGENTBUS_SDK_MAX_RETRIES", "3")),
                    backoff=rc.ExponentialDelay(
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


def _run_with_resilience(fn: Any, timeout: float | None = None) -> Any:
    """Run a sync callable through the retry/breaker/bulkhead stack.

    The order matters — see the module comment. RetryWithBackoff wraps the raw
    fn first (retries within one bulkhead slot); CircuitProtector wraps that
    (breaker sees post-retry outcomes); the bulkhead is outermost (one
    concurrency slot for the whole retry sequence).

    A ProtectionException from resilient_circuit (breaker open, retries
    exhausted) is unwrapped into its cause so callers still see a
    TransportError or ServiceUnavailable, not a library-specific error type.

    bulkman.execute returns a Future[ExecutionResult]; we block on .result()
    then translate: success -> the value, failure -> re-raise the original error.
    """
    import bulkman
    from resilient_circuit.exceptions import ProtectionException

    guarded = _sdk_safety_net()(fn)

    def _wrapped() -> Any:
        try:
            return guarded()
        except ProtectionException as exc:
            # Retries exhausted or breaker open — unwrap to the ORIGINAL error
            # so a caller catching TransportError still catches this. `__cause__`
            # is set by RetryWithBackoffPolicy when it gives up.
            cause = exc.__cause__ or exc.__context__
            if cause is not None:
                raise cause  # noqa: B904 - deliberate: propagate the original
            raise

    try:
        future = _sdk_bulkhead().execute(_wrapped)
        result = future.result(timeout=timeout)
    except bulkman.BulkheadFullError as exc:
        # Turn a bulkhead refusal into a TransportError with clear text — a
        # caller waiting for a slot forever is worse than a fast failure they
        # can retry against.
        raise TransportError(
            f"agentbus SDK bulkhead full ({exc}); raise AGENTBUS_SDK_MAX_CONCURRENT "
            "or AGENTBUS_SDK_MAX_QUEUE if this is a legitimate high-fan-out caller."
        ) from exc
    except _ConcurrentFuturesTimeout as exc:
        # SEV-1 (macbook-admin-bd8e86 thread 01M08ZBXDD8PQ9J70MM4VDBZR0):
        # future.result(timeout=timeout) raises concurrent.futures._base.TimeoutError
        # when the SDK call takes longer than `timeout` — which is exactly what
        # happens during a network outage. On Python 3.10.x that class does
        # NOT subclass OSError (it did only in 3.11+ where CFT became an alias
        # of the builtin TimeoutError). Every downstream `except` guard in
        # watch.py / cli.py / onboarding.py catches OSError | httpx.HTTPError
        # | AgentBusError | ... — none of them catch CFT on 3.10, so it
        # escaped the whole recovery stack and killed the watcher process
        # during the exact condition the reconnect loop existed to survive.
        #
        # Fix: translate at the BOUNDARY into TransportError, with the
        # original set as __cause__. Now every existing guard catches it on
        # every Python version, at every call site, including ones nobody
        # audited yet. Closes the whole class rather than the one traceback.
        raise TransportError(
            f"agentbus SDK call did not complete within {timeout}s "
            f"({type(exc).__name__} — likely a transient network stall). "
            "The reconnect loop treats this as retryable."
        ) from exc
    if result.success:
        return result.result
    assert result.error is not None
    # bulkman wraps every non-BulkheadError as `BulkheadError(f"Execution
    # failed: {e}")` and sets `__cause__` to the original — see
    # bulkman/threading.py's _run(). Unwrap it so a caller who catches
    # AuthError / QuotaExceeded / TransportError still sees the typed error,
    # not a library-specific wrapper that erases the classification a caller
    # is trying to branch on.
    err = result.error
    if isinstance(err, bulkman.BulkheadError) and err.__cause__ is not None:
        raise err.__cause__
    raise err


def _encode_attachments(paths: Sequence[str] | None) -> list[dict[str, str]]:
    """Read files and declare the type the bytes actually are.

    The server sniffs content types before egress because mail-api rejects a
    mismatch between the declared type and the sniffed one.

    REG-6 (round-3 audit): every file is size-checked BEFORE it is opened, and
    a file over AGENTBUS_MAX_ATTACHMENT_BYTES (default 50 MB) is refused with
    an AgentBusError — never opened, never base64-encoded, never buffered.
    Peak memory used to be ~4-5x file size (raw + base64 + JSON + httpx buffer);
    an unbounded loop over 25 x 10 MB let a forward peak at ~2 GB. The cap
    reads at the OS level (os.stat) so the refusal costs nothing.
    """
    limit = _max_attachment_bytes()
    server_limit = _server_max_attachment_bytes()
    payload = []
    for path in paths or []:
        try:
            size = os.stat(path).st_size
        except OSError as exc:
            raise AgentBusError(f"cannot read attachment '{path}': {exc}") from exc
        # F7 (issuedb #4): SERVER-CAP CHECK FIRST. This is the wall the user
        # actually hits, and it fires with the exact 10 MiB number the docs
        # promise — no confusing "50 MB client cap" for a file the server was
        # never going to accept. Costs one os.stat per attachment.
        if size > server_limit:
            raise AgentBusError(
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"AgentBus server rejects attachments over {server_limit:,} bytes "
                f"(~{server_limit // (1024 * 1024)} MiB). Failing fast here — "
                "the client would otherwise upload the whole file and wait for "
                "the server to return 413. Split the file, or set "
                "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES if your server was "
                "reconfigured with a higher ceiling."
            )
        if size > limit:
            raise AgentBusError(
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"client cap is {limit:,} bytes (~{limit // (1024 * 1024)} MB). "
                "The client buffers the whole file in RAM, then again as base64, "
                "then again in the JSON body and the HTTP buffer, so a large "
                "attachment can OOM the sending host well before the server sees "
                "the request. Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                "has the RAM budget, or split the file. A streaming upload API "
                "is planned."
            )
        with open(path, "rb") as handle:
            data = handle.read()
        guessed, _ = mimetypes.guess_type(path)
        payload.append(
            {
                "filename": os.path.basename(path),
                "content_base64": base64.b64encode(data).decode(),
                "content_type": guessed or "application/octet-stream",
            }
        )
    return payload


def _key_from_disk(agent: str | None) -> str:
    """The credential this client already wrote, read back from where it put it.

    THE BUG THIS FIXES. `__init__` resolved the key from an explicit argument or
    `AGENTBUS_API_KEY` and NOWHERE ELSE — it never read a file. So `agentbus
    setup`, whose sealing step builds `AgentBus(base_url=..., agent=name)` with
    no key, raised "no API key" on a machine that had just been signed in
    seconds earlier in the same command. On an encrypted workspace that left the
    agent with NO published sealing key: registration succeeded, the bound key
    was minted, MCP was wired, the summary ended green, and the one line that
    makes an encrypted workspace encrypted had silently failed.

    Worse, the error text told the operator to save the key to
    `~/.config/agentbus/operator.env` — which `signin` had ALREADY done, and
    which this code could not read. Advice that names the file it ignores is
    how somebody spends an hour proving their key is fine.

    ORDER IS LEAST-PRIVILEGE FIRST. The agent's own bound key is preferred
    because the operations this fallback exists to serve are "self" operations
    (publishing your OWN pubkey is authorised as self — see
    routes_agents.register_public_key, where publishing FOR a peer is the one
    move the design exists to prevent). The unbound operator credential is the
    fallback, not the default, so a routine call does not reach for the most
    powerful key on the box.
    """
    # SEV-1-B (#234): when an agent is NAMED, only that agent's own bound key is
    # eligible. Falling through to operator.env on `AgentBus(agent="peer")` when
    # keys/peer.env is missing was a silent identity escalation — the operator
    # key is workspace-wide, so a script that constructed AgentBus with an
    # arbitrary agent name acted as that peer with operator authority, and the
    # server saw a signed, attested send from that peer. The CLI's
    # resolve_credentials already refused this ("--agent is an ASSERTION OF
    # IDENTITY, not 'go find that agent's credential'"); the SDK constructor
    # bypassed it. Same rule here: an unspecified `agent` legitimately falls back
    # to operator.env (the pre-agent-binding acting mode), a specified one does
    # NOT. A missing per-agent file raises rather than substituting, so the
    # failure surfaces as a NamedIdentityKeyMissing the caller can catch.
    config = os.path.join(os.path.expanduser("~"), ".config", "agentbus")

    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    entry = raw.strip()
                    if not entry or entry.startswith("#"):
                        continue
                    # `export KEY=value`, the shape onboarding writes.
                    entry = entry.removeprefix("export ").strip()
                    name, sep, value = entry.partition("=")
                    if sep and name.strip() == "AGENTBUS_API_KEY":
                        value = value.strip().strip("'\"")
                        if value:
                            return value
        except OSError:
            return ""
        return ""

    if agent:
        # REG-8 (round-3) + REG-8b (round-3.5 sweep): PATH TRAVERSAL / identity
        # escalation. Round-3 sanitized this one call site; macbook's re-audit
        # (round-3.5) found FOUR MORE sibling call sites with the same
        # unsanitized keys/<agent>.env pattern (cli.py `_key_for_agent`, join
        # --name, setup, service; hooks/claude_code.py `_adopt_credential_for`).
        # ALL now route through sealing.bound_env_filename so the same
        # sanitizer decides the filename everywhere — separators to '_',
        # '..' to '_' — and nothing can escape keys/ into operator.env.
        return _read(os.path.join(config, "keys", sealing.bound_env_filename(agent)))
    return _read(os.path.join(config, "operator.env"))


class _AsyncCircuitBreaker:
    """Minimal per-process breaker for the async client (REG-7 follow-up).

    resilient_circuit — the sync breaker — is sync-only, so the async path
    hand-rolls one. Semantics mirror the sync CircuitProtector *as it is
    paired with RetryWithBackoff*: the breaker sees POST-RETRY outcomes, so a
    whole failing retry-sequence counts as ONE failure, not N attempts. After
    `failure_limit` consecutive failing sequences the breaker opens for
    `cooldown` seconds; while open, calls fail fast (no retry, no bulkhead
    slot) instead of hammering a bus that is demonstrably down. When the
    cooldown lapses the breaker is half-open and allows a probe; a failing
    probe re-opens it IMMEDIATELY (one failure, not `failure_limit`), while
    `success_limit` clean probes close it fully — the standard CQ cycle, so a
    sustained outage costs one probe burst per cooldown, not N.

    State is only monotonic timestamps and counters — no asyncio primitive —
    so it has no event-loop affinity and can be a module-level singleton
    shared across every AsyncAgentBus and every loop in the process (the same
    way the sync breaker is module-level). Mutations are non-awaiting, so
    within single-threaded async they are atomic per coroutine step.
    """

    def __init__(self, failure_limit: int = 5, success_limit: int = 2, cooldown: float = 30.0):
        self.failure_limit = failure_limit
        self.success_limit = success_limit
        self.cooldown = cooldown
        self._open_until = 0.0
        self._half_open = False
        self._failures = 0
        self._successes = 0
        self._last_error: BaseException | None = None

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def last_error(self) -> BaseException | None:
        return self._last_error

    def on_success(self) -> None:
        self._failures = 0
        self._successes += 1
        if self._successes >= self.success_limit:
            self._open_until = 0.0
            self._half_open = False
            self._successes = 0

    def on_failure(self, exc: BaseException) -> None:
        self._successes = 0
        self._last_error = exc
        if self._half_open:
            # A failed half-open probe re-opens immediately — do not require
            # failure_limit more failures to admit the bus is still down.
            self._open_until = time.monotonic() + self.cooldown
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= self.failure_limit:
            self._open_until = time.monotonic() + self.cooldown
            self._half_open = True
            self._failures = 0


_ASYNC_CIRCUIT_BREAKER: _AsyncCircuitBreaker | None = None


def _async_circuit_breaker() -> _AsyncCircuitBreaker:
    """Lazy per-process singleton — one async breaker for every client.

    Mirrors `_sdk_safety_net`: one breaker state means one down bus fails fast
    for ALL async clients in the process, not once per instance.
    """
    global _ASYNC_CIRCUIT_BREAKER
    if _ASYNC_CIRCUIT_BREAKER is None:
        _ASYNC_CIRCUIT_BREAKER = _AsyncCircuitBreaker(
            failure_limit=int(os.environ.get("AGENTBUS_SDK_CB_FAILURE_LIMIT", "5")),
            success_limit=int(os.environ.get("AGENTBUS_SDK_CB_SUCCESS_LIMIT", "2")),
            cooldown=float(os.environ.get("AGENTBUS_SDK_CB_COOLDOWN", "30")),
        )
    return _ASYNC_CIRCUIT_BREAKER

