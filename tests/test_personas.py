"""Personas client surface (SPECS/0021).

Three-way agreed design (client + backend + ui, thread
01M093JGVWQ2HXT3G6KJRG40T3). Farshid chose POLICY: the server enforces
the vocabulary and admin authority is required to assign a lane.

Backend shipped the column (commit e8f9cf7, migration
01M0982RBKD6M7J9ESTGYWA5RK). These tests pin the client surface that
consumes it.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

from agentbus_client import cli as cli_module
from agentbus_client.client import AgentBus, AsyncAgentBus

# --------------------------------------------------------------- SDK register


def test_sdk_register_passes_persona_in_payload():
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with patch.object(bus, "_request", side_effect=fake_request):
        bus.register("test-agent", persona="backend")

    assert captured.get("persona") == "backend"


def test_sdk_register_omits_persona_when_not_provided():
    """Absence is not the same as empty string. A None persona must NOT
    appear in the payload at all — the server treats a missing field and
    an empty field differently (missing = 'don't change', empty = 'clear
    it')."""
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with (
        patch.object(bus, "_request", side_async=fake_request)
        if False
        else patch.object(bus, "_request", side_effect=fake_request)
    ):
        bus.register("test-agent")

    assert "persona" not in captured


async def test_async_register_passes_persona():
    bus = AsyncAgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict = {}

    async def fake_request(method, path, json=None, **kw):
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with patch.object(bus, "_request", side_effect=fake_request):
        await bus.register("test-agent", persona="legal")
    assert captured.get("persona") == "legal"


# --------------------------------------------------------------- CLI register


def _register_args(**over):
    base = {
        "name": None,
        "label": None,
        "role": None,
        "workdir": None,
        "ephemeral": False,
        "repo_remote": None,
        "capability": [],
        "unlisted": False,
        "persona": None,
        "agent": None,
        "json": False,
        "api_key": None,
        "base_url": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_register_argparse_has_persona_flag():
    """The --persona flag is registered on the register subparser.
    Without this, the flag is unreachable from the CLI even though the
    SDK accepts it."""
    src = _cli_source()
    assert '"--persona"' in src
    assert 'metavar="LANE"' in src or "metavar='LANE'" in src


def test_cli_setup_argparse_has_persona_flag():
    """Same for the setup subparser — the primary onboarding path."""
    src = _cli_source()
    # setup's --persona is in a different block from register's
    assert src.count('"--persona"') >= 2


# --------------------------------------------------------------- whoami display


def test_whoami_displays_persona_when_present(monkeypatch, capsys):
    class _Bus:
        def whoami(self, agent=None):
            return {
                "workspace": {"slug": "test-ws"},
                "agent": {"name": "me", "persona": "backend"},
                "address": "me@x",
                "unread": {},
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_whoami(
        argparse.Namespace(json=False, qr=False, agent=None, api_key=None, base_url=None)
    )
    out = capsys.readouterr().out
    assert "persona:   backend" in out


def test_whoami_silent_when_persona_absent(monkeypatch, capsys):
    """Forward-compatible: old server returns no persona field. The line
    must not appear so the output is byte-identical to pre-persona."""

    class _Bus:
        def whoami(self, agent=None):
            return {
                "workspace": {"slug": "test-ws"},
                "agent": {"name": "me"},
                "address": "me@x",
                "unread": {},
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_whoami(
        argparse.Namespace(json=False, qr=False, agent=None, api_key=None, base_url=None)
    )
    out = capsys.readouterr().out
    assert "persona" not in out


# --------------------------------------------------------------- phonebook display


def test_phonebook_shows_persona_column_when_any_agent_has_one(monkeypatch, capsys):
    class _Bus:
        def phonebook(self, *a, **kw):
            return [
                {
                    "name": "alice",
                    "presence": "responsive",
                    "address": "a@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": "backend",
                },
                {
                    "name": "bob",
                    "presence": "idle",
                    "address": "b@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": None,
                },
            ]

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_phonebook(
        argparse.Namespace(
            query=None,
            capability=None,
            label=None,
            json=False,
            agent=None,
            api_key=None,
            base_url=None,
        )
    )
    out = capsys.readouterr().out
    assert "backend" in out
    # Bob has no persona — shows '-' not empty.
    lines = [line for line in out.splitlines() if "bob" in line.lower()]
    assert any("-" in line for line in lines)


def test_phonebook_no_persona_column_when_nobody_has_one(monkeypatch, capsys):
    """Forward-compatible: old server returns no persona for anyone. The
    column must not appear, so the layout is byte-identical to pre-persona."""

    class _Bus:
        def phonebook(self, *a, **kw):
            return [
                {
                    "name": "alice",
                    "presence": "responsive",
                    "address": "a@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": None,
                },
            ]

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_phonebook(
        argparse.Namespace(
            query=None,
            capability=None,
            label=None,
            json=False,
            agent=None,
            api_key=None,
            base_url=None,
        )
    )
    out = capsys.readouterr().out
    # The persona column header would show "backend" or similar; its absence
    # means no column was added. Verify by checking the line doesn't have
    # a trailing persona field (the address should be the last significant field).
    assert "persona" not in out.lower()


# --------------------------------------------------------------- lane in hook payload


def test_notify_command_substitutes_lane_placeholder():
    """The {lane} template placeholder must substitute. An empty lane
    (no persona) produces an empty string, not a KeyError."""
    import subprocess
    import unittest.mock as mock

    from agentbus_client import watch as watch_module

    calls: list[str] = []
    handler = watch_module.notify_command('echo "lane={lane}"')

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler({"subject": "s", "sender_display": "p", "lane": "backend"})
    assert "lane=backend" in calls[0]

    calls.clear()
    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler({"subject": "s", "sender_display": "p"})  # no lane key
    assert "lane=" in calls[0]  # empty, not KeyError


def _inject_body(sock_args):
    import json as _json
    import socket as _socket

    from agentbus_client.hooks import claude_code as hooks

    captured: list[bytes] = []

    class _FakeSock:
        def settimeout(self, s):
            pass

        def connect(self, addr):
            pass

        def sendall(self, data):
            captured.append(data)

        def close(self):
            pass

    with (
        patch.dict("os.environ", {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/fake"}),
        patch.object(_socket, "socket", return_value=_FakeSock()),
    ):
        hooks.inject(sock_args)
    assert captured, "no payload was sent to the socket"
    return _json.loads(captured[0])["message"]["content"]


def test_inject_uses_my_lane_for_the_reminder():
    """The handoff reminder must use the RECEIVER's own lane (my_lane),
    NOT the sender's lane — the SEV-2 fix.

    0.9.34 used the sender's lane, so a frontend sender messaging a backend
    receiver printed "Your lane is: frontend" — wrong for the receiver. The
    reminder must always say the receiving agent's lane."""
    args = argparse.Namespace(
        subject="test",
        sender="peer",
        delivery="01D",
        seq="",
        direction="bus",
        inbound_source=None,
        lane="frontend",
        my_lane="backend",
    )
    body = _inject_body(args)
    assert "Your lane is: backend" in body
    assert "HAND IT OFF" in body
    # The sender's lane is NOT what the reminder reports.
    assert "Your lane is: frontend" not in body


def test_inject_sender_lane_alone_does_not_trigger_reminder():
    """--lane (sender's persona) without --my-lane must NOT trigger the
    receiver's reminder. They are distinct; the sender's lane is not the
    receiver's lane."""
    args = argparse.Namespace(
        subject="test",
        sender="peer",
        delivery="01D",
        seq="",
        direction="bus",
        inbound_source=None,
        lane="frontend",
        my_lane=None,
    )
    body = _inject_body(args)
    assert "Your lane is" not in body


def test_inject_no_lane_no_reminder():
    args = argparse.Namespace(
        subject="test",
        sender="peer",
        delivery="01D",
        seq="",
        direction="bus",
        inbound_source=None,
        lane=None,
        my_lane=None,
    )
    body = _inject_body(args)
    assert "Your lane is" not in body


# --------------------------------------------------------------- lane collision


def test_watch_does_not_overwrite_sender_lane_with_acting_agent_persona(tmp_path):
    """cmd_watch must NOT clobber the server's `lane` (the SENDER's persona,
    #267) with the acting agent's own persona.

    Backend #267 enriched the wake/delivery with the SENDER's persona as a
    top-level `lane`. The 0.9.34 with_lane wrapper stamped the ACTING agent's
    persona from whoami() on top of it, so `{lane}` in an --exec template —
    and the hook's "Your lane is: ..." reminder — reported the receiver's
    lane instead of whoever actually wrote. Same field, two meanings.

    Known-positive control: the acting agent has persona "backend"; the
    message arrives carrying lane "frontend" (a frontend sender). The handler
    must deliver lane "frontend" unchanged."""

    from agentbus_client import watch as watch_module

    class MockBus:
        agent = "me"
        base_url = "http://test"

        def whoami(self, agent=None):
            # The acting agent's OWN persona — the value that used to win.
            return {"agent": {"name": "me", "persona": "backend"}}

    captured: list[dict] = []

    class FakeWatcher:
        def __init__(self, bus, agent, *args, on_message=None, **kwargs):
            self.handler = on_message

        def run(self, once=False):
            # A message arrives whose sender is a frontend-persona agent.
            self.handler({"delivery_id": "01D", "lane": "frontend"})
            return 0

    def fake_notify_command(cmd):
        def handler(message):
            captured.append(message)

        return handler

    state = tmp_path / "watch.json"
    args = argparse.Namespace(
        agent="me",
        wait=0,
        no_coalesce=False,
        coalesce_window=2500,
        coalesce_quiet=800,
        exec="echo {lane}",
        append=None,
        state=str(state),
        once=True,
        daemon=False,
        cursor=None,
        persona=None,
    )

    with (
        patch.object(cli_module._common, "_bus", return_value=MockBus()),
        patch.object(cli_module, "_watch_pidfile", return_value=tmp_path / "pid"),
        patch.object(watch_module, "notify_command", fake_notify_command),
        patch.object(watch_module, "Watcher", FakeWatcher),
    ):
        cli_module.cmd_watch(args)

    assert captured, "the wake handler never delivered the message"
    delivered = captured[0]
    # The acting agent is a backend persona; the sender is frontend. `lane`
    # must report the SENDER, never the receiver — the #267 contract.
    assert delivered.get("lane") == "frontend", (
        "the acting agent's persona overwrote the sender's lane — {lane} in "
        "an --exec template (and the hook reminder) reports the WRONG agent"
    )


def test_watch_injects_my_lane_without_clobbering_sender_lane(tmp_path):
    """SEV-2 companion to the regression test: `with_my_lane` adds the
    acting agent's OWN persona as `my_lane`, and MUST NOT touch the
    server's `lane` (the sender's persona). Both facts must be present
    on the delivered message — the sender's lane intact AND the
    receiver's my_lane added."""
    from agentbus_client import watch as watch_module

    class MockBus:
        agent = "me"
        base_url = "http://test"

        def whoami(self, agent=None):
            return {"agent": {"name": "me", "persona": "backend"}}

    captured: list[dict] = []

    class FakeWatcher:
        def __init__(self, bus, agent, *a, on_message=None, **kw):
            self.handler = on_message

        def run(self, once=False):
            self.handler({"delivery_id": "01D", "lane": "frontend"})
            return 0

    def fake_notify_command(cmd):
        def handler(message):
            captured.append(message)

        return handler

    state = tmp_path / "watch.json"
    args = argparse.Namespace(
        agent="me",
        wait=0,
        no_coalesce=False,
        coalesce_window=2500,
        coalesce_quiet=800,
        exec="echo {lane}",
        append=None,
        state=str(state),
        once=True,
        daemon=False,
        cursor=None,
        persona=None,
    )

    with (
        patch.object(cli_module._common, "_bus", return_value=MockBus()),
        patch.object(cli_module, "_watch_pidfile", return_value=tmp_path / "pid"),
        patch.object(watch_module, "notify_command", fake_notify_command),
        patch.object(watch_module, "Watcher", FakeWatcher),
    ):
        cli_module.cmd_watch(args)

    delivered = captured[0]
    # Sender's lane (frontend) preserved — the #267 contract.
    assert delivered.get("lane") == "frontend", "sender lane was clobbered"
    # Receiver's own lane (backend) present as my_lane — the SEV-2 fix.
    assert delivered.get("my_lane") == "backend", "my_lane was not injected"


def _cli_source() -> str:
    """The CLI is a package now (one module per command family): read all of it."""
    from pathlib import Path as _P

    return "".join(f.read_text() for f in sorted(_P(cli_module.__file__).parent.glob("*.py")))
