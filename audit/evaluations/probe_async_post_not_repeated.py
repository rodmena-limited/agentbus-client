#!/usr/bin/env python3
"""#91: a non-idempotent POST reaches the server once on a 5xx, on both clients.

Real HTTP to a local server that answers 503 and counts. On 0.9.105 and 0.9.106 the
async client makes 4 attempts. A GET must still be repeated, so that "one attempt"
cannot pass just because retry was switched off.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import _common  # noqa: F401

from agentbus_client.client import AgentBus, AsyncAgentBus
from agentbus_client.client.errors import AgentBusError

seen: list[str] = []


class Failing(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _fail(self) -> None:
        seen.append(self.command)
        body = json.dumps({"code": "unavailable", "detail": "probe"}).encode()
        self.send_response(503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _fail  # noqa: N815
    do_POST = _fail  # noqa: N815


def count_sync(url: str, method: str) -> int:
    seen.clear()
    bus = AgentBus(api_key="ab_sk_probe", base_url=url)
    with contextlib.suppress(AgentBusError):
        bus._request(method, "/v1/probe")
    return len(seen)


def count_async(url: str, method: str) -> int:
    seen.clear()

    async def once() -> None:
        async with AsyncAgentBus(api_key="ab_sk_probe", base_url=url) as bus:
            with contextlib.suppress(AgentBusError):
                await bus._request(method, "/v1/probe")

    asyncio.run(once())
    return len(seen)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Failing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    results = {
        "sync POST": (count_sync(url, "POST"), 1),
        "async POST": (count_async(url, "POST"), 1),
        "async GET": (count_async(url, "GET"), 4),
    }
    server.shutdown()
    failed = 0
    for label, (got, want) in results.items():
        good = got == want
        failed += 0 if good else 1
        print(f"{'ok  ' if good else 'FAIL'} {label} attempts={got} want={want}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
