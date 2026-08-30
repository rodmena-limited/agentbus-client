"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx

from .base import _Base
from .errors import (
    TransportError,
    _raise_for,
)
from .memory import SyncMemoryMixin
from .resilience import (
    _SYNC_CLIENTS,
    _run_with_resilience,
)
from .sync_block import SyncBlockMixin
from .sync_directory import SyncDirectoryMixin
from .sync_messaging import SyncMessagingMixin
from .sync_misc import SyncMiscMixin


class AgentBus(
    _Base,
    SyncMessagingMixin,
    SyncDirectoryMixin,
    SyncMiscMixin,
    SyncMemoryMixin,
    SyncBlockMixin,
):
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
        # Closed by the resilience layer's shutdown hook so an in-flight socket
        # read cannot hold the interpreter open (issuedb #26).
        _SYNC_CLIENTS.add(self._client)

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

        # THE BUDGET IS SHARED BY EVERY ATTEMPT (issuedb #26). The whole retry
        # sequence gets call_timeout + 5s; each attempt's own httpx timeout is
        # bounded by what is left, so no attempt can outlive the caller's
        # deadline and an abandoned sequence ends within one backoff sleep.
        call_timeout = kwargs.pop("timeout", None) or self.timeout
        budget = call_timeout + 5
        deadline = time.monotonic() + budget

        def _do_request() -> Any:
            remaining = deadline - time.monotonic()
            attempt_timeout = max(0.05, min(call_timeout, remaining))
            try:
                response = self._client.request(
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

        # REG-7: wrap every request in the resilience stack. Long-polls pass their
        # own timeout through so a bulkman queue-block never overrides the
        # caller's wait budget.
        if os.environ.get("AGENTBUS_SDK_RESILIENCE") == "0":
            # Explicit opt-out for a caller with its own resilience layer.
            return _do_request()
        # #45: retry a 5xx only when a repeat is SAFE. A GET/HEAD is safe by
        # definition; a mutating call is safe only when we minted an
        # idempotency key for it, because a 5xx says the server FAILED, not
        # that it did nothing. Without this, one `register` produced four 500s
        # and four chances to half-create an identity.
        safe_to_repeat = method.upper() in {"GET", "HEAD", "OPTIONS"} or idempotent
        return _run_with_resilience(_do_request, timeout=budget, idempotent=safe_to_repeat)
