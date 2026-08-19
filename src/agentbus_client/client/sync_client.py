"""Typed sync and async clients for the AgentBus API."""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import os
import uuid

_ConcurrentFuturesTimeout = _cf.TimeoutError
from typing import Any

import httpx

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://agentbus.rodmena.co.uk"

from .base import _Base
from .errors import (
        TransportError,
        _raise_for,
)
from .resilience import (
        _run_with_resilience,
)
from .sync_directory import SyncDirectoryMixin
from .sync_messaging import SyncMessagingMixin
from .sync_misc import SyncMiscMixin


class AgentBus(_Base, SyncMessagingMixin, SyncDirectoryMixin, SyncMiscMixin):
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

