"""Duration and instant coercion for the reminder surface.

SHARED BY BOTH CLIENT TWINS ON PURPOSE. `AgentBus.remind` and
`AsyncAgentBus.remind` must agree about what `--delay 2h` means down to the
second; that pair has drifted before on smaller details than this
(`phonebook(label=)` landed on one and not the other), and a scheduling
disagreement between them would surface as a reminder arriving at the wrong
time with nothing to point at.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any


def _duration_seconds(value: Any) -> int | None:
    """Coerce a duration to whole seconds. None passes through.

    Accepts what `_parse_duration` accepts (`90m`, `2h`, `3d`, bare seconds), a
    timedelta, or an int. Returns None for None so a caller can splat the result
    into a payload without deciding whether the key belongs there.
    """
    if value is None:
        return None
    if isinstance(value, _dt.timedelta):
        return int(value.total_seconds())
    if isinstance(value, bool):  # bool is an int subclass; refuse it explicitly
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int):
        return value
    from .cli._common import _parse_duration

    return int(_parse_duration(str(value)).total_seconds())


def _as_instant(value: Any) -> str | None:
    """Coerce an absolute time to a UTC ISO-8601 string. None passes through.

    ALWAYS UTC, AND ALWAYS EXPLICIT ABOUT IT. A naive datetime is read as local
    time and converted, rather than being sent as-is and interpreted as UTC by a
    server in another zone — an off-by-hours reminder is the kind of bug that
    looks like flakiness rather than a defect.

    Sub-second precision is PRESERVED. RunFlow honours `fire_at` exactly and
    truncating it fires early into a not-yet-due row, which their mail-api
    incident showed reads as a green run that accomplished nothing.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value  # already formatted by the caller; server validates
    if isinstance(value, _dt.datetime):
        moment = value.astimezone(_dt.timezone.utc) if value.tzinfo else value.astimezone()
        return moment.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    raise ValueError(f"expected a datetime or ISO-8601 string, got {type(value).__name__}")
