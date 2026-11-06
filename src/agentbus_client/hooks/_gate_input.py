from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

GUARD_FIELD_LIMIT = 4096
GUARD_DEPTH_LIMIT = 512
UNREADABLE_CALL = "unreadable_tool_call"
_GUARD_KEEP = 2000
_LARGEST_SETTING = 3600.0
_TOOL_NAME = re.compile(r'"tool_name"\s*:\s*("(?:[^"\\]|\\.)*")')


def fit_to_guard_limit(value: object, depth: int = 0) -> Any:  # noqa: ANN401
    if isinstance(value, str):
        if len(value) <= GUARD_FIELD_LIMIT:
            return value
        elided = len(value) - 2 * _GUARD_KEEP
        marker = f"\n...[{elided} characters elided by the AgentBus gate]...\n"
        return value[:_GUARD_KEEP] + marker + value[-_GUARD_KEEP:]
    if isinstance(value, (dict, list)) and depth >= GUARD_DEPTH_LIMIT:
        return f"...[nesting past {GUARD_DEPTH_LIMIT} levels elided by the AgentBus gate]..."
    if isinstance(value, dict):
        return {key: fit_to_guard_limit(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [fit_to_guard_limit(item, depth + 1) for item in value]
    return value


def _bus_reachable(base: str, timeout: float) -> tuple[bool, str]:
    """A bounded TCP connect to the bus — the cheapest possible "is the network there".

    urllib's single `timeout` covers connect AND read, so a dead network used to
    cost the full read budget per attempt, twice (peer review C5: 24s per tool
    call). A connect that fails in <= `timeout` answers the question the guard
    actually has — can we reach the bus at all — for a fraction of the cost.
    """
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        return False, f"AGENTBUS_BASE_URL {base!r} does not parse: {exc}"
    if not host:
        return False, f"no host in AGENTBUS_BASE_URL {base!r}"
    if parts.scheme not in ("http", "https"):
        return False, f"AGENTBUS_BASE_URL {base!r} is not an http or https URL"
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except (OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _tool_name_in(raw: str) -> str:
    found = _TOOL_NAME.search(raw)
    if found is None:
        return ""
    try:
        return str(json.loads(found.group(1)))
    except ValueError:
        return ""


def _read_tool_call() -> tuple[str, object]:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError) as exc:
        return "", {UNREADABLE_CALL: f"stdin could not be read: {type(exc).__name__}"}
    if not raw.strip():
        return "", None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return _tool_name_in(raw), {UNREADABLE_CALL: raw}
    if not isinstance(payload, dict):
        return "", {UNREADABLE_CALL: raw}
    return str(payload.get("tool_name") or ""), payload.get("tool_input")


def gate_setting(
    name: str, default: float, *, smallest: float, notes: list[str], whole: bool = False
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if smallest <= value <= _LARGEST_SETTING and (value.is_integer() or not whole):
        return value
    notes.append(f"{name}={raw!r} is not usable, so the gate used {default:g}.")
    return default
