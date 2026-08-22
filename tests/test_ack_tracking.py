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
        "to": ["a"],
        "cc": None,
        "priority": None,
        "subject": "s",
        "body": "b",
        "attach": [],
        "require_available": False,
        "payload": None,
        "guarantee": None,
        "derived_from": [],
        "json": True,
        "require_ack": False,
        "ack_window": None,
        "agent": None,
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

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(cli_module._common, "_read_body", lambda b: b or "")
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

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(cli_module._common, "_read_body", lambda b: b or "")
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

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(
        _sys,
        "stdin",
        io.StringIO(
            '{"to": "a", "subject": "x", "text": "y", "require_ack": true}\n'
            '{"to": "b", "subject": "x", "text": "y", "require_ack": true, "ack_window": "90m"}\n'
        ),
    )
    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False, raising=False)

    cli_module.cmd_send_batch(_args_())
    assert captured[0].get("require_ack") is True
    assert captured[1].get("require_ack") is True
    assert captured[1].get("ack_window") == dt.timedelta(minutes=90)


def _args_(**over):
    base = {"agent": None, "json": False, "stop_on_error": False}
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- reminders verb


def _rem_args(**over):
    base = {"owed": False, "agent": None, "json": False}
    base.update(over)
    return argparse.Namespace(**base)


class _RemBus:
    """Deterministic reminders bus. `rows` returned by the requested view."""

    def __init__(self, owing=None, owed=None):
        self._owing = owing or []
        self._owed = owed or []

    def reminders_owing(self):
        return self._owing

    def reminders_owed(self):
        return self._owed


def _render(fn, args):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


def test_reminders_owing_renders_sender_view(monkeypatch):
    rows = [
        {
            "delivery_id": "01D1",
            "subject": "please confirm schema",
            "required_by": "2026-08-19T00:00:00Z",
            "attempts_so_far": 2,
            "last_attempt_at": "2026-08-18T00:05:00Z",
            "next_attempt_at": "2026-08-18T00:45:00Z",
            "thread_id": "01T",
            "recipient_name": "peer-b",
        }
    ]
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _RemBus(owing=rows))
    out = _render(cli_module.cmd_reminders, _rem_args(owed=False))
    assert "owing" in out
    assert "peer-b" in out
    assert "please confirm schema" in out
    assert "attempts=2" in out


def test_reminders_owed_shows_sender(monkeypatch):
    import contextlib
    import io

    rows = [
        {
            "delivery_id": "01D2",
            "subject": "rerun with these flags",
            "required_by": "2026-08-19T00:00:00Z",
            "attempts_so_far": 0,
            "last_attempt_at": None,
            "next_attempt_at": "2026-08-18T00:10:00Z",
            "thread_id": "01T",
            "sender_name": "peer-a",
        }
    ]

    class _B:
        def reminders_owed(self):
            return rows

        def reminders_owing(self):
            return []

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _B())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_module.cmd_reminders(_rem_args(owed=True))
    out = buf.getvalue()
    assert "owed" in out
    assert "peer-a" in out
    assert "rerun with these flags" in out


def test_reminders_json_output(monkeypatch):
    import contextlib
    import io
    import json as _json

    rows = [
        {
            "delivery_id": "01D",
            "subject": "sub",
            "required_by": "r",
            "attempts_so_far": 1,
            "last_attempt_at": None,
            "next_attempt_at": "n",
            "thread_id": "t",
            "sender_name": "peer",
        }
    ]

    class _B:
        def reminders_owed(self):
            return rows

        def reminders_owing(self):
            return []

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _B())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_module.cmd_reminders(_rem_args(owed=True, json=True))
    data = _json.loads(buf.getvalue())
    assert data["count"] == 1
    assert data["owed"][0]["subject"] == "sub"


def test_reminders_404_graceful(monkeypatch, capsys):
    from agentbus_client.client import AgentBusError

    class _B:
        def reminders_owing(self):
            raise AgentBusError("not found", status=404, code="not_found")

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _B())
    rc = cli_module.cmd_reminders(_rem_args(owed=False))
    assert rc == 1
    assert "not enabled on this server yet" in capsys.readouterr().err


def test_reminders_sdk_methods_hit_right_endpoints():
    from agentbus_client.client import AgentBus

    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    with patch.object(bus, "_request", return_value={"owing": [{"delivery_id": "x"}]}) as m:
        bus.reminders_owing()
    m.assert_called_once_with("GET", "/v1/reminders/owing")

    with patch.object(bus, "_request", return_value={"owed": [{"delivery_id": "x"}]}) as m:
        bus.reminders_owed()
    m.assert_called_once_with("GET", "/v1/reminders/owed")
