"""Typed sync and async clients for the AgentBus API."""
from __future__ import annotations

import concurrent.futures as _cf
import logging

_ConcurrentFuturesTimeout = _cf.TimeoutError
from typing import Any

import httpx

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

