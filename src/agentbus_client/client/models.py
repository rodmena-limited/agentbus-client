"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# ------------------------------------------------------------------ models


@dataclass
class Delivery:
    """One message as delivered to one agent."""

    delivery_id: str
    seq: int
    subject: str
    sender: str
    state: str
    thread_id: str
    message_id: str
    labels: list[str] = field(default_factory=list)
    attachment_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Delivery:
        # SEV-4 (#234): agent_seq is a MONOTONIC POSITIVE integer per agent;
        # `.get("agent_seq") or 0` used to collapse 0 and None, which happens to
        # be harmless because seq starts at 1 — but the pattern was wrong for a
        # value where "missing" and "zero" are semantically different, and would
        # bite the day a server returned 0 for a legitimate first delivery.
        # Explicit None handling keeps the invariant readable.
        raw_seq = data.get("agent_seq")
        seq = int(raw_seq) if raw_seq is not None else 0
        return cls(
            delivery_id=data["delivery_id"],
            seq=seq,
            subject=data.get("subject") or "",
            sender=data.get("sender_display") or data.get("sender_address") or "",
            state=data.get("state") or "",
            thread_id=data.get("thread_id") or "",
            message_id=data.get("message_id") or "",
            labels=list(data.get("labels") or []),
            attachment_count=data.get("attachment_count") or 0,
            raw=data,
        )


#: REG-6 (round-3 audit): per-attachment size ceiling — the quick fix.
#: _encode_attachments buffers the raw file + its base64 form + the JSON body
#: + httpx's own copy = peak ~4-5x file size, doubled again on encrypted send
#: via _apply_seal. A 500 MB video used to OOM small VMs / containers before
#: the server was even reached. This cap FAILS FAST at the boundary with a
#: clear error, so a caller sees the size wall as a refusal rather than an
#: OOM traceback. The real fix (streaming base64/multipart upload) needs a
#: server change and is a separate follow-up. Override via env for genuine
#: large-file needs on hosts with the RAM to spend.
_DEFAULT_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB


def _max_attachment_bytes() -> int:
    raw = os.environ.get("AGENTBUS_MAX_ATTACHMENT_BYTES")
    if not raw:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_MAX_ATTACHMENT_BYTES
    return v if v > 0 else _DEFAULT_MAX_ATTACHMENT_BYTES


#: F7 (issuedb #4): the SERVER's per-attachment ceiling, applied BEFORE any
#: sealing or upload happens. Distinct from the RAM-safety cap above.
#:
#: Without this check the client streams the whole file through sealing and
#: the network before the server returns 413. A peer's repro of an 11 MiB
#: attachment took 53.7 s of wall time to hit the 10 MiB server ceiling —
#: pure waste on both sides, and the caller learns nothing until the very end.
#:
#: The server does not yet publish this cap machine-readably (backend #249
#: opens GET /v1/limits for it). When that lands, replace this constant with a
#: single cached fetch at startup and delete the env override — the server is
#: the authority. For now: hardcoded to match the documented 10 MiB value,
#: with an env override so an operator whose own server was reconfigured up
#: is not blocked by a stale client.
_DEFAULT_SERVER_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB, per docs

#: Encrypted-workspace attachment inflation. On an ENCRYPTED workspace the
#: server sees base64(age_armor(raw)) — double base64 (age armor, then JSON
#: transport), i.e. ~1.333^2 = 1.806x. Measured byte-exact on two seats
#: (macbook, ui; thread 01M0BGFKX4EE8WV6T68BTARGTE): 7,340,032 raw ->
#: 13,256,496 wire = 1.806x, 9,961,472 -> 17,990,808 = 1.806x. Deterministic
#: (age armor + base64 are both fixed-ratio), not content-dependent.
#:
#: So the effective encrypted per-attachment limit is ~10 MiB / 1.806 = 5.5 MiB
#: raw, NOT the documented 10 MiB. The pre-seal fast-reject uses this factor to
#: skip the expensive seal when the raw is clearly over; the post-seal exact
#: check is the authoritative backstop.
_SEAL_INFLATION_FACTOR = 1.806


def _server_max_attachment_bytes() -> int:
    raw = os.environ.get("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES")
    if not raw:
        return _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES
    return v if v > 0 else _DEFAULT_SERVER_MAX_ATTACHMENT_BYTES


# Ack-tracking (SPECS/0022): the ack-window cap the server enforces. The
# client enforces the same bound locally so a caller learns the limit fast
# rather than getting a 422 back from the server. Mirrors the agreed spec:
# "--ack-window MUST be bounded; server refuses > 168h (7 days)".
_ACK_WINDOW_MAX_SECONDS = 168 * 3600  # 7 days
_ACK_WINDOW_DEFAULT_SECONDS = 24 * 3600  # 24h, applied when require_ack is set


def _ack_window_seconds(ack_window: Any, *, default_when_set: bool) -> int | None:
    """Normalise an ack-window argument to seconds, or None when absent.

    Accepts a `datetime.timedelta` (the SDK's documented type) or an int
    (seconds). Returns None when the caller supplied neither — the caller
    then decides whether to apply the default (it does, when require_ack
    is set).

    The 168h server cap is enforced HERE too, so a caller gets a fast
    local error instead of a round-trip 422. The error names the cap in
    seconds and days, matching the spec's wording.
    """
    import datetime as _dt

    if ack_window is None:
        return _ACK_WINDOW_DEFAULT_SECONDS if default_when_set else None
    if isinstance(ack_window, _dt.timedelta):
        seconds = int(ack_window.total_seconds())
    elif isinstance(ack_window, bool):  # bool is an int subclass; refuse it
        raise ValueError("ack_window must be a timedelta or seconds, not a bool")
    elif isinstance(ack_window, int):
        seconds = ack_window
    else:
        raise ValueError(
            f"ack_window must be a datetime.timedelta or int seconds, got {type(ack_window).__name__}"
        )
    if seconds <= 0:
        raise ValueError("ack_window must be positive")
    if seconds > _ACK_WINDOW_MAX_SECONDS:
        raise ValueError(
            f"ack_window of {seconds}s exceeds the server cap of "
            f"{_ACK_WINDOW_MAX_SECONDS}s ({_ACK_WINDOW_MAX_SECONDS // 3600}h / 7 days); "
            "after that the sender should just re-send if it still matters"
        )
    return seconds
