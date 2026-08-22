"""`agentbus health <agent>` canary consumer (0.9.26).

Backend deployed GET /v1/agents/{name}/health (thread
01M08ZABM8B3N2VB1TV7R7J2ED, backend commit d6a38e3). Contract locked
byte-for-byte with the client's ask. These tests pin the client-side
half: SDK.health() call, CLI verb rendering, exit codes on stale state,
404 handling.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

from agentbus_client import cli as cli_module
from agentbus_client.client import AgentBus, AgentBusError

LIVE_RESPONSE = {
    "agent": "target-agent",
    "wake_channel_state": "live",
    "subscriber_count": 1,
    "last_seen_at": "2026-08-17T23:32:00.587657Z",
    "last_pong_at": "2026-08-17T23:29:50.787860Z",
    "last_stream_attached_at": "2026-08-17T23:31:40.529233Z",
    "last_stream_detached_at": None,
    "keepalive_age_seconds": 4,
    "watcher_alive": True,
    "capabilities": {"supports_canary_heartbeat": True},
}


STALE_RESPONSE = {
    **LIVE_RESPONSE,
    "wake_channel_state": "stale",
    "watcher_alive": False,
    "keepalive_age_seconds": 640,  # past the 600s stale threshold
}


# --------------------------------------------------------------- SDK


def test_sdk_health_hits_the_right_endpoint():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    with patch.object(bus, "_request", return_value=LIVE_RESPONSE) as m:
        result = bus.health("target-agent")

    m.assert_called_once_with("GET", "/v1/agents/target-agent/health", agent=None)
    assert result == LIVE_RESPONSE


def test_sdk_health_passes_agent_when_named():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    with patch.object(bus, "_request", return_value=LIVE_RESPONSE) as m:
        bus.health("target-agent", agent="acting-as")
    m.assert_called_once_with("GET", "/v1/agents/target-agent/health", agent="acting-as")


async def test_async_sdk_health_parity():
    """Async client must have parity — a caller cannot know which path is
    behind the SDK. pytest-asyncio's asyncio_mode=auto (see pyproject.toml)
    handles the event-loop fixture for us."""
    from agentbus_client.client import AsyncAgentBus

    bus = AsyncAgentBus(api_key="ab_sk_test_test", agent="me")

    async def fake_request(*a, **kw):
        assert a == ("GET", "/v1/agents/target-agent/health")
        return LIVE_RESPONSE

    with patch.object(bus, "_request", side_effect=fake_request):
        result = await bus.health("target-agent")
    assert result == LIVE_RESPONSE


# --------------------------------------------------------------- CLI


def _args(**over):
    base = {"target_agent": "target", "agent": None, "json": False}
    base.update(over)
    return argparse.Namespace(**base)


class _FakeBus:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.agent = "me"

    def health(self, target_agent, agent=None):
        if self._raises:
            raise self._raises
        return self._response


def test_cli_health_live_prints_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _FakeBus(LIVE_RESPONSE))
    rc = cli_module.cmd_health(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "wake_channel_state:       live" in out
    assert "watcher_alive:            True" in out
    assert "subscriber_count:         1" in out
    assert "keepalive_age_seconds:    4" in out
    assert "server supports canary heartbeat" in out


def test_cli_health_stale_prints_note_and_exits_nonzero(monkeypatch, capsys):
    """A stale wake_channel is the sender's actionable signal: even if
    presence reads 'responsive', a send would land in a queue nothing
    drains. The CLI must exit non-zero so scripts branching on `agentbus
    health X` see the failure."""
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _FakeBus(STALE_RESPONSE))
    rc = cli_module.cmd_health(_args())
    assert rc == 1
    out = capsys.readouterr().out
    assert "wake_channel_state:       stale" in out
    assert "require_responsive" in out  # actionable hint present


def test_cli_health_json_output(monkeypatch, capsys):
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _FakeBus(LIVE_RESPONSE))
    rc = cli_module.cmd_health(_args(json=True))
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data == LIVE_RESPONSE


def test_cli_health_404_prints_clear_error(monkeypatch, capsys):
    """Backend returns 404 for unknown agent in caller's workspace
    (existence undisclosed). The CLI must render that as an actionable
    error, not a raw traceback."""
    bus = _FakeBus(raises=AgentBusError("agent not found", status=404, code="not_found"))
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: bus)
    rc = cli_module.cmd_health(_args(target_agent="ghost"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown agent 'ghost'" in err


def test_cli_health_defaults_to_acting_agent(monkeypatch, capsys):
    """When no target is given, check ourselves — the common case for
    an operator running the command to verify their own watcher is
    healthy."""
    bus = _FakeBus(LIVE_RESPONSE)
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: bus)
    rc = cli_module.cmd_health(_args(target_agent=None))
    assert rc == 0
    out = capsys.readouterr().out
    # Target came from bus.agent ("me").
    assert "agent: me" in out


def test_cli_health_no_target_and_no_acting_agent_exits_two(monkeypatch, capsys):
    bus = _FakeBus(LIVE_RESPONSE)
    bus.agent = None
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: bus)
    rc = cli_module.cmd_health(_args(target_agent=None))
    assert rc == 2
    err = capsys.readouterr().err
    assert "no target agent" in err


def test_cli_health_verb_registered_in_parser():
    """A verb nobody can invoke is not a verb."""
    src = _cli_source()
    assert '"health"' in src or "'health'" in src
    assert "cmd_health" in src


def _cli_source() -> str:
    """The CLI is a package now (one module per command family): read all of it."""
    from pathlib import Path as _P

    return "".join(f.read_text() for f in sorted(_P(cli_module.__file__).parent.glob("*.py")))
