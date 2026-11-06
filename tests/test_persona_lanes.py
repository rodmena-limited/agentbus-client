from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agentbus_client import cli as cli_module

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------- lane in hook payload


def test_notify_command_substitutes_lane_placeholder() -> None:
    """The {lane} template placeholder must substitute. An empty lane
    (no persona) produces an empty string, not a KeyError."""
    import subprocess
    from unittest import mock

    from agentbus_client import watch as watch_module

    calls: list[str] = []
    handler = watch_module.notify_command('echo "lane={lane}"')

    def fake_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler({"subject": "s", "sender_display": "p", "lane": "backend"})
    assert "lane=backend" in calls[0]

    calls.clear()
    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler({"subject": "s", "sender_display": "p"})  # no lane key
    assert "lane=" in calls[0]  # empty, not KeyError


def _inject_body(sock_args: Any) -> Any:
    import json as _json
    import socket as _socket

    from agentbus_client.hooks import claude_code as hooks

    captured: list[bytes] = []

    class _FakeSock:
        def settimeout(self, s: Any) -> None:
            pass

        def connect(self, addr: Any) -> None:
            pass

        def sendall(self, data: Any) -> None:
            captured.append(data)

        def close(self) -> None:
            pass

    with (
        patch.dict("os.environ", {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/fake"}),  # noqa: S108
        patch.object(_socket, "socket", return_value=_FakeSock()),
    ):
        hooks.inject(sock_args)
    assert captured, "no payload was sent to the socket"
    return _json.loads(captured[0])["message"]["content"]


def test_inject_uses_my_lane_for_the_reminder() -> None:
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


def test_inject_sender_lane_alone_does_not_trigger_reminder() -> None:
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


def test_inject_no_lane_no_reminder() -> None:
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


def test_watch_does_not_overwrite_sender_lane_with_acting_agent_persona(tmp_path: Path) -> None:
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

        def whoami(self, agent: Any = None) -> dict[str, Any]:
            # The acting agent's OWN persona — the value that used to win.
            return {"agent": {"name": "me", "persona": "backend"}}

    captured: list[dict[str, Any]] = []

    class FakeWatcher:
        def __init__(
            self, bus: Any, agent: str, *args: Any, on_message: Any = None, **kwargs: Any
        ) -> None:
            self.handler = on_message

        def run(self, *, once: bool = False) -> int:
            # A message arrives whose sender is a frontend-persona agent.
            self.handler({"delivery_id": "01D", "lane": "frontend"})
            return 0

    def fake_notify_command(cmd: Any) -> Any:
        def handler(message: dict[str, Any]) -> None:
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


def test_watch_injects_my_lane_without_clobbering_sender_lane(tmp_path: Path) -> None:
    """SEV-2 companion to the regression test: `with_my_lane` adds the
    acting agent's OWN persona as `my_lane`, and MUST NOT touch the
    server's `lane` (the sender's persona). Both facts must be present
    on the delivered message — the sender's lane intact AND the
    receiver's my_lane added."""
    from agentbus_client import watch as watch_module

    class MockBus:
        agent = "me"
        base_url = "http://test"

        def whoami(self, agent: Any = None) -> dict[str, Any]:
            return {"agent": {"name": "me", "persona": "backend"}}

    captured: list[dict[str, Any]] = []

    class FakeWatcher:
        def __init__(
            self, bus: Any, agent: str, *a: Any, on_message: Any = None, **kw: Any
        ) -> None:
            self.handler = on_message

        def run(self, *, once: bool = False) -> int:
            self.handler({"delivery_id": "01D", "lane": "frontend"})
            return 0

    def fake_notify_command(cmd: Any) -> Any:
        def handler(message: dict[str, Any]) -> None:
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
