from __future__ import annotations

import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
import argparse
from unittest import mock

from agentbus_client.cli import cmd_watch


class MockBus:
    agent = "test-agent"
    base_url = "http://test"

    def whoami(self, agent: Any = None) -> dict[str, Any]:
        return {"agent": {"persona": "receiver-persona"}}


class MockCoalescer:
    def __init__(self, handler: Any, *args: Any, **kwargs: Any) -> None:
        self.handler = handler

    def close(self) -> None:
        pass

    def handle(self, message: str) -> None:
        self.handler(message)


def test_lane_overwrite() -> None:
    args = argparse.Namespace(
        agent="test-agent",
        wait=0,
        no_coalesce=False,
        coalesce_window=0,
        coalesce_quiet=0,
        exec="echo {lane}",
        append=None,
        state=None,
        once=False,
        daemon=False,
        cursor=None,
    )

    captured_messages: list[dict[str, Any]] = []

    def fake_notify_command(cmd: Any) -> Any:
        def handler(message: dict[str, Any]) -> None:
            captured_messages.append(message)

        return handler

    class FakeWatcher:
        def __init__(
            self, bus: Any, agent: str, *args: Any, on_message: Any = None, **kwargs: Any
        ) -> None:
            self.handler = on_message

        def run(self, *, once: bool = False) -> int:
            # Simulate receiving a message from the server with the SENDER'S persona
            self.handler({"delivery_id": "123", "lane": "sender-persona"})
            return 0

    with (
        mock.patch("agentbus_client.cli._common._bus", return_value=MockBus()),
        mock.patch("agentbus_client._coalesce.Coalescer", MockCoalescer),
        mock.patch("agentbus_client.watch.notify_command", fake_notify_command),
        mock.patch("agentbus_client.watch.Watcher", FakeWatcher),
    ):
        cmd_watch(args)

    if not captured_messages:
        print("FAIL: No messages captured")
        sys.exit(1)

    msg = captured_messages[0]
    print(f"Captured message lane: {msg.get('lane')}")

    if msg.get("lane") == "receiver-persona":
        print("FAIL: Receiver's persona overwrote the sender's persona!")
        sys.exit(1)
    elif msg.get("lane") == "sender-persona":
        print("PASS: Sender's persona was preserved.")
        sys.exit(0)
    else:
        print("FAIL: Unexpected lane value.")
        sys.exit(1)


if __name__ == "__main__":
    test_lane_overwrite()
