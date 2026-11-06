from __future__ import annotations

import os
import sys

from .errors import AgentBusError

_SERVER_ERROR_FIRST = 500
_SERVER_ERROR_LAST = 599
_DEFAULT_MAX_RETRIES = 3
_REPORTED_RETRY_VALUES: set[str] = set()


def _is_server_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, AgentBusError) and _SERVER_ERROR_FIRST <= exc.status <= _SERVER_ERROR_LAST
    )


def _safe_to_repeat(method: str, *, idempotent: bool) -> bool:
    return method.upper() in {"GET", "HEAD", "OPTIONS"} or idempotent


def _unsafe_to_repeat(exc: BaseException, *, idempotent: bool) -> bool:
    return not idempotent and _is_server_error(exc)


def _report_retry_value(raw: str, used: int) -> None:
    if raw in _REPORTED_RETRY_VALUES:
        return
    _REPORTED_RETRY_VALUES.add(raw)
    print(  # noqa: T201
        f"agentbus: AGENTBUS_SDK_MAX_RETRIES={raw!r} is not a whole number of 0 or more. "
        f"The client is using {used}. Set it to 0 or higher, or unset it for the default "
        f"of {_DEFAULT_MAX_RETRIES}.",
        file=sys.stderr,
    )


def _sdk_max_retries() -> int:
    raw = os.environ.get("AGENTBUS_SDK_MAX_RETRIES")
    if raw is None:
        return _DEFAULT_MAX_RETRIES
    try:
        value = int(raw)
    except ValueError:
        _report_retry_value(raw, _DEFAULT_MAX_RETRIES)
        return _DEFAULT_MAX_RETRIES
    if value < 0:
        _report_retry_value(raw, 0)
        return 0
    return value
