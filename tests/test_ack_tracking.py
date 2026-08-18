"""Ack-tracking client surface (SPECS/0022).

Greenlit by Farshid. TO-only binding (never CC). Server owns the
delivery_reminders table, sweep, gate, reply-as-ack, give-up emitter.
Client owns the send-time flag, batch parity, and the (upcoming)
reminders verb.

These tests pin the FORWARD-COMPATIBLE send surface: --require-ack adds
`require_ack`/`ack_window_seconds` to the payload; a server that predates
ack-tracking ignores them, so the flag is safe to pass before the backend
ships.
"""

from __future__ import annotations

import argparse
import datetime as dt
from unittest.mock import patch

import pytest

from agentbus_client import cli as cli_module
from agentbus_client.client import AgentBus, AsyncAgentBus, _ack_window_seconds


# --------------------------------------------------------------- SDK


def test_sdk_send_require_ack_adds_fields():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    with patch.object(bus, "_request", side_effect=fake_request):
        bus.send(["peer"], "s", "b", require_ack=True)

    assert captured.get("require_ack") is True
    # Default 24h window applied when require_ack is set.
    assert captured.get("ack_window_seconds") == 24 * 3600


def test_sdk_send_require_ack_respects_explicit_window():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    with patch.object(bus, "_request", side_effect=fake_request):
        bus.send(["a"], "s", "b", require_ack=True, ack_window=dt.timedelta(hours=2))

    assert captured.get("ack_window_seconds") == 2 * 3600


def test_sdk_send_omit_require_ack_leaves_payload_clean():
    """Absence is not empty string. Without --require-ack, no require_ack
    or ack_window_seconds in the payload — old servers never see them."""
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    with patch.object(bus, "_request", side_effect=fake_request):
        bus.send(["a"], "s", "b")

    assert "require_ack" not in captured
    assert "ack_window_seconds" not in captured


def test_sdk_send_ack_window_capped_at_168h():
    """The 168h server cap is enforced client-side so a caller gets a fast
    local error instead of a round-trip 422."""
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    with pytest.raises(ValueError, match="168h"):
        bus.send(["a"], "s", "b", require_ack=True, ack_window=dt.timedelta(days=8))


def test_sdk_send_rejects_zero_or_negative_window():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    with pytest.raises(ValueError, match="positive"):
        bus.send(["a"], "s", "b", require_ack=True, ack_window=dt.timedelta(seconds=0))
    with pytest.raises(ValueError, match="positive"):
        bus.send(["a"], "s", "b", require_ack=True, ack_window=-60)


def test_sdk_send_rejects_bool_as_window():
    """bool is an int subclass; accepting it would let True==1s pass."""
    with pytest.raises(ValueError, match="not a bool"):
        _ack_window_seconds(True, default_when_set=True)


def test_ack_window_seconds_helper_direct():
    assert _ack_window_seconds(None, default_when_set=True) == 24 * 3600
    assert _ack_window_seconds(None, default_when_set=False) is None
    assert _ack_window_seconds(3600, default_when_set=True) == 3600
    assert _ack_window_seconds(dt.timedelta(minutes=90), default_when_set=True) == 5400


async def test_async_send_require_ack_parity():
    """Async must have parity — the drift that bit `phonebook(label=)`
    and `derived_from` before."""
    bus = AsyncAgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    async def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    with patch.object(bus, "_request", side_effect=fake_request):
        await bus.send(["a"], "s", "b", require_ack=True, ack_window=dt.timedelta(hours=3))

    assert captured.get("require_ack") is True
    assert captured.get("ack_window_seconds") == 3 * 3600


# ---------------------------------------------------------------- CLI duration parse


def test_parse_duration():
    import datetime as _dt

    assert cli_module._parse_duration("90m") == _dt.timedelta(minutes=90)
    assert cli_module._parse_duration("2h") == _dt.timedelta(hours=2)
    assert cli_module._parse_duration("3d") == _dt.timedelta(days=3)
    assert cli_module._parse_duration("3600") == _dt.timedelta(seconds=3600)


def test_parse_duration_invalid_raises_clear_error():
    with pytest.raises(ValueError, match="invalid duration"):
        cli_module._parse_duration("banana")
    with pytest.raises(ValueError, match="invalid duration"):
        cli_module._parse_duration("")


# ---------------------------------------------------------------- CLI send


def _send_args(**over):
    base = {
        "to": ["a"], "cc": None, "priority": None, "subject": "s", "body": "b",
        "attach": [], "require_available": False, "payload": None,
        "guarantee": None, "derived_from": [], "json": True,
        "require_ack": False, "ack_window": None, "agent": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_send_require_ack_passes_through(monkeypatch, capsys):
    captured: dict = {}

    class _Bus:
        agent = "me"

        def send(self, to, **kw):
            captured.update(kw)
            return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    monkeypatch.setattr(cli_module, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(cli_module, "_read_body", lambda b: b or "")
    cli_module.cmd_send(_send_args(require_ack=True, ack_window="2h"))

    assert captured.get("require_ack") is True
    # 2h parsed to a timedelta, then to seconds by the SDK (which we're
    # calling directly here, so it arrives as the timedelta).
    assert captured.get("ack_window") == dt.timedelta(hours=2)


def test_cli_send_no_require_ack_is_clean(monkeypatch):
    """Without --require-ack the CLI passes require_ack=False and
    ack_window=None. The SDK then omits both from the payload (proven by
    test_sdk_send_omit_require_ack_leaves_payload_clean), so old servers
    never see them."""
    captured: dict = {}

    class _Bus:
        def send(self, to, **kw):
            captured.update(kw)
            return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    monkeypatch.setattr(cli_module, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(cli_module, "_read_body", lambda b: b or "")
    cli_module.cmd_send(_send_args())

    assert captured.get("require_ack") is False
    assert captured.get("ack_window") is None


def test_cli_send_batch_require_ack_per_item(monkeypatch, capsys):
    captured: list[dict] = []

    class _Bus:
        def send(self, to, **kw):
            captured.append(kw)
            return {"id": "01M", "delivery_count": 1, "thread_id": "01T", "cc": []}

    import io
    import sys as _sys
    monkeypatch.setattr(cli_module, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(_sys, "stdin", io.StringIO(
        '{"to": "a", "subject": "x", "text": "y", "require_ack": true}\n'
        '{"to": "b", "subject": "x", "text": "y", "require_ack": true, "ack_window": "90m"}\n'
    ))
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False, raising=False)

    cli_module.cmd_send_batch(_args_())
    assert captured[0].get("require_ack") is True
    assert captured[1].get("require_ack") is True
    assert captured[1].get("ack_window") == dt.timedelta(minutes=90)


def _args_(**over):
    base = {"agent": None, "json": False, "stop_on_error": False}
    base.update(over)
    return argparse.Namespace(**base)
