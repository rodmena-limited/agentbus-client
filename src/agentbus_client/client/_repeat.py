from __future__ import annotations

import httpx

from agentbus_client._env_numbers import env_int, env_seconds

from .errors import AgentBusError

_SERVER_ERROR_FIRST = 500
_SERVER_ERROR_LAST = 599
_DEFAULT_MAX_RETRIES = 3


def _is_server_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, AgentBusError) and _SERVER_ERROR_FIRST <= exc.status <= _SERVER_ERROR_LAST
    )


def _safe_to_repeat(method: str, *, idempotent: bool) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"} or idempotent


def _unsafe_to_repeat(exc: BaseException, *, idempotent: bool) -> bool:
    return not idempotent and _is_server_error(exc)


def _sdk_max_retries() -> int:
    return env_int("AGENTBUS_SDK_MAX_RETRIES", _DEFAULT_MAX_RETRIES, minimum=0, below=0)


def _sdk_max_concurrent() -> int:
    return env_int("AGENTBUS_SDK_MAX_CONCURRENT", 8)


def _connection_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=env_int("AGENTBUS_MAX_CONNECTIONS", 20),
        max_keepalive_connections=env_int("AGENTBUS_MAX_KEEPALIVE_CONNECTIONS", 10, minimum=0),
        keepalive_expiry=env_seconds("AGENTBUS_KEEPALIVE_EXPIRY", 30.0),
    )
