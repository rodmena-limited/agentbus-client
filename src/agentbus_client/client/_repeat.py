from __future__ import annotations

from .errors import AgentBusError

_SERVER_ERROR_FIRST = 500
_SERVER_ERROR_LAST = 599


def _is_server_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, AgentBusError) and _SERVER_ERROR_FIRST <= exc.status <= _SERVER_ERROR_LAST
    )


def _safe_to_repeat(method: str, *, idempotent: bool) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"} or idempotent


def _unsafe_to_repeat(exc: BaseException, *, idempotent: bool) -> bool:
    return not idempotent and _is_server_error(exc)
