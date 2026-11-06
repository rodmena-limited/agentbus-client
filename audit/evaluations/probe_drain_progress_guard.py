#!/usr/bin/env python3
"""CLAIM (watch.Watcher._drain): a drain terminates. FALSIFIED 2026-08-20: a batch whose seqs do
not advance the cursor (agent_seq null/0 or <= cursor) loops forever at full speed while holding
_drain_lock, so _drain_async becomes a permanent no-op and the watcher is RUNNING but deaf."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, cast

from _common import verdict

from agentbus_client.client.models import Delivery
from agentbus_client.watch import Watcher

if TYPE_CHECKING:
    from agentbus_client.client import AgentBus


class Bus:
    api_key = "x"
    base_url = "http://probe.invalid"
    agent = "probe"

    def __init__(self) -> None:
        self.calls = 0

    def inbox(self, cursor: Any, limit: int = 100, agent: Any = None) -> list[Any]:
        self.calls += 1
        return [Delivery.from_api({"delivery_id": "d1", "agent_seq": None, "subject": "s"})]


bus = Bus()
w = Watcher(cast("AgentBus", bus), "probe", on_message=lambda m: None, cursor=5)
done = threading.Event()


def run() -> None:
    try:
        w._drain()
    finally:
        done.set()


threading.Thread(target=run, daemon=True).start()
time.sleep(1.5)
print(f"drain returned={done.is_set()} inbox calls in 1.5s={bus.calls}")
raise SystemExit(verdict(done.is_set(), "no progress guard: hot loop against the server"))
