#!/usr/bin/env python3
"""CLAIM (watch.py module docstring): the watcher 'reconnects with backoff'. FALSIFIED 2026-08-20:
a stream the server ACCEPTS (200 text/event-stream) and then closes cleanly makes _stream_once
return normally; run() loops immediately — no sleep, no log line. Observed 217 reconnects in 12s."""

from __future__ import annotations

import contextlib
import io
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from _common import verdict

from agentbus_client.watch import Watcher

if TYPE_CHECKING:
    from agentbus_client.client import AgentBus

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(128)
port = srv.getsockname()[1]
opens: list[float] = []


def serve() -> None:
    while True:
        c, _ = srv.accept()

        def one(c: Any = c) -> None:
            try:
                c.recv(65536)
                opens.append(time.time())
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
                time.sleep(0.05)
            finally:
                c.close()

        threading.Thread(target=one, daemon=True).start()


threading.Thread(target=serve, daemon=True).start()


class Bus:
    api_key = "x"
    agent = "probe"
    base_url = f"http://127.0.0.1:{port}"

    def inbox(self, *a: Any, **k: Any) -> list[Any]:
        return []

    def whoami(self) -> dict[str, Any]:
        return {}


w = Watcher(cast("AgentBus", Bus()), "probe", on_message=lambda m: None)
err = io.StringIO()


def run() -> None:
    with contextlib.redirect_stderr(err):
        w.run()


threading.Thread(target=run, daemon=True).start()
time.sleep(8)
logged = sum(1 for line in err.getvalue().splitlines() if "retrying in" in line)
print(f"stream opens in 8s: {len(opens)}; backoff log lines: {logged}")
raise SystemExit(
    verdict(
        len(opens) <= 8,
        f"{len(opens)} reconnects in 8s with {logged} log lines — clean EOF bypasses backoff",
    )
)
