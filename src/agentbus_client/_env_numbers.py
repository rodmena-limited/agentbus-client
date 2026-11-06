from __future__ import annotations

import contextlib
import os
import sys
import threading

LONGEST_SECONDS = 31_536_000
_REPORTED: set[tuple[str, str]] = set()
_REPORTED_LOCK = threading.Lock()


def _report(name: str, raw: str, rule: str, remedy: str) -> None:
    with _REPORTED_LOCK:
        if (name, raw) in _REPORTED:
            return
        _REPORTED.add((name, raw))
    stream = sys.stderr
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        print(f"agentbus: {name}={raw!r} is not {rule}. {remedy}", file=stream)


def _remedy(used: object, wanted: str, default: object) -> str:
    return (
        f"The client is using {used}. Set it to {wanted}, or unset it for the default of {default}."
    )


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
    below: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        used = default
    else:
        if value < minimum:
            used = default if below is None else below
        elif maximum is None or value <= maximum:
            return value
        else:
            used = default
    if maximum is None:
        rule, wanted = f"a whole number of {minimum} or more", f"{minimum} or higher"
    else:
        rule, wanted = f"a whole number from {minimum} to {maximum}", "a value in that range"
    _report(name, raw, rule, _remedy(used, wanted, default))
    return used


def env_seconds(name: str, default: float, *, zero_allowed: bool = True) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if 0 <= value <= LONGEST_SECONDS and (zero_allowed or value > 0):
        return value
    lowest = "from 0 to" if zero_allowed else "above 0 and up to"
    rule = f"a number of seconds {lowest} {LONGEST_SECONDS}"
    _report(name, raw, rule, _remedy(f"{default:g}", "a value in that range", f"{default:g}"))
    return default
