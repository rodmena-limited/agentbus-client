"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from typing import Any

import httpx

from .async_block import AsyncBlockMixin
from .async_directory import AsyncDirectoryMixin
from .async_messaging import AsyncMessagingMixin
from .async_misc import AsyncMiscMixin
from .base import _Base
from .errors import (
    TransportError,
    _raise_for,
)
from .memory import AsyncMemoryMixin
from .resilience import (
    _async_circuit_breaker,
    _is_transient_sdk_error,
)


class AsyncAgentBus(
    _Base,
    AsyncMessagingMixin,
    AsyncDirectoryMixin,
    AsyncMiscMixin,
    AsyncMemoryMixin,
    AsyncBlockMixin,
):
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

        # Same shared budget as the sync client (issuedb #26): the sequence gets
        # call_timeout + 5s and every attempt is bounded by what is left.
        call_timeout = kwargs.pop("timeout", None) or self.timeout
        budget = call_timeout + 5
        deadline = time.monotonic() + budget

        async def _do_request() -> Any:
            remaining = deadline - time.monotonic()
            attempt_timeout = max(0.05, min(call_timeout, remaining))
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers=self._headers(agent, idempotent, idempotency_key),
                    timeout=attempt_timeout,
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise TransportError(str(exc)) from exc
            _raise_for(response)
            payload = response.json() if response.content else None
            self._capture_challenge(payload)
            return payload

        if os.environ.get("AGENTBUS_SDK_RESILIENCE") == "0":
            return await _do_request()
        return await self._run_with_resilience_async(_do_request, deadline=budget)

    async def _run_with_resilience_async(self, fn: Any, *, deadline: float | None = None) -> Any:
        """Retry-with-backoff + async bulkhead + breaker for async _request.

        Semantics mirror the sync `_run_with_resilience`: retries only fire for
        transient errors (transport / 503), non-transient errors pass through
        immediately, and one Semaphore slot covers the whole retry sequence so
        N callers never multiply their load during a bus deploy.

        Two additions close the async/sync gap (ticket #18):

        - CIRCUIT BREAKER. resilient_circuit (the sync breaker) is sync-only,
          so a hand-rolled per-process breaker lives here. It sees POST-RETRY
          outcomes (a whole failing retry-sequence counts as one failure);
          after `failure_limit` consecutive failing sequences it opens, and
          calls then FAIL FAST with no retry and no bulkhead slot until the
          cooldown lapses. Without it, a sustained outage left every async
          call retrying at full concurrency forever, with no memory that the
          bus was already down — unlike the sync client.
        - OUTER DEADLINE. `deadline` (call_timeout + 5, mirrored from the sync
          call site) bounds the whole retry sequence plus the bulkhead wait via
          asyncio.wait_for, so a choked call cannot retry behind the caller's
          own timeout indefinitely; it surfaces as a TransportError.

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

        breaker = _async_circuit_breaker()

        # REG-10: fetch (or create) the semaphore for THIS loop.
        loop = asyncio.get_running_loop()
        bulkheads: dict[int, asyncio.Semaphore] | None = getattr(
            self, "_async_bulkheads_by_loop", None
        )
        if bulkheads is None:
            bulkheads = {}
            self._async_bulkheads_by_loop: dict[int, asyncio.Semaphore] = bulkheads
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

        if not breaker.admit():
            # Fail fast — the bus has demonstrably been failing (or a half-open
            # probe is already in flight). A FRESH TransportError per caller, with
            # the last observed error as __cause__: re-raising one shared exception
            # instance across callers grew its traceback without bound (S9).
            raise TransportError(
                "agentbus SDK async circuit breaker is OPEN: recent calls to the bus "
                "failed, so the client is failing fast for the cooldown instead of "
                "retrying at full rate. Retry after the cooldown, or set "
                "AGENTBUS_SDK_RESILIENCE=0 to bypass the resilience layer."
            ) from breaker.last_error()
        verdict_given = False

        async def _sequence() -> Any:
            last_exc: BaseException | None = None
            async with sem:
                for attempt in range(max_retries + 1):
                    try:
                        result = await fn()
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
                    else:
                        nonlocal verdict_given
                        verdict_given = True
                        breaker.on_success()
                        return result
            assert last_exc is not None
            # The whole retry sequence failed transiently — record it against
            # the breaker so a sustained outage opens it and later calls fail fast.
            verdict_given = True
            breaker.on_failure(last_exc)
            raise last_exc

        try:
            if deadline is not None:
                try:
                    return await asyncio.wait_for(_sequence(), timeout=deadline)
                except asyncio.TimeoutError as exc:
                    # A deadline IS a failure of the bus to answer in time: count
                    # it, or a slow outage never opens the breaker (S9).
                    verdict_given = True
                    breaker.on_failure(exc)
                    raise TransportError(
                        f"agentbus SDK call did not complete within {deadline}s "
                        "(deadline = caller timeout + 5s; likely a transient "
                        "network stall)."
                    ) from exc
            return await _sequence()
        finally:
            if not verdict_given:
                breaker.release_probe()
