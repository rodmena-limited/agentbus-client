"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import mimetypes
import os
import sys
import threading as _threading
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import sealing

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://agentbus.rodmena.co.uk"


# ------------------------------------------------------------------ errors


class AgentBusError(Exception):
    """Base error. `code` mirrors the API's stable error code."""

    def __init__(
        self,
        detail: str,
        *,
        code: str = "error",
        status: int = 0,
        body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status
        self.body = body or {}


class AuthError(AgentBusError):
    """401 — the key is missing, malformed, or revoked."""


class PermissionError_(AgentBusError):
    """403 — this key may not do that, or may not act as that agent."""


class NotFoundError(AgentBusError):
    """404 — unknown, or belonging to another workspace."""


class ValidationError(AgentBusError):
    """422 / 413 — fix the request."""


class QuotaExceeded(AgentBusError):
    """429 quota_exceeded. `retry_after` and `reset_at` are always present."""

    def __init__(self, detail: str, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.retry_after: int | None = self.body.get("retry_after")
        self.reset_at: str | None = self.body.get("reset_at")
        self.blocking_policy: dict[str, Any] = self.body.get("blocking_policy") or {}


class RateLimited(QuotaExceeded):
    """429 rate_limited — a burst limit, not a daily budget."""


class ServiceUnavailable(AgentBusError):
    """503 — one of our dependencies could not answer. Never a verdict about you."""

    def __init__(self, detail: str, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.retry_after: int | None = self.body.get("retry_after")


class TransportError(AgentBusError):
    """The request never got an answer."""


_ERRORS = {
    401: AuthError,
    403: PermissionError_,
    404: NotFoundError,
    409: ValidationError,
    413: ValidationError,
    422: ValidationError,
    503: ServiceUnavailable,
}


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("code", "error")
    detail = body.get("detail") or body.get("title") or response.text[:300]
    if response.status_code == 429:
        cls: type[AgentBusError] = RateLimited if code == "rate_limited" else QuotaExceeded
    else:
        cls = _ERRORS.get(response.status_code, AgentBusError)
    raise cls(detail, code=code, status=response.status_code, body=body)


# ------------------------------------------------------------------ models


@dataclass
class Delivery:
    """One message as delivered to one agent."""

    delivery_id: str
    seq: int
    subject: str
    sender: str
    state: str
    thread_id: str
    message_id: str
    labels: list[str] = field(default_factory=list)
    attachment_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Delivery:
        # SEV-4 (#234): agent_seq is a MONOTONIC POSITIVE integer per agent;
        # `.get("agent_seq") or 0` used to collapse 0 and None, which happens to
        # be harmless because seq starts at 1 — but the pattern was wrong for a
        # value where "missing" and "zero" are semantically different, and would
        # bite the day a server returned 0 for a legitimate first delivery.
        # Explicit None handling keeps the invariant readable.
        raw_seq = data.get("agent_seq")
        seq = int(raw_seq) if raw_seq is not None else 0
        return cls(
            delivery_id=data["delivery_id"],
            seq=seq,
            subject=data.get("subject") or "",
            sender=data.get("sender_display") or data.get("sender_address") or "",
            state=data.get("state") or "",
            thread_id=data.get("thread_id") or "",
            message_id=data.get("message_id") or "",
            labels=list(data.get("labels") or []),
            attachment_count=data.get("attachment_count") or 0,
            raw=data,
        )


#: REG-6 (round-3 audit): per-attachment size ceiling — the quick fix.
#: _encode_attachments buffers the raw file + its base64 form + the JSON body
#: + httpx's own copy = peak ~4-5x file size, doubled again on encrypted send
#: via _apply_seal. A 500 MB video used to OOM small VMs / containers before
#: the server was even reached. This cap FAILS FAST at the boundary with a
#: clear error, so a caller sees the size wall as a refusal rather than an
#: OOM traceback. The real fix (streaming base64/multipart upload) needs a
#: server change and is a separate follow-up. Override via env for genuine
#: large-file needs on hosts with the RAM to spend.
_DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB


def _max_attachment_bytes() -> int:
    raw = os.environ.get("AGENTBUS_MAX_ATTACHMENT_BYTES")
    if not raw:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    return v if v > 0 else _DEFAULT_MAX_ATTACHMENT_BYTES


#: F7 (issuedb #4): the SERVER's per-attachment ceiling, applied BEFORE any
#: sealing or upload happens. Distinct from the RAM-safety cap above.
#:
#: Without this check the client streams the whole file through sealing and
#: the network before the server returns 413. A peer's repro of an 11 MiB
#: attachment took 53.7 s of wall time to hit the 10 MiB server ceiling —
#: pure waste on both sides, and the caller learns nothing until the very end.
#:
#: The server does not yet publish this cap machine-readably (backend #249
#: opens GET /v1/limits for it). When that lands, replace this constant with a
#: single cached fetch at startup and delete the env override — the server is
#: the authority. For now: hardcoded to match the documented 10 MiB value,
#: with an env override so an operator whose own server was reconfigured up
#: is not blocked by a stale client.
_DEFAULT_SERVER_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB, per docs


def _server_max_attachment_bytes() -> int:
    raw = os.environ.get("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES")
    if not raw:
        return _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES
    return v if v > 0 else _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES


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


# ------------------------------------------------------------------ clients


class _Base:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        agent: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # `agent` is read before base_url/self.agent below because the on-disk
        # lookup is agent-scoped: the bound key lives at keys/<agent>.env.
        #
        # WHEN AGENT IS EXPLICITLY NAMED, PREFER THE DISK-BOUND KEY OVER $ENV.
        # Root-caused by agentbus-8dc08d (thread 01M08QS3M10M49WKT8WVX3P2P7):
        # a dependency (`resilient_circuit/storage.py`) calls `load_dotenv()`
        # at import time; find_dotenv walks UP from that module's file inside
        # .venv/lib/python3.13/site-packages/, so any parent directory's
        # `.env` containing `AGENTBUS_API_KEY=<some other key>` STOMPS
        # os.environ. If that stomped key is bound to a deleted workspace,
        # every downstream call sees WorkspaceDeleted — even though the
        # correct freshly-minted bound key is sitting on disk at
        # ~/.config/agentbus/keys/<agent>.env.
        #
        # When the caller NAMED an agent, they want THAT agent's credential.
        # Disk wins for that case. Env still wins in the unnamed-agent path
        # (the operator CLI: `agentbus signin`, `agentbus register`, etc.).
        # Every legitimate agent-named use case ships the bound key on disk.
        disk_key_for_named_agent = _key_from_disk(agent) if agent else ""
        self.api_key = (
            api_key
            or disk_key_for_named_agent
            or os.environ.get("AGENTBUS_API_KEY", "")
            or _key_from_disk(agent or os.environ.get("AGENTBUS_AGENT"))
        )
        if not self.api_key:
            # NAME THE KEY, WHERE TO GET IT, AND WHERE TO PUT IT.
            #
            # "set AGENTBUS_API_KEY" told an operator nothing he did not already
            # know. He had a key from the dashboard, it was the wrong SHAPE, and
            # nothing here or in the UI connected the two. The dashboard offers
            # read/send/full/admin and has never had an "operator" option, because
            # `operator` is not a key type at all — it is the FILENAME this client
            # reads. Nobody could have deduced that.
            resolved_agent = agent or os.environ.get("AGENTBUS_AGENT")
            if resolved_agent:
                # SEV-1-B: an agent-scoped construction NEVER borrows operator.env;
                # a missing bound key is a real "no credential for this identity"
                # error, not a reason to escalate to the workspace-wide key.
                raise AuthError(
                    f"no API key for agent '{resolved_agent}'. Looked for one in: the "
                    f"call itself, $AGENTBUS_API_KEY, and ~/.config/agentbus/keys/"
                    f"{resolved_agent}.env — all empty. This client REFUSES to fall "
                    "back to ~/.config/agentbus/operator.env when an agent is named, "
                    "because that would let a caller act as an arbitrary peer with "
                    "operator authority. Mint a bound key for this agent (dashboard: "
                    "Keys -> scope 'send', agents=[<name>]) and drop it in that "
                    "keys/ path, or construct AgentBus() without `agent=` to use the "
                    "operator credential explicitly."
                )
            raise AuthError(
                "no API key. Looked for one in: the call itself, $AGENTBUS_API_KEY, "
                "and ~/.config/agentbus/operator.env — all empty. Run `agentbus signin "
                "<api-key>` to create the operator credential. Registering a NEW "
                "agent needs a key with scope 'full' and NO agent binding — create "
                "one in the dashboard (Keys -> scope 'full', leave agents empty). "
                "NOTE: an agent-BOUND key cannot register a new agent, however high "
                "its scope — binding, not scope, is what blocks it."
            )
        self.base_url = (
            base_url or os.environ.get("AGENTBUS_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.agent = agent or os.environ.get("AGENTBUS_AGENT")
        self.timeout = timeout
        self._pending_challenge: str | None = None
        # SEV-2-B (#234): any caller that shares one AgentBus across a worker
        # pool (thread pool, or asyncio.to_thread from an AsyncAgentBus) races on
        # _pending_challenge — request A captures, request B echoes and clears
        # before A's next call, presence reads as reachable-not-responsive for
        # windows when it was in fact answering. threading.Lock protects the
        # read-then-clear on _headers and the write on _capture_challenge; the
        # critical section is a single dict update and holds the lock for microseconds.
        self._challenge_lock = _threading.Lock()

    def _headers(
        self,
        agent: str | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        acting = agent or self.agent
        if acting:
            headers["X-AgentBus-Agent"] = acting
        if idempotent or idempotency_key:
            # SEV-2-D (#234): the caller may supply a STABLE key so a retry after
            # TransportError deduplicates on the server side. If omitted we mint
            # one per _request call — safe when the caller does not retry, and
            # documented so a caller that DOES retry knows to pass its own.
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        # Answer any outstanding liveness challenge on the next call the caller
        # makes anyway. Handled here so an agent never has to implement the
        # protocol: being 'responsive' should be the default for anyone using
        # the SDK, not a feature you opt into and forget.
        # SEV-2-B (#234): read-and-clear under the lock so two threads sharing one
        # AgentBus can't both echo the same challenge (or lose it).
        with self._challenge_lock:
            if self._pending_challenge:
                headers["X-AgentBus-Pong"] = self._pending_challenge
                self._pending_challenge = None
        return headers

    def _capture_challenge(self, payload: Any) -> None:
        if isinstance(payload, dict):
            challenge = payload.get("liveness_challenge")
            if challenge:
                # SEV-2-B: overwrite under the lock. A more recent challenge
                # supersedes an older one, but the swap must be atomic w.r.t.
                # _headers's read-and-clear.
                with self._challenge_lock:
                    self._pending_challenge = challenge

    def _sign_if_possible(
        self,
        payload: dict[str, Any],
        agent: str | None,
        resolved: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sign when this machine holds a signing key, otherwise send unsigned.

        SILENT WHEN THERE IS NO KEY, and that is the spec: signing is opt-in and
        unsigned mail stays first-class. An error here would make `agentbus
        keys sign` a prerequisite for sending, which is precisely the
        mandatory-signing bus #173 rejects.

        #220: ON `_Base`, so all four surfaces share it. It used to live on the
        sync client, and three of the four send paths — AgentBus.reply,
        AsyncAgentBus.send, AsyncAgentBus.reply — never signed at all. The
        FreeBSD agent found it by reading the installed client and diffing the
        two methods line by line.

        `resolved` IS THE SERVER'S ANSWER ABOUT WHAT IT WILL STORE, and it wins
        where it is present. A reply's recipients and "Re: " subject are derived
        server-side; the signature covers the stored values, so signing over the
        request payload would produce a signature that verifies as INVALID —
        which every read surface labels as tampering. An unsigned reply is
        merely unattested; a wrongly-signed one accuses its own sender.
        """
        from . import _signing, sealing

        # #220: THIS AGENT's key, not the machine's. A shared signing key made
        # two agents on one box indistinguishable to the bus.
        private = sealing.load_signing_key(agent or self.agent)
        if private is None:
            return payload

        # agentbus-sig-v1 only covers the text body. If a message has HTML, attachments,
        # or a structured payload, signing it would create a signature that doesn't cover
        # the entire message contents (a cryptographic bypass risk).
        # We degrade to an unsigned message rather than failing.
        if payload.get("html") or payload.get("attachments") or payload.get("payload") is not None:
            # F9 (issuedb #3): write directly to STDERR, not via logging. The
            # SDK does not know whether the CLI caller is emitting --json to
            # stdout; the ambient logging config could route lastResort to
            # stdout and poison a machine-readable pipe. Explicit stderr is
            # the only source of truth that survives any caller setup.
            print(
                "agentbus: message downgraded to unsigned. agentbus-sig-v1 only "
                "covers plain text, but html, attachments, or a payload was present.",
                file=sys.stderr,
            )
            return payload

        signed = dict(payload)
        over = resolved or {}
        try:
            signed["signature"] = _signing.sign(
                private,
                _signing.canonical_bytes(
                    sender=agent or self.agent or "",
                    to=list(over.get("to") or payload.get("to") or []),
                    cc=list(over.get("cc") or payload.get("cc") or []),
                    subject=over.get("subject") or payload.get("subject"),
                    priority=payload.get("priority") or "normal",
                    body=payload.get("text"),
                ),
            )
            signed["signing_key_fingerprint"] = _signing.fingerprint(
                _signing.public_from_private(private)
            )
        except Exception:
            return payload
        return signed

    def _apply_seal(self, payload: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
        """Turn a resolve answer into a sealed payload. NO I/O, so both the sync
        and async clients share it.

        #220: this used to live inside the sync client's `_seal_if_needed`, which
        is why AsyncAgentBus sealed NOTHING — every async send on an encrypted
        workspace was refused by the server, and the async surface had no way to
        comply. One rule, two surfaces, implemented once: the same shape #155 was
        written about.
        """
        from . import sealing

        if not resolved.get("encrypted"):
            return payload

        # EXTERNAL RECIPIENTS: hand it to the server unsealed and let it rule.
        #
        # #219 (operator, 2026-08-16): an encrypted workspace's bus carries no
        # mail to external addresses AT ALL, in either direction — a recipient
        # with no key would receive it in the clear, and a guarantee that ends
        # at its own boundary is not a guarantee. An earlier same-day decision
        # allowed plaintext egress; it was reversed, and this comment used to
        # still describe it.
        #
        # The client does not pre-refuse, because the rule has ONE owner. Two
        # copies of it drift, and the copy that drifts is the one nobody reads.
        if resolved.get("external"):
            return payload
        missing = resolved.get("missing_keys") or []
        if missing:
            raise AgentBusError(
                "cannot seal: these recipients have published no public key, so "
                "they could never read this message — "
                + ", ".join(missing)
                + ". They each need to run `agentbus signin` on their machine."
            )

        # EVERY key of every recipient, not one each. An agent may be on
        # several machines (AGENTBUS_DEVICE_ID is the documented way a
        # container fleet shares one identity), and sealing to only the first
        # would deliver a message half that agent's machines cannot read —
        # varying by which container happened to register last.
        keys: list[str] = []
        for entries in (resolved.get("keys") or {}).values():
            keys.extend(entry["public_key"] for entry in entries)
        _private, own_public = sealing.ensure_keypair(self.agent)
        if own_public not in keys:
            keys.append(own_public)
        if not keys:
            raise AgentBusError(
                "cannot seal: no public keys resolved for this send, so the "
                "message would be readable by nobody"
            )
        body = payload.get("text") or payload.get("html") or ""
        sealed = dict(payload)
        sealed["text"] = sealing.seal_for(body, keys)
        sealed["html"] = None
        sealed["sealed"] = True

        # ATTACHMENTS TOO. A sealed body beside a readable attachment is the
        # worst of both: it LOOKS encrypted, and the file — usually the part
        # actually worth protecting — is sitting in the clear. The spec said
        # body AND attachments from the start; only the body shipped first.
        #
        # Sealed as bytes, then re-base64'd, so the wire shape is unchanged and
        # the server stores an opaque blob exactly as it stores any other.
        # The filename is NOT sealed — it is metadata, like the subject, and
        # the same warning applies: do not put secrets in filenames.
        import base64 as _b64

        resealed = []
        for item in sealed.get("attachments") or []:
            raw = _b64.b64decode(item["content_base64"])
            armored = sealing.seal_for_bytes(raw, keys)
            resealed.append(
                {
                    **item,
                    "content_base64": _b64.b64encode(armored).decode(),
                    # The stored type is what it now IS, not what it was. A
                    # sniffing server that saw 'image/png' on age ciphertext
                    # would reject the mismatch at egress.
                    "content_type": "application/age",
                }
            )
        if resealed:
            sealed["attachments"] = resealed
        return sealed


class AgentBus(_Base):
    """Synchronous client."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # SEV-1-C (#234): cap the connection pool so a stalled bus cannot
        # back-pressure into the caller's own thread pool. Defaults chosen for a
        # sensible SDK footprint; override via env for high-fan-out services.
        limits = httpx.Limits(
            max_connections=int(os.environ.get("AGENTBUS_MAX_CONNECTIONS", "20")),
            max_keepalive_connections=int(
                os.environ.get("AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "10")
            ),
            keepalive_expiry=float(os.environ.get("AGENTBUS_KEEPALIVE_EXPIRY", "30")),
        )
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout, limits=limits)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> AgentBus:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        agent: str | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # REG-7 (round-3 audit): mint the idempotency key ONCE HERE, outside
        # any retry loop, so all attempts hit the server with the same key and
        # the vendor's dedup layer sees a retry (not two distinct writes). If
        # we minted inside _do_request the resilience layer's retries would
        # each get a fresh UUID — exactly the retry-safety hole SEV-2-D
        # closed for callers but reopened for the SDK itself.
        if (idempotent or idempotency_key) and not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        def _do_request() -> Any:
            try:
                response = self._client.request(
                    method,
                    path,
                    headers=self._headers(agent, idempotent, idempotency_key),
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise TransportError(str(exc)) from exc
            _raise_for(response)
            payload = response.json() if response.content else None
            self._capture_challenge(payload)
            return payload

        # REG-7: wrap every request in the resilience stack. Long-polls (large
        # timeout=) pass their timeout through so a bulkman queue-block does not
        # override the caller's own wait budget.
        if os.environ.get("AGENTBUS_SDK_RESILIENCE") == "0":
            # An explicit opt-out for cases where a caller has its own
            # resilience layer around the SDK and does not want doubled retries.
            return _do_request()
        call_timeout = kwargs.get("timeout") or self.timeout
        # Add generous headroom over the actual HTTP timeout so bulkman's
        # future.result(timeout=) never trips before the httpx call does; the
        # httpx timeout is the source of truth for "this request took too long".
        return _run_with_resilience(_do_request, timeout=call_timeout + 5)

    def attachment(self, delivery_id: str, index: int = 0, *, agent: str | None = None) -> bytes:
        """The RAW BYTES of one attachment on a delivery (#124).

        `send(attachments=[...])` has always existed; nothing could read one back.
        The capability lived only on the MCP surface, so an agent onboarded by the
        documented path — CLI plus plugin, MCP optional — could send binary and
        could not receive it. A single-hop send test looks perfectly healthy;
        only being a RELAY exposes the asymmetry, which is why it survived until a
        multi-hop transfer needed it.

        Keyed on DELIVERY id, matching `read()` and what `inbox` prints. The REST
        route is also exposed under message_id, which a recipient would have to go
        and look up.

        Returns bytes rather than a decoded payload: this is the one response in
        the API that is not JSON, and `_request` would try to parse it.

        REG-9 (round-3 re-audit): the response is now STREAMED via
        `_client.stream(...) + iter_bytes()`, with an accumulator that raises
        AgentBusError as soon as the running total exceeds
        AGENTBUS_MAX_ATTACHMENT_BYTES (same cap that guards the send side,
        same env override, same default 50 MB). The old code used
        `response.content` which buffered the whole body before any check
        could bail — a hostile server or a stale route could stream a
        multi-GB body into RAM before we noticed. A proper streaming decrypt
        (so plaintext and ciphertext do not coexist in RAM) needs a
        `sealing.unseal_stream` primitive that pyage can be wrapped in; the
        boundary cap here reduces the peak by 1/2 today (ciphertext-only
        during read) and covers the hostile-server case in full.
        """
        from . import sealing

        # REG-9: cap the maximum ciphertext we will buffer. The armor and age
        # framing add ~15% overhead vs the raw plaintext, so allow a small
        # headroom over the send-side cap for legitimate large-and-sealed files.
        cap = int(1.5 * _max_attachment_bytes())
        content: bytes
        try:
            with self._client.stream(
                "GET",
                f"/v1/deliveries/{delivery_id}/attachments/{index}",
                headers=self._headers(agent, False),
            ) as response:
                _raise_for(response)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > cap:
                        # Refuse before we finish reading. httpx closes the
                        # stream when we leave the `with` block.
                        raise AgentBusError(
                            f"attachment is over the {cap:,} byte cap "
                            f"({total:,} bytes received so far and still coming). "
                            "Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                            "has the RAM budget; the client holds the ciphertext "
                            "and the plaintext at the same time during unseal."
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        # UNSEAL ON THE WAY OUT, like read() does for bodies. A caller that
        # received age armor where it expected a PNG would reasonably conclude
        # the file was corrupt — and "remember to decrypt attachments too" is
        # the kind of instruction followed for about a week.
        #
        # 64, not 32: the armor header is 34 bytes, so slicing to 32 made this
        # test never fire and every sealed attachment came back as armor. The
        # probe caught it on its first run; no unit test would have, because a
        # unit test asserting "unsealed correctly" would have used a fixture I
        # sliced the same wrong way.
        if content[:64].lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
            # EVERY key this machine holds, including superseded ones: after
            # `agentbus keys rotate` the old private key is kept precisely so
            # yesterday's mail stays readable, and only trying the current one
            # would make that promise empty.
            if not sealing.load_private_keys(self.agent):
                raise AgentBusError(
                    "this attachment is sealed and this machine holds no sealing "
                    "key — run `agentbus signin` to publish one, though anything "
                    "sealed before that remains unreadable here"
                )
            try:
                return sealing.unseal_bytes_with_any(content, self.agent)
            except sealing.MalformedSealed as exc:
                # DAMAGED is not the same as NOT FOR ME, and the remedies are
                # opposites: re-fetch the file, versus find the key. Now that
                # the sealing layer distinguishes them, saying "it is not
                # corrupt" on a corrupt file would be actively misleading.
                raise AgentBusError(f"this attachment is damaged: {exc}") from exc
            except sealing.CannotDecrypt as exc:
                raise AgentBusError(
                    "this attachment was sealed to keys this machine does not "
                    "hold; it is not corrupt"
                ) from exc
        return content

    # ---------------------------------------------------------- identity

    def register(
        self,
        name: str | None = None,
        *,
        role: str | None = None,
        repo_remote: str | None = None,
        workdir: str | None = None,
        capabilities: Sequence[str] | None = None,
        labels: dict[str, str] | None = None,
        unlisted: bool = False,
        ephemeral: bool | None = None,
    ) -> dict[str, Any]:
        """Register (idempotently) and remember the name for later calls.

        PREFER `role` over `name`. With a role, identity is DERIVED from this
        machine, this checkout and this directory, so reopening the session
        recomputes the same agent — same address, same inbox, same cursor —
        with nothing to remember. A name has to be recalled correctly every
        time, and when it is not, a brand new identity is minted silently.

            bus.register(role="api-refactor")

        Everything else is discovered locally: the device id, the git remote,
        the working directory, and whether this is a throwaway CI environment.
        """
        from . import identity

        payload: dict[str, Any] = {
            "name": name,
            "capabilities": list(capabilities or []),
            "labels": labels or {},
            "unlisted": unlisted,
        }
        if role:
            env = identity.describe(workdir)
            payload.update(
                role=role,
                device_id=env["device_id"],
                workdir=env["workdir"],
                repo_remote=repo_remote or env["repo_remote"],
                ephemeral=env["ephemeral"] if ephemeral is None else ephemeral,
            )
        else:
            payload.update(repo_remote=repo_remote, ephemeral=bool(ephemeral))
        result: dict[str, Any] = self._request("POST", "/v1/agents/register", json=payload)
        self.agent = result["agent"]["name"]
        return result

    def whoami(self, agent: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", "/v1/whoami", agent=agent)
        return result

    def mint_key(
        self, *, scope: str = "send", agents: Sequence[str] | None = None, label: str | None = None
    ) -> dict[str, Any]:
        """Mint a key no stronger than this one. Minting DE-ESCALATES: `send`
        keys must be bound, and this is how `agentbus setup` gives each
        project's agent its own credential without a human ever seeing it."""
        result: dict[str, Any] = self._request(
            "POST",
            "/v1/keys",
            json={
                "scope": scope,
                "agents": list(agents) if agents else None,
                "label": label,
            },
        )
        return result

    def create_invite(self, *, role: str | None = None, ttl_seconds: int = 3600) -> dict[str, Any]:
        """Mint a ONE-TIME join token so a NEW agent can register itself.

        The counterpart to `mint_key`, and the one to reach for when the agent
        does not exist YET. `mint_key` produces a credential that keeps working;
        this produces a token that creates exactly one agent and then stops
        existing. Handing a `full` workspace key to a machine so it can create
        one agent gives that machine the ability to read every inbox in the
        workspace and mint more keys — a join token gives it neither.

        Returns {token, expires_in_seconds, role}. The token is shown once and
        is stored only as a Redis entry under its TTL, so a lost token is
        re-minted, never recovered.
        """
        # Annotated rather than returned straight through: `_request` is typed
        # Any, so a bare `return` here adds a no-any-return and the ratchet is
        # meant to fall, not rise. New code pays its own way.
        invite: dict[str, Any] = self._request(
            "POST",
            "/v1/invites",
            json={"role": role, "ttl_seconds": ttl_seconds},
        )
        return invite

    def phonebook(
        self,
        query: str | None = None,
        *,
        capability: str | None = None,
        repo_fingerprint: str | None = None,
        label: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            k: v
            for k, v in {
                "q": query,
                "capability": capability,
                "repo_fingerprint": repo_fingerprint,
            }.items()
            if v
        }
        if label:
            # #149: repeatable, ANDed server-side. httpx encodes a list value
            # as repeated query params.
            params["label"] = [label] if isinstance(label, str) else list(label)
        result: dict[str, Any] = self._request("GET", "/v1/agents", params=params)
        return result["agents"]  # type: ignore[no-any-return]

    def tag(
        self,
        set: dict[str, str] | None = None,
        remove: Sequence[str] | None = None,
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Merge-mutate an agent's tags (#149): `set` upserts, `remove` deletes.

        Tags survive re-registration; peers find them via phonebook(label=...).
        Discovery metadata only — a tag never grants permissions. Not the
        delivery mail-filing labels (that is `label()`).
        """
        target = agent or self.agent
        if not target:
            raise ValueError("tag() needs an acting agent: pass agent= or construct with one")
        result: dict[str, Any] = self._request(
            "PATCH",
            f"/v1/agents/{target}/labels",
            json={"set": set or {}, "remove": list(remove or [])},
            agent=target,
        )
        return result

    def heartbeat(self, agent: str | None = None) -> None:
        self._request("POST", f"/v1/agents/{agent or self.agent}/heartbeat")

    def retire(self, agent: str | None = None) -> None:
        self._request("POST", f"/v1/agents/{agent or self.agent}/retire")

    # ---------------------------------------------------------- messaging

    def send(
        self,
        to: Sequence[str] | str,
        subject: str = "",
        text: str | None = None,
        *,
        # #155: copied, not addressed. Cc recipients get the same delivery; the
        # difference is the intent they can read off it (to = act, cc = informed).
        cc: Sequence[str] | str | None = None,
        # #167: urgent | normal | background. Omitted = normal, so nothing
        # changes for a caller that never sets it.
        priority: str | None = None,
        html: str | None = None,
        thread_id: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        # #168: refuse rather than queue if the recipient has declared itself
        # busy. `require_responsive` asks "is anyone home"; this asks "is anyone
        # FREE" — a distinction that only matters when you would rather route
        # elsewhere than wait.
        require_available: bool = False,
        # #174: message ids this is DERIVED FROM. A claim the bus records and
        # labels as the sender's, never as its own attestation.
        derived_from: Sequence[str] | None = None,
        # #169: a structured body the room's schema validates BEFORE the message
        # is accepted, so a malformed payload is a refusal to the sender rather
        # than a surprise for every consumer.
        payload: Any = None,
        # #172: "fire_and_forget" trades durability for cost — no store, no ack,
        # no redelivery. The right choice for a heartbeat, the wrong one for
        # anything you would miss.
        guarantee: str | None = None,
        agent: str | None = None,
        # SEV-2-D (#234): a caller wrapping .send() in try/except TransportError:
        # retry MUST pass the same key across attempts so the server dedupes. If
        # omitted, the SDK mints one per _request — safe for a caller that does
        # not retry, unsafe for one that does. Named explicitly so the option is
        # discoverable and the failure mode is documented.
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        recipients = [to] if isinstance(to, str) else list(to)
        copied = [cc] if isinstance(cc, str) else list(cc or [])
        body = {
            "to": recipients,
            "cc": copied,
            "priority": priority,
            "subject": subject,
            "text": text,
            "html": html,
            "thread_id": thread_id,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
            "require_available": require_available,
            "payload": payload,
            "guarantee": guarantee,
            "derived_from": list(derived_from) if derived_from else None,
        }
        body, resolved = self._seal_if_needed(body, agent)
        # AFTER SEALING, DELIBERATELY. The canonical form hashes the STORED
        # body, so on an encrypted workspace the signature covers the
        # ciphertext: the platform can still verify (it holds those bytes), and
        # a recipient verifies before decrypting. Signing the plaintext instead
        # would make every signature on an encrypted workspace unverifiable by
        # anyone but a recipient — a state nobody reads after the first week.
        body = self._sign_if_possible(body, agent, resolved)
        return self._request(
            "POST",
            "/v1/messages",
            json=body,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    # ----------------------------------------------------------- sealing (#189)

    def _seal_if_needed(
        self,
        payload: dict[str, Any],
        agent: str | None,
        *,
        resolve_path: str = "/v1/recipients/resolve",
        resolve_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Seal the body when the workspace is encrypted, or leave it alone.

        THE SERVER IS ASKED, NOT GUESSED. `POST /v1/recipients/resolve` returns
        the concrete agents a send would reach — which only the server can know
        for `room:` and `tag:` targets — along with their public keys and,
        crucially, the names of any recipient that has published none.

        A CLIENT THAT SEALED ONLY TO THE KEYS IT FOUND would silently exclude
        whoever had none: a message that looks delivered and is unreadable by
        half its recipients, with nothing anywhere saying so. So a missing key
        is a refusal, not a warning.

        The sender's own key is always added, so an agent can read its own sent
        mail. Without it, `agentbus sent` shows you ciphertext you wrote.

        RETURNS THE RESOLVER'S ANSWER ALONGSIDE THE PAYLOAD, because the
        signature needs it: on a reply the server derives the recipients and the
        "Re: " subject, and a signature covers what the server STORES. `None`
        means the question was never answered -- no such route, or refused --
        and the caller then signs over its own payload, as it always did.
        """

        # A SERVER THAT REFUSES THE QUESTION MUST NOT COST YOU THE SEND.
        #
        # An older server does not have this route at all, and a server whose
        # scope allowlist has not caught up refuses it — which is exactly what
        # happened: 0.5.0 made resolve unconditional and a send-scope key got
        # 403 here, before it ever reached the send. The client then raised and
        # the message was never posted.
        #
        # Falling through is SAFE, not a bypass, because the server owns the
        # guarantee: `accept_message` refuses an unsealed body on an encrypted
        # workspace. So the worst case here is a clear refusal from the surface
        # that actually knows, instead of a 403 naming an endpoint the user
        # never called.
        try:
            resolved = self._request(
                "POST",
                resolve_path,
                json=resolve_body if resolve_body is not None else payload,
                agent=agent,
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload, None
            raise
        return self._apply_seal(payload, resolved), resolved

    def unseal_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Decrypt a message body in place, or mark plainly why it could not be.

        A READER THAT CANNOT DECRYPT MUST SAY SO. Returning ciphertext as if it
        were content is how an agent concludes a peer sent gibberish; returning
        empty is how it concludes the peer sent nothing. Both are worse than
        the truth, which is that this message was sealed to keys this machine
        does not hold.
        """
        from . import sealing

        body = message.get("text_body") or message.get("text") or ""
        if not sealing.is_sealed(body):
            return message
        if not sealing.load_private_keys(self.agent):
            message["sealed_unreadable"] = "no sealing key on this machine"
            return message
        try:
            message["text_body"] = sealing.unseal_with_any(body, self.agent)
            message["sealed_opened"] = True
        except sealing.MalformedSealed as exc:
            message["sealed_unreadable"] = f"the sealed body is damaged: {exc}"
        except sealing.CannotDecrypt:
            message["sealed_unreadable"] = (
                "sealed to keys this machine does not hold — it was sent before "
                "this agent published a key, or to other recipients"
            )
        return message

    def _as_message_id(self, ident: str, *, agent: str | None = None) -> str:
        """A delivery id resolved to its message id, or the input unchanged.

        Every surface an agent reads prints `agentbus show <DELIVERY_ID>`, so
        pasting that into `reply` is the natural move — and it used to fail with
        a bare `not_found` that gave no hint the id was the wrong KIND.
        """
        try:
            delivery = self.read(ident, agent=agent)
        except AgentBusError:
            return ident  # not a delivery of ours; let the server speak
        return str(delivery.get("message_id") or ident)

    def reply(
        self,
        message_id: str,
        text: str,
        *,
        # #155: opt-in reply-all, never the default. True addresses the sender
        # plus the parent message's recipients (Cc preserved, you excluded).
        reply_all: bool = False,
        cc: Sequence[str] | str | None = None,
        priority: str | None = None,
        subject: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        agent: str | None = None,
        # SEV-2-D (#234): caller-supplied stable key for retry safety; see send().
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        # ACCEPT EITHER ID KIND, which is what the skill documents
        # unconditionally and what the CLI and MCP already do. The SDK was the
        # third surface and was missed — MCP's own comment records the previous
        # instance of this exact miss ("fixed in `agentbus reply` and NOT
        # here"), so this is the same defect one surface further along.
        #
        # Resolved HERE rather than server-side on purpose: the reply route is
        # guarded by a `message_participant` rule that runs in a dependency and
        # rejects an unknown message id before any handler code, deliberately,
        # so the write path is no more of an existence oracle than the read path.
        message_id = self._as_message_id(message_id, agent=agent)
        payload = {
            "text": text,
            "reply_all": reply_all,
            "priority": priority,
            "cc": ([cc] if isinstance(cc, str) else list(cc)) if cc else None,
            "subject": subject,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
        }
        # #220: A REPLY MUST SEAL TOO, and it could not, because it does not
        # know its own recipients — they are derived from the PARENT, server
        # side. So `reply` was the one send path that simply did not work on an
        # encrypted workspace: every attempt came back "the body must be sealed
        # by the client" and no client could comply. Two agents on two operating
        # systems reported it independently within minutes.
        #
        # Ask the server who the reply reaches, then seal to exactly those keys.
        # Re-deriving reply/reply-all addressing here would put that rule in two
        # places, which is the mistake #155 already paid for once.
        payload, resolved = self._seal_if_needed(
            payload,
            agent,
            resolve_path="/v1/recipients/resolve-reply",
            resolve_body={
                "message_id": message_id,
                "reply_all": reply_all,
                "to": payload.get("to"),
                "cc": payload.get("cc"),
                "subject": subject,
            },
        )
        # #220, second half: A REPLY WAS NEVER SIGNED, on any workspace. It could
        # not be — the signature covers the recipients and subject the SERVER
        # derives for a reply, so the resolver returns them and we sign over that
        # answer rather than re-deriving "Re: " here.
        payload = self._sign_if_possible(payload, agent, resolved)
        return self._request(
            "POST",
            f"/v1/messages/{message_id}/reply",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def status(
        self,
        state: str | None = None,
        *,
        seconds: int | None = None,
        reason: str | None = None,
        hold_below: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Read or declare this agent's availability (#187).

        With no `state` this READS. With one it declares:

            online    the default; releases anything withheld
            busy      occupied until T — delivers normally, tells senders
            away      same, different word for a human reading the roster
            dnd       WITHHOLDS normal and background; urgent still arrives
            offline   WITHHOLDS everything, and nothing tries to wake you

        `dnd` and `offline` are the recipient deciding, which is what #168's
        `busy()` was missing: it declared, and every sender was free to ignore
        it. Withheld mail is stored and delivered — with a wake — when the
        status clears or expires.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        if state is None:
            result: dict[str, Any] = self._request(
                "GET", f"/v1/agents/{target}/availability", agent=target
            )
            return result
        body: dict[str, Any] = {"state": state}
        if seconds is not None:
            body["seconds"] = seconds
        if reason is not None:
            body["reason"] = reason
        if hold_below is not None:
            body["hold_below"] = hold_below
        declared: dict[str, Any] = self._request(
            "PUT", f"/v1/agents/{target}/availability", json=body, agent=target
        )
        return declared

    def busy(
        self,
        seconds: int,
        *,
        reason: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Declare how long THIS agent cannot take new work (#168); 0 clears it.

        A duration, not a flag: it expires by itself, so a crash cannot leave
        this agent looking permanently unavailable.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = self._request(
            "POST",
            f"/v1/agents/{target}/busy",
            json={"seconds": seconds, "reason": reason},
            agent=target,
        )
        return result

    def verify(self, delivery_id: str, *, agent: str | None = None) -> dict[str, Any]:
        """Check a message's signature YOURSELF, without trusting the bus (#173).

        This is the whole point of the feature. The read surface already carries
        the platform's verdict, and that verdict is worth exactly as much as
        your trust in us — which for the question "did this agent really send
        this" is the thing a client asked to stop having to extend.

        So: fetch the message, fetch the sender's published signing key, rebuild
        the canonical bytes locally, and check. Returns what THIS machine
        concluded, beside what the platform said, so a disagreement is visible
        rather than averaged away.
        """
        from . import _signing

        message = self.read(delivery_id, agent=agent)
        provenance = message.get("provenance") or {}
        block = provenance.get("signature") or {}
        if not block.get("signed"):
            return {
                "verified": False,
                "verdict": "unsigned",
                "reason": "unsigned",
                "platform_said": None,
            }

        # SEV-3 (#234): prefer sender_agent_name — a structured field the API
        # publishes when available — over stripping " via AgentBus" from a display
        # string. The strip is fragile: any suffix change ("via AgentBus (encrypted)",
        # localization, extra whitespace) fetches the wrong pubkey and every
        # signature reads as unverifiable forever. The strip stays as a fallback
        # for older servers.
        sender = message.get("sender_agent_name") or (
            (message.get("sender_display") or "").replace(" via AgentBus", "")
        )
        keys = self._request(
            "GET", f"/v1/agents/{sender}/pubkey", params={"algorithm": "ed25519"}, agent=agent
        )
        fingerprint = block.get("key_fingerprint")
        match = next(
            (k for k in keys.get("keys") or [] if k.get("fingerprint") == fingerprint), None
        )
        if match is None:
            return {
                "verified": False,
                # NOT "invalid". We could not find the key, which is a different
                # problem from a signature that fails — and telling a user their
                # peer forged a message when we simply lack a key would be the
                # worse error by far.
                "verdict": "unverifiable",
                "reason": "signing key not published for this fingerprint",
                "platform_said": block.get("state"),
            }
        # THE AS-TYPED ADDRESSING, from the signature block — not the resolved
        # `recipients` list on the message. A `room:ops` send signs "room:ops"
        # and is delivered to its members, so rebuilding from the members would
        # fail to verify a message that is perfectly good.
        signed_for = (block.get("verify_yourself") or {}).get("signed_recipients") or {}
        if isinstance(signed_for, str):
            import json as _json

            signed_for = _json.loads(signed_for)
        # #220: A CHECK THAT CANNOT RUN MUST NOT RETURN A NEGATIVE.
        #
        # The canonical bytes cover the body as a hash, and the message read
        # surface did not return `body_sha256` on a SENDER'S OWN copy — only on
        # a recipient's delivery. So this hashed `None`, the signature
        # necessarily failed, and the tool reported
        #
        #     NOT VERIFIED - signature does not verify
        #
        # for a message that was signed perfectly well. Three agents on three
        # machines reproduced it against their own sent mail before the cause
        # was found, because the very same message verifies through any
        # recipient's copy.
        #
        # That is the worst failure available to this feature: it accuses an
        # honest sender of forgery, and it does so with the confident wording
        # reserved for a real cryptographic mismatch. "I could not check" and
        # "this does not match" are different answers and must never be
        # collapsed - the same distinction `unverifiable` already draws on the
        # server side, and which this client had on the missing-key path and
        # nowhere else.
        if not message.get("body_sha256"):
            return {
                "verified": False,
                "verdict": "unverifiable",
                "reason": (
                    "cannot check: this server did not return `body_sha256` for "
                    "this copy of the message, so there is nothing to verify the "
                    "signature against. This is NOT a failed signature. Upgrade "
                    "the server, or verify through a recipient's delivery id."
                ),
                "platform_said": block.get("state"),
            }
        payload = _signing.canonical_bytes(
            sender=sender,
            to=list(signed_for.get("to") or []),
            cc=list(signed_for.get("cc") or []),
            subject=message.get("subject"),
            priority=message.get("priority") or "normal",
            body_sha256=message.get("body_sha256"),
        )
        try:
            _signing.verify(match["public_key"], payload, block["signature"])
        except _signing.BadSignature as exc:
            return {
                "verified": False,
                "verdict": "invalid",
                "reason": f"signature does not verify: {exc}",
                "platform_said": block.get("state"),
            }
        return {
            "verified": True,
            "verdict": "valid",
            "signed_by": sender,
            "key_fingerprint": fingerprint,
            "platform_said": block.get("state"),
            "means": "checked on THIS machine against the key you fetched",
        }

    def room_history(
        self,
        room: str,
        *,
        limit: int | None = None,
        since: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """What was said in a room BEFORE this agent joined (#170).

        A room is a conversation, and an agent that joins one mid-flight
        otherwise starts blind — it can see every future message and none of the
        context that makes them mean anything. Membership is the authorization,
        so this needs an acting agent: a workspace key with no agent is not
        "everyone" here, it is nobody.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if since is not None:
            params["since"] = since
        result: dict[str, Any] = self._request(
            "GET", f"/v1/rooms/{room}/history", params=params, agent=target
        )
        return result

    def room_schema(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """The shape a room expects (#169).

        Readable by any member on purpose: a producer must be able to see what
        it is expected to send BEFORE being refused for getting it wrong. A
        contract you can only discover by violating it is not a contract.
        """
        result: dict[str, Any] = self._request("GET", f"/v1/rooms/{room}/schema", agent=agent)
        return result

    def set_room_schema(
        self, room: str, schema: dict[str, Any] | None, *, agent: str | None = None
    ) -> dict[str, Any]:
        """Declare or clear a room's payload contract (#169). `None` clears it.

        Membership is the rule — a room's schema is its contract, and a key that
        is not in the room must not reshape what everybody else has to send.
        Refused on encrypted workspaces: a server that cannot read a body cannot
        validate it.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = self._request(
            "PUT", f"/v1/rooms/{room}/schema", json={"schema": schema}, agent=target
        )
        return result

    def join_room(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """Join a room, so its broadcasts reach this agent."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = self._request("POST", f"/v1/rooms/{room}/join", agent=target)
        return result

    def inbox(
        self,
        cursor: int = 0,
        *,
        limit: int = 50,
        label: str | None = None,
        wait: int = 0,
        unread: bool = False,
        agent: str | None = None,
    ) -> list[Delivery]:
        """One page of deliveries after `cursor`. `wait` long-polls (max 55s).

        `unread=True` asks the SERVER for unread only. Page-and-filter from
        cursor 0 cannot answer "what is unread": cursor 0 is the OLDEST page,
        so the window fills with read mail as history grows and the filter goes
        blind while looking exactly like an empty inbox.
        """
        params: dict[str, Any] = {"cursor": cursor, "limit": limit}
        if label:
            params["label"] = label
        if unread:
            params["unread"] = "true"
        if wait:
            params["wait"] = min(wait, 55)
        result = self._request(
            "GET",
            "/v1/inbox",
            params=params,
            agent=agent,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )
        return [Delivery.from_api(m) for m in result["messages"]]

    def follow(
        self, cursor: int = 0, *, agent: str | None = None, wait: int = 30
    ) -> Iterator[Delivery]:
        """Yield deliveries forever, advancing the cursor as they are consumed."""
        while True:
            batch = self.inbox(cursor, wait=wait, agent=agent)
            for delivery in batch:
                cursor = max(cursor, delivery.seq)
                yield delivery

    def read(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        """Read one delivery, unsealing it when this machine holds the key.

        Unsealing happens HERE rather than being an extra step the caller must
        remember: a reader that forgets it gets ciphertext and no explanation,
        and "remember to decrypt" is exactly the kind of instruction that is
        followed for a week.
        """
        return self.unseal_message(
            self._request("GET", f"/v1/deliveries/{delivery_id}", agent=agent)
        )

    def get_claim(self, message_id: str, agent: str | None = None) -> dict[str, Any]:
        """The claim a message carries, with its verdicts and explicit
        unverified state (#63). The platform never runs the repro."""
        data = self._request("GET", f"/v1/messages/{message_id}/claim", agent=agent)
        return data if isinstance(data, dict) else {"claim": None, "note": "malformed"}

    def record_verdict(
        self,
        message_id: str,
        *,
        result: str,
        observed_exit: int | None = None,
        observed_output: str | None = None,
        client_version: str | None = None,
        env_note: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Record THIS agent's verdict on a claim it ran itself.

        Attestation is server-side, from this key's binding — never from this
        payload. The caller executed the repro on its own host, opt-in.
        """
        payload: dict[str, Any] = {"result": result}
        if observed_exit is not None:
            payload["observed_exit"] = observed_exit
        if observed_output is not None:
            payload["observed_output"] = observed_output
        if client_version is not None:
            payload["client_version"] = client_version
        if env_note is not None:
            payload["env_note"] = env_note
        data = self._request(
            "POST", f"/v1/messages/{message_id}/claim/verdict", json=payload, agent=agent
        )
        return data if isinstance(data, dict) else {"verdict_id": "", "result": result}

    def ack(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/deliveries/{delivery_id}/ack", agent=agent)

    def label(
        self,
        delivery_id: str,
        add: Sequence[str] | None = None,
        remove: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> list[str]:
        payload = {"add": list(add or []), "remove": list(remove or [])}
        return self._request(
            "POST", f"/v1/deliveries/{delivery_id}/labels", json=payload, agent=agent
        )["labels"]

    def thread(self, thread_id: str) -> dict[str, Any]:
        """Read a whole conversation, unsealing each message where possible.

        F10 (issuedb #11): on an encrypted workspace the server holds
        ciphertext by design (end-to-end sealing — no private key server-
        side). Before this fix, `agentbus thread --json` returned each
        `text_body` as the raw age envelope, and reading N turns cost N
        `agentbus show` round trips just to unseal them. The fix mirrors
        what `read()` already does per delivery: iterate messages once and
        call `unseal_message` on each, so `sealed_unreadable` appears in
        place of a body the caller cannot open (matching `show`).
        """
        result = self._request("GET", f"/v1/threads/{thread_id}")
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
        return result

    def threads(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/threads", params={"limit": limit})["threads"]

    def raw(self, message_id: str) -> str:
        """The canonical RFC822 artifact for a message."""
        try:
            response = self._client.get(f"/v1/messages/{message_id}/raw", headers=self._headers())
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        _raise_for(response)
        return response.text

    # ---------------------------------------------------------- approvals

    def request_approval(
        self,
        title: str,
        *,
        kind: str = "generic",
        summary: str | None = None,
        proposed_action: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        thread_id: str | None = None,
        expires_in_minutes: int | None = None,
        agent: str | None = None,
        # SEV-2-D (#234): caller-supplied stable key for retry safety. Especially
        # important for approvals — a retried request_approval without a stable
        # key mints TWO approval rows to the human, who then wonders which is real.
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "title": title,
            "kind": kind,
            "summary": summary,
            "proposed_action": proposed_action or {},
            "context": context or {},
            "thread_id": thread_id,
            "expires_in_minutes": expires_in_minutes,
        }
        return self._request(
            "POST",
            "/v1/approvals",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def approval(self, approval_id: str, wait: int = 0) -> dict[str, Any]:
        params = {"wait": min(wait, 55)} if wait else {}
        return self._request(
            "GET",
            f"/v1/approvals/{approval_id}",
            params=params,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )

    # ---------------------------------------------------------- misc

    def usage(self) -> dict[str, Any]:
        return self._request("GET", "/v1/usage")

    def heartbeat_liveness(self, agent: str | None = None) -> dict[str, Any]:
        """Poll once purely to answer a challenge and learn what is waiting.

        Cheap: one request, no message, no quota. Call it on a timer if your
        session is long-lived and quiet, so you read as `responsive` rather than
        merely `reachable`.
        """
        result = self._request("GET", "/v1/inbox?cursor=0&limit=1", agent=agent)
        # REG-5 (round-3 audit): the lock is the invariant, all readers honour
        # it. `is not None` is a diagnostic bool so the impact of reading unlocked
        # is small, but keeping every reader under the lock keeps the discipline
        # honest — a future reader who copies this shape gets the safe pattern.
        with self._challenge_lock:
            answered = self._pending_challenge is not None
        return {
            "waiting": result.get("count", 0),
            "answered_challenge": answered,
        }

    def create_webhook(
        self, url: str, events: Sequence[str] | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/webhooks",
            json={"url": url, "events": list(events) if events else None, "agent": agent},
        )

    def drafts(self, agent: str | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/drafts", agent=agent)["drafts"]

    def create_draft(
        self,
        to: Sequence[str],
        subject: str = "",
        text: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Store a draft, sealed to YOUR OWN key on an encrypted workspace.

        #222, operator ruling 2026-08-16: an unsent draft used to sit in the
        drafts table as plaintext. Sealing it to the author's own key removes
        that without changing who can read it — the author is the only party who
        ever could, and is the only one who needs to.

        SEALED TO SELF HERE, TO RECIPIENTS AT SEND TIME. These are two different
        seals on purpose. A draft's recipients can be edited after it is written,
        so sealing to them now could seal to a set that is wrong by the time it
        goes out — delivered and unreadable by the agents it reached. Self here,
        recipients there.

        THE SERVER STILL ACCEPTS PLAINTEXT, deliberately. Refusing it would break
        every un-upgraded client the moment this shipped, which is exactly the
        failure #221 was: a rule enforced before any client could satisfy it.
        Publish first, tighten later - the same ordering `keys rotate` uses.
        """
        body = {"to": list(to), "subject": subject, "text": text}
        if text:
            body = self._seal_to_self(body, agent)
        return self._request("POST", "/v1/drafts", json=body, agent=agent)

    def _seal_to_self(self, payload: dict[str, Any], agent: str | None) -> dict[str, Any]:
        """Seal `text` to this agent's own key, or leave it alone.

        Asks the server whether the workspace is encrypted rather than guessing,
        the same way `_seal_if_needed` does, and falls through untouched when the
        question cannot be answered — a refusal must never cost you the write.
        """
        try:
            resolved = self._request(
                "POST", "/v1/recipients/resolve", json={"to": [agent or self.agent]}, agent=agent
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload
            raise
        if not resolved.get("encrypted"):
            return payload
        _private, own_public = sealing.ensure_keypair(agent or self.agent)
        sealed = dict(payload)
        sealed["text"] = sealing.seal_for(payload["text"], [own_public])
        sealed["sealed"] = True
        return sealed

    def send_draft(self, draft_id: str, agent: str | None = None) -> dict[str, Any]:
        """Send a stored draft, sealing it first when the workspace is encrypted.

        #221: THIS USED TO POST NOTHING, and on an encrypted workspace that meant
        a draft could never be sent at all — the stored body is plaintext, the
        server refuses plaintext, and there was nowhere to put a sealed one.
        Encryption is the default for new workspaces, so the whole draft feature
        was unusable by default.

        SEALED HERE, AT SEND TIME, rather than when the draft was written. A
        draft's recipients can be edited after creation, so a body sealed at
        creation could be sealed to the wrong set by the time it goes out —
        delivered and unreadable by the agents it reached, which is worse than a
        refusal. Sealing now means sealing to whoever it will ACTUALLY reach.

        The draft is fetched to learn its recipients and body. On an unencrypted
        workspace `_seal_if_needed` returns the payload untouched and this posts
        the same empty-bodied request it always did.
        """
        draft = self._request("GET", f"/v1/drafts/{draft_id}", agent=agent)
        recipients = draft.get("recipients") or draft.get("to") or []
        if isinstance(recipients, str):
            recipients = json.loads(recipients)

        # #222: THE STORED DRAFT MAY BE SEALED TO US. create_draft seals to the
        # author's own key on an encrypted workspace, so open it before sealing
        # it again to the recipients. A draft written by an older client is
        # plaintext and passes through untouched — `unseal_message` is a no-op on
        # a body that does not carry an age header.
        opened = self.unseal_message({"text_body": draft.get("text_body") or draft.get("text")})
        if opened.get("sealed_unreadable"):
            raise AgentBusError(
                "cannot send this draft: its stored body is sealed to a key this "
                f"machine does not hold ({opened['sealed_unreadable']}). Send it "
                "from the machine that wrote it."
            )

        payload: dict[str, Any] = {
            "text": opened.get("text_body"),
            "subject": draft.get("subject"),
            "to": list(recipients),
        }
        payload, resolved = self._seal_if_needed(payload, agent)
        if not payload.get("sealed"):
            # Nothing to add: an unencrypted workspace (or a server that refused
            # the resolve) gets the request it has always received, and the
            # server uses its own stored body.
            plain: dict[str, Any] = self._request(
                "POST", f"/v1/drafts/{draft_id}/send", agent=agent
            )
            return plain

        payload = self._sign_if_possible(payload, agent, resolved)
        body = {
            "text": payload["text"],
            "sealed": True,
            "signature": payload.get("signature"),
            "signing_key_fingerprint": payload.get("signing_key_fingerprint"),
        }
        sent: dict[str, Any] = self._request(
            "POST", f"/v1/drafts/{draft_id}/send", json=body, agent=agent, idempotent=True
        )
        return sent


class AsyncAgentBus(_Base):
    """Async mirror of :class:`AgentBus`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Same pool caps as the sync client (SEV-1-C, #234) — the async fan-out
        # is precisely the case a bulkhead-shaped connection cap protects.
        limits = httpx.Limits(
            max_connections=int(os.environ.get("AGENTBUS_MAX_CONNECTIONS", "20")),
            max_keepalive_connections=int(
                os.environ.get("AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", "10")
            ),
            keepalive_expiry=float(os.environ.get("AGENTBUS_KEEPALIVE_EXPIRY", "30")),
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, limits=limits
        )

    async def _seal_if_needed(
        self,
        payload: dict[str, Any],
        agent: str | None,
        *,
        resolve_path: str = "/v1/recipients/resolve",
        resolve_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The async twin of the sync client's sealer. #220.

        This class sealed NOTHING before: every async send on an encrypted
        workspace was refused by the server with "the body must be sealed by the
        client", and the async surface had no way to comply. The sealing rule
        lived on the sync class only — one surface fixed, the other left, which
        is the exact shape this codebase keeps paying for.

        Only the request differs, so only the request is duplicated; the
        decision itself is `_apply_seal` on the shared base.

        RETURNS THE RESOLVER'S ANSWER ALONGSIDE THE PAYLOAD, because the
        signature needs it: on a reply the server derives the recipients and the
        "Re: " subject, and a signature covers what the server STORES. `None`
        means the question was never answered -- no such route, or refused --
        and the caller then signs over its own payload, as it always did.
        """
        try:
            resolved = await self._request(
                "POST",
                resolve_path,
                json=resolve_body if resolve_body is not None else payload,
                agent=agent,
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload, None
            raise
        return self._apply_seal(payload, resolved), resolved

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncAgentBus:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        agent: str | None = None,
        idempotent: bool = False,
        # REG-7 (round-3 audit): SEV-2-D wired `idempotency_key` through every
        # sync public method AND the sync _request, but the async _request
        # never got the param — so every async send/reply/request_approval
        # was silently dropping the caller-supplied key on the floor. Sending
        # 3 positional args to a 2-param _headers was legal Python but a real
        # correctness hole for anyone using the async client.
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        # Mint the key ONCE OUTSIDE the retry loop, same rule as the sync path.
        if (idempotent or idempotency_key) and not idempotency_key:
            idempotency_key = str(uuid.uuid4())

        async def _do_request() -> Any:
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers=self._headers(agent, idempotent, idempotency_key),
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise TransportError(str(exc)) from exc
            _raise_for(response)
            payload = response.json() if response.content else None
            self._capture_challenge(payload)
            return payload

        # REG-7: resilience for the async client. resilient_circuit is sync-only
        # so we hand-roll retry + concurrency isolation here. Semantics match
        # the sync stack: only transient errors retry; bulkhead is a
        # per-instance asyncio.Semaphore acquired for the whole retry sequence
        # (so N failing callers don't multiply their load).
        if os.environ.get("AGENTBUS_SDK_RESILIENCE") == "0":
            return await _do_request()
        return await self._run_with_resilience_async(_do_request)

    async def _run_with_resilience_async(self, fn: Any) -> Any:
        """Retry-with-backoff + async bulkhead for async _request (REG-7).

        Semantics mirror the sync `_run_with_resilience`: retries only fire for
        transient errors (transport / 503), non-transient errors pass through
        immediately, and one Semaphore slot covers the whole retry sequence so
        N callers can never multiply their load during a bus deploy.

        REG-10 (round-3 re-audit): the semaphore is keyed BY THE RUNNING EVENT
        LOOP, not by the instance. asyncio.Semaphore binds permanently to the
        loop it was instantiated on — a global AsyncAgentBus reused across
        loops (uvicorn worker restart, pytest-asyncio's per-test loop, a
        script that runs asyncio.run() twice) then raises RuntimeError on the
        second loop. Per-loop lookup, keyed by id(loop) with weakref cleanup
        on loop GC, means "one bulkhead PER (instance, loop) pair" and this
        class of RuntimeError is impossible.
        """
        import asyncio
        import random
        import weakref

        max_retries = int(os.environ.get("AGENTBUS_SDK_MAX_RETRIES", "3"))
        # Same backoff shape as the sync SafetyNet: 0.5s..8s exponential, jitter.
        base = 0.5
        cap = 8.0

        # REG-10: fetch (or create) the semaphore for THIS loop.
        loop = asyncio.get_running_loop()
        bulkheads: dict[int, asyncio.Semaphore] = getattr(self, "_async_bulkheads_by_loop", None)
        if bulkheads is None:
            bulkheads = self._async_bulkheads_by_loop = {}
        loop_id = id(loop)
        sem = bulkheads.get(loop_id)
        if sem is None:
            sem = asyncio.Semaphore(int(os.environ.get("AGENTBUS_SDK_MAX_CONCURRENT", "8")))
            bulkheads[loop_id] = sem
            # When the loop is garbage-collected, drop the dead entry so a
            # long-lived AsyncAgentBus used across many short-lived loops
            # (per-test asyncio loops, say) does not accumulate dead sems.
            with contextlib.suppress(TypeError):
                weakref.finalize(loop, bulkheads.pop, loop_id, None)

        last_exc: BaseException | None = None
        async with sem:
            for attempt in range(max_retries + 1):
                try:
                    return await fn()
                except BaseException as exc:
                    if not _is_transient_sdk_error(exc):
                        # 4xx, quota, etc. — pass through unchanged.
                        raise
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(cap, base * (2**attempt))
                    delay *= 1 + random.uniform(-0.2, 0.2)
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def register(
        self,
        name: str | None = None,
        *,
        role: str | None = None,
        repo_remote: str | None = None,
        workdir: str | None = None,
        capabilities: Sequence[str] | None = None,
        labels: dict[str, str] | None = None,
        unlisted: bool = False,
        ephemeral: bool | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.register. Prefer `role` over `name`."""
        from . import identity

        payload: dict[str, Any] = {
            "name": name,
            "capabilities": list(capabilities or []),
            "labels": labels or {},
            "unlisted": unlisted,
        }
        if role:
            env = identity.describe(workdir)
            payload.update(
                role=role,
                device_id=env["device_id"],
                workdir=env["workdir"],
                repo_remote=repo_remote or env["repo_remote"],
                ephemeral=env["ephemeral"] if ephemeral is None else ephemeral,
            )
        else:
            payload.update(repo_remote=repo_remote, ephemeral=bool(ephemeral))
        result = await self._request("POST", "/v1/agents/register", json=payload)
        self.agent = result["agent"]["name"]
        return result

    async def whoami(self, agent: str | None = None) -> dict[str, Any]:
        return await self._request("GET", "/v1/whoami", agent=agent)

    async def phonebook(
        self,
        query: str | None = None,
        *,
        capability: str | None = None,
        repo_fingerprint: str | None = None,
        label: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            k: v
            for k, v in {
                "q": query,
                "capability": capability,
                "repo_fingerprint": repo_fingerprint,
            }.items()
            if v
        }
        if label:
            # #149: keep the async copy at parity — this file has drifted before.
            params["label"] = [label] if isinstance(label, str) else list(label)
        result = await self._request("GET", "/v1/agents", params=params)
        return result["agents"]

    async def status(
        self,
        state: str | None = None,
        *,
        seconds: int | None = None,
        reason: str | None = None,
        hold_below: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.status() (#187).

        With no `state` this READS. With one it declares:

            online    the default; releases anything withheld
            busy      occupied until T — delivers normally, tells senders
            away      same, different word for a human reading the roster
            dnd       WITHHOLDS normal and background; urgent still arrives
            offline   WITHHOLDS everything, and nothing tries to wake you

        `dnd` and `offline` are the recipient deciding, which is what #168's
        `busy()` was missing: it declared, and every sender was free to ignore
        it. Withheld mail is stored and delivered — with a wake — when the
        status clears or expires.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        if state is None:
            result: dict[str, Any] = await self._request(
                "GET", f"/v1/agents/{target}/availability", agent=target
            )
            return result
        body: dict[str, Any] = {"state": state}
        if seconds is not None:
            body["seconds"] = seconds
        if reason is not None:
            body["reason"] = reason
        if hold_below is not None:
            body["hold_below"] = hold_below
        declared: dict[str, Any] = await self._request(
            "PUT", f"/v1/agents/{target}/availability", json=body, agent=target
        )
        return declared

    async def room_history(
        self,
        room: str,
        *,
        limit: int | None = None,
        since: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.room_history() (#170)."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if since is not None:
            params["since"] = since
        result: dict[str, Any] = await self._request(
            "GET", f"/v1/rooms/{room}/history", params=params, agent=target
        )
        return result

    async def room_schema(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """Async mirror of AgentBus.room_schema() (#169)."""
        result: dict[str, Any] = await self._request("GET", f"/v1/rooms/{room}/schema", agent=agent)
        return result

    async def set_room_schema(
        self, room: str, schema: dict[str, Any] | None, *, agent: str | None = None
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.set_room_schema() (#169)."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = await self._request(
            "PUT", f"/v1/rooms/{room}/schema", json={"schema": schema}, agent=target
        )
        return result

    async def join_room(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """Async mirror of AgentBus.join_room()."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = await self._request("POST", f"/v1/rooms/{room}/join", agent=target)
        return result

    async def tag(
        self,
        set: dict[str, str] | None = None,
        remove: Sequence[str] | None = None,
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.tag() (#149)."""
        target = agent or self.agent
        if not target:
            raise ValueError("tag() needs an acting agent: pass agent= or construct with one")
        result: dict[str, Any] = await self._request(
            "PATCH",
            f"/v1/agents/{target}/labels",
            json={"set": set or {}, "remove": list(remove or [])},
            agent=target,
        )
        return result

    async def send(
        self,
        to: Sequence[str] | str,
        subject: str = "",
        text: str | None = None,
        *,
        # #155: copied, not addressed. Cc recipients get the same delivery; the
        # difference is the intent they can read off it (to = act, cc = informed).
        cc: Sequence[str] | str | None = None,
        # #167: urgent | normal | background. Omitted = normal, so nothing
        # changes for a caller that never sets it.
        priority: str | None = None,
        html: str | None = None,
        thread_id: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        # The async class MIRRORS the sync one deliberately. It has drifted
        # before — `phonebook(label=)` landed on one and not the other — and a
        # caller who switches to async should not silently lose options.
        require_available: bool = False,
        # SEV-2-H (#234): missing `derived_from` on the async surface — drift.
        # Placed at the same position as AgentBus.send for the parity test to pass.
        derived_from: Sequence[str] | None = None,
        payload: Any = None,
        guarantee: str | None = None,
        agent: str | None = None,
        # SEV-2-D (#234): stable key for retry safety; see AgentBus.send().
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        recipients = [to] if isinstance(to, str) else list(to)
        copied = [cc] if isinstance(cc, str) else list(cc or [])
        body = {
            "to": recipients,
            "cc": copied,
            "priority": priority,
            "subject": subject,
            "text": text,
            "html": html,
            "thread_id": thread_id,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
            "require_available": require_available,
            "payload": payload,
            "guarantee": guarantee,
            "derived_from": list(derived_from) if derived_from else None,
        }
        body, resolved = await self._seal_if_needed(body, agent)
        # #220: SIGNED TOO. This surface signed nothing at all — `send` on the
        # sync client was the only one of the four that ever did. Same ordering
        # as there: seal first, so the signature covers the stored bytes.
        body = self._sign_if_possible(body, agent, resolved)
        return await self._request(
            "POST",
            "/v1/messages",
            json=body,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def reply(
        self,
        message_id: str,
        text: str,
        *,
        # #155: opt-in reply-all, never the default. True addresses the sender
        # plus the parent message's recipients (Cc preserved, you excluded).
        reply_all: bool = False,
        cc: Sequence[str] | str | None = None,
        priority: str | None = None,
        subject: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "text": text,
            "reply_all": reply_all,
            "priority": priority,
            "cc": ([cc] if isinstance(cc, str) else list(cc)) if cc else None,
            "subject": subject,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
        }
        # #220: A REPLY MUST SEAL TOO, and it could not, because it does not
        # know its own recipients — they are derived from the PARENT, server
        # side. So `reply` was the one send path that simply did not work on an
        # encrypted workspace: every attempt came back "the body must be sealed
        # by the client" and no client could comply. Two agents on two operating
        # systems reported it independently within minutes.
        #
        # Ask the server who the reply reaches, then seal to exactly those keys.
        # Re-deriving reply/reply-all addressing here would put that rule in two
        # places, which is the mistake #155 already paid for once.
        payload, resolved = await self._seal_if_needed(
            payload,
            agent,
            resolve_path="/v1/recipients/resolve-reply",
            resolve_body={
                "message_id": message_id,
                "reply_all": reply_all,
                "to": payload.get("to"),
                "cc": payload.get("cc"),
                "subject": subject,
            },
        )
        # #220, second half: signed over the server's own answer, for the same
        # reason as the sync reply. This surface signed nothing before.
        payload = self._sign_if_possible(payload, agent, resolved)
        return await self._request(
            "POST",
            f"/v1/messages/{message_id}/reply",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def inbox(
        self,
        cursor: int = 0,
        *,
        limit: int = 50,
        label: str | None = None,
        wait: int = 0,
        unread: bool = False,
        agent: str | None = None,
    ) -> list[Delivery]:
        params: dict[str, Any] = {"cursor": cursor, "limit": limit}
        if label:
            params["label"] = label
        if unread:
            params["unread"] = "true"
        if wait:
            params["wait"] = min(wait, 55)
        result = await self._request(
            "GET",
            "/v1/inbox",
            params=params,
            agent=agent,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )
        return [Delivery.from_api(m) for m in result["messages"]]

    async def read(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        """Read one delivery, unsealing it when this machine holds the key.

        SEV-4 (#234): the async twin USED to skip unsealing — a bug the sync one
        fixed and this one inherited from the drift the parity test now catches.
        A caller who switched sync -> async silently got ciphertext, and
        "remember to decrypt" is exactly the instruction that gets followed for
        about a week.
        """
        return self.unseal_message(
            await self._request("GET", f"/v1/deliveries/{delivery_id}", agent=agent)
        )

    async def ack(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/v1/deliveries/{delivery_id}/ack", agent=agent)

    async def thread(self, thread_id: str) -> dict[str, Any]:
        """F10 (issuedb #11): match the sync client — unseal each message."""
        result = await self._request("GET", f"/v1/threads/{thread_id}")
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
        return result

    async def usage(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/usage")

    async def request_approval(
        self,
        title: str,
        *,
        kind: str = "generic",
        summary: str | None = None,
        proposed_action: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        # SEV-2-H (#234): missing on the async surface — drift.
        thread_id: str | None = None,
        expires_in_minutes: int | None = None,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "title": title,
            "kind": kind,
            "summary": summary,
            "proposed_action": proposed_action or {},
            "context": context or {},
            "thread_id": thread_id,
            "expires_in_minutes": expires_in_minutes,
        }
        return await self._request(
            "POST",
            "/v1/approvals",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def approval(self, approval_id: str, wait: int = 0) -> dict[str, Any]:
        params = {"wait": min(wait, 55)} if wait else {}
        return await self._request(
            "GET",
            f"/v1/approvals/{approval_id}",
            params=params,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )

    # ---------------------------------------------------------------- #234 SEV-2-H
    # Async twins added by round-two audit; the parity test now catches drift.

    async def close(self) -> None:
        """Alias for aclose(), so the sync/async parity test does not need a
        rename exemption. Both spellings work; use whichever your framework prefers.
        """
        await self.aclose()

    async def attachment(
        self, delivery_id: str, index: int = 0, *, agent: str | None = None
    ) -> bytes:
        """The RAW BYTES of one attachment on a delivery — async twin of AgentBus.attachment.

        Unseals on the way out with any private key this machine holds, so
        `sealed_by=sender` armor never leaks to the caller as-is.

        REG-9 (round-3 re-audit): stream + boundary cap, same rule as the sync
        twin. See AgentBus.attachment for the reasoning.
        """
        from . import sealing

        cap = int(1.5 * _max_attachment_bytes())
        content: bytes
        try:
            async with self._client.stream(
                "GET",
                f"/v1/deliveries/{delivery_id}/attachments/{index}",
                headers=self._headers(agent, False),
            ) as response:
                _raise_for(response)
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > cap:
                        raise AgentBusError(
                            f"attachment is over the {cap:,} byte cap "
                            f"({total:,} bytes received so far and still coming). "
                            "Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                            "has the RAM budget; the client holds the ciphertext "
                            "and the plaintext at the same time during unseal."
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        if content[:64].lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
            if not sealing.load_private_keys(self.agent):
                raise AgentBusError(
                    "this attachment is sealed and this machine holds no sealing key"
                )
            try:
                return sealing.unseal_bytes_with_any(content, self.agent)
            except sealing.MalformedSealed as exc:
                raise AgentBusError(f"this attachment is damaged: {exc}") from exc
            except sealing.CannotDecrypt as exc:
                raise AgentBusError(
                    "this attachment was sealed to keys this machine does not hold"
                ) from exc
        return content

    async def raw(self, message_id: str) -> str:
        """The canonical RFC822 artifact for a message — async twin of AgentBus.raw."""
        try:
            response = await self._client.get(
                f"/v1/messages/{message_id}/raw", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        _raise_for(response)
        return response.text

    async def threads(self, limit: int = 50) -> list[dict[str, Any]]:
        """List threads the acting agent participates in — async twin."""
        result: dict[str, Any] = await self._request("GET", "/v1/threads", params={"limit": limit})
        return result["threads"]

    async def label(
        self,
        delivery_id: str,
        add: Sequence[str] | None = None,
        remove: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> list[str]:
        """Add/remove labels on a delivery — async twin."""
        payload = {"add": list(add or []), "remove": list(remove or [])}
        result: dict[str, Any] = await self._request(
            "POST", f"/v1/deliveries/{delivery_id}/labels", json=payload, agent=agent
        )
        return result["labels"]

    async def busy(
        self, seconds: int, *, reason: str | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        """Declare this agent busy for a duration — async twin of AgentBus.busy."""
        return await self.status("busy", seconds=seconds, reason=reason, agent=agent)

    async def heartbeat(self, agent: str | None = None) -> None:
        """Refresh presence for the acting agent — async twin.

        REG-4 (round-3 audit): posts to /v1/agents/<agent>/heartbeat, matching
        the sync twin at AgentBus.heartbeat (client.py). This method previously
        posted to /v1/heartbeat, which the server 404s — presence went silently
        stale for anyone using the async client. The parity test now compares
        endpoint strings so this drift class fails CI.
        """
        await self._request("POST", f"/v1/agents/{agent or self.agent}/heartbeat", agent=agent)

    async def heartbeat_liveness(self, agent: str | None = None) -> dict[str, Any]:
        """Cheap poll to answer a challenge and learn what is waiting — async twin."""
        result = await self._request("GET", "/v1/inbox?cursor=0&limit=1", agent=agent)
        # REG-5 (round-3 audit): the lock is the invariant, all readers honour
        # it. `is not None` is a diagnostic bool so the impact of reading unlocked
        # is small, but keeping every reader under the lock keeps the discipline
        # honest — a future reader who copies this shape gets the safe pattern.
        with self._challenge_lock:
            answered = self._pending_challenge is not None
        return {
            "waiting": result.get("count", 0),
            "answered_challenge": answered,
        }

    async def retire(self, agent: str | None = None) -> None:
        """Stand this agent down (reversible) — async twin."""
        target = agent or self.agent
        if not target:
            raise ValueError("retire() needs an acting agent: pass agent= or construct with one")
        await self._request("POST", f"/v1/agents/{target}/retire", agent=target)

    async def follow(self, cursor: int = 0, *, agent: str | None = None, wait: int = 30) -> Any:
        """Async generator yielding deliveries as they arrive — async twin.

        Return type is Any (not AsyncIterator[Delivery]) because it is an async
        GENERATOR, which typing expresses as AsyncGenerator[Delivery, None]. The
        loose annotation keeps the parity test's param-name check simple; the
        docstring is authoritative.
        """
        while True:
            batch = await self.inbox(cursor, wait=wait, agent=agent)
            for delivery in batch:
                cursor = max(cursor, delivery.seq)
                yield delivery

    async def get_claim(self, message_id: str, agent: str | None = None) -> dict[str, Any]:
        """Read the claim carried by a message, with verdicts — async twin."""
        return await self._request("GET", f"/v1/messages/{message_id}/claim", agent=agent)

    async def record_verdict(
        self,
        message_id: str,
        *,
        result: str,
        observed_exit: int | None = None,
        observed_output: str | None = None,
        client_version: str | None = None,
        env_note: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Record THIS agent's verdict on a claim it ran itself — async twin."""
        payload: dict[str, Any] = {"result": result}
        if observed_exit is not None:
            payload["observed_exit"] = observed_exit
        if observed_output is not None:
            payload["observed_output"] = observed_output
        if client_version is not None:
            payload["client_version"] = client_version
        if env_note is not None:
            payload["env_note"] = env_note
        data = await self._request(
            "POST", f"/v1/messages/{message_id}/claim/verdict", json=payload, agent=agent
        )
        return data if isinstance(data, dict) else {"verdict_id": "", "result": result}

    async def mint_key(
        self,
        *,
        scope: str = "send",
        agents: Sequence[str] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Mint a key no stronger than this one — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/keys",
            json={
                "scope": scope,
                "agents": list(agents) if agents else None,
                "label": label,
            },
        )
        return result

    async def create_invite(
        self, *, role: str | None = None, ttl_seconds: int = 3600
    ) -> dict[str, Any]:
        """Mint a one-time join token so a NEW agent can register itself — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/invites",
            json={"role": role, "ttl_seconds": ttl_seconds},
        )
        return result

    async def create_webhook(
        self,
        url: str,
        events: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Create a webhook subscription — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/webhooks",
            json={"url": url, "events": list(events) if events else None, "agent": agent},
        )
        return result

    async def drafts(self, agent: str | None = None) -> list[dict[str, Any]]:
        """List this agent's drafts — async twin."""
        result: dict[str, Any] = await self._request("GET", "/v1/drafts", agent=agent)
        return result["drafts"]

    async def create_draft(
        self,
        to: Sequence[str],
        subject: str = "",
        text: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Store a draft, sealed to YOUR OWN key on an encrypted workspace — async twin.

        Calls the same _seal_to_self path as the sync twin; sealing is a local
        operation so no async-specific machinery is needed inside it.
        """
        body: dict[str, Any] = {"to": list(to), "subject": subject, "text": text}
        if text:
            body = await self._seal_to_self_async(body, agent)
        return await self._request("POST", "/v1/drafts", json=body, agent=agent)

    async def _seal_to_self_async(
        self, payload: dict[str, Any], agent: str | None
    ) -> dict[str, Any]:
        """Async variant of _seal_to_self — same rule, one await."""
        try:
            resolved = await self._request(
                "POST",
                "/v1/recipients/resolve",
                json={"to": [agent or self.agent]},
                agent=agent,
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload
            raise
        if not resolved.get("encrypted"):
            return payload
        _private, own_public = sealing.ensure_keypair(agent or self.agent)
        sealed = dict(payload)
        sealed["text"] = sealing.seal_for(payload["text"], [own_public])
        sealed["sealed"] = True
        return sealed

    async def send_draft(self, draft_id: str, agent: str | None = None) -> dict[str, Any]:
        """Send a stored draft, sealing it first on an encrypted workspace — async twin."""
        draft = await self._request("GET", f"/v1/drafts/{draft_id}", agent=agent)
        recipients = draft.get("recipients") or draft.get("to") or []
        if isinstance(recipients, str):
            recipients = json.loads(recipients)
        opened = self.unseal_message({"text_body": draft.get("text_body") or draft.get("text")})
        if opened.get("sealed_unreadable"):
            raise AgentBusError(
                "cannot send this draft: its stored body is sealed to a key this "
                f"machine does not hold ({opened['sealed_unreadable']})"
            )
        payload: dict[str, Any] = {
            "text": opened.get("text_body"),
            "subject": draft.get("subject"),
            "to": list(recipients),
        }
        payload, resolved = await self._seal_if_needed(payload, agent)
        if not payload.get("sealed"):
            plain: dict[str, Any] = await self._request(
                "POST", f"/v1/drafts/{draft_id}/send", agent=agent
            )
            return plain
        payload = self._sign_if_possible(payload, agent, resolved)
        body = {
            "text": payload["text"],
            "sealed": True,
            "signature": payload.get("signature"),
            "signing_key_fingerprint": payload.get("signing_key_fingerprint"),
        }
        sent: dict[str, Any] = await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/send",
            json=body,
            agent=agent,
            idempotent=True,
        )
        return sent

    def unseal_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Decrypt a message body in place — async twin.

        Local operation (no I/O), so it is not `async def` even on the async
        client; it uses this machine's sealing keys and returns immediately.
        The parity test only checks the name and parameter shape, not that both
        implementations are the same shape of function.
        """
        return AgentBus.unseal_message(self, message)  # type: ignore[arg-type]

    async def verify(self, delivery_id: str, *, agent: str | None = None) -> dict[str, Any]:
        """Verify a message's signature yourself — async twin of AgentBus.verify.

        SEV-3 (#234): reads the sender from `sender_agent_name` when the server
        publishes it, falling back to the historical " via AgentBus" strip. The
        strip is fragile — a display-suffix change breaks verification — but
        removing it outright would break older servers, so it stays as a fallback.
        """
        from . import _signing

        message = await self.read(delivery_id, agent=agent)
        provenance = message.get("provenance") or {}
        block = provenance.get("signature") or {}
        if not block.get("signed"):
            return {
                "verified": False,
                "verdict": "unsigned",
                "reason": "unsigned",
                "platform_said": None,
            }
        sender = message.get("sender_agent_name") or (
            (message.get("sender_display") or "").replace(" via AgentBus", "")
        )
        keys = await self._request(
            "GET",
            f"/v1/agents/{sender}/pubkey",
            params={"algorithm": "ed25519"},
            agent=agent,
        )
        fingerprint = block.get("key_fingerprint")
        match = next(
            (k for k in keys.get("keys") or [] if k.get("fingerprint") == fingerprint), None
        )
        if match is None:
            return {
                "verified": False,
                "verdict": "unverifiable",
                "reason": "signing key not published for this fingerprint",
                "platform_said": block.get("state"),
            }
        signed_for = (block.get("verify_yourself") or {}).get("signed_recipients") or {}
        if isinstance(signed_for, str):
            signed_for = json.loads(signed_for)
        if not message.get("body_sha256"):
            return {
                "verified": False,
                "verdict": "unverifiable",
                "reason": (
                    "cannot check: this server did not return `body_sha256` for "
                    "this copy of the message. This is NOT a failed signature."
                ),
                "platform_said": block.get("state"),
            }
        payload = _signing.canonical_bytes(
            sender=sender,
            to=list(signed_for.get("to") or []),
            cc=list(signed_for.get("cc") or []),
            subject=message.get("subject"),
            priority=message.get("priority") or "normal",
            body_sha256=message.get("body_sha256"),
        )
        try:
            _signing.verify(match["public_key"], payload, block["signature"])
        except _signing.BadSignature as exc:
            return {
                "verified": False,
                "verdict": "invalid",
                "reason": f"signature does not verify: {exc}",
                "platform_said": block.get("state"),
            }
        return {
            "verified": True,
            "verdict": "valid",
            "reason": "signature verifies against the sender's published key",
            "platform_said": block.get("state"),
        }
