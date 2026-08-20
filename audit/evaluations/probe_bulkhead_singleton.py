#!/usr/bin/env python3
"""CLAIM (resilience._sdk_bulkhead docstring): 'one concurrency lane for the whole SDK per
process'. FALSIFIED 2026-08-20: the lazy init is unlocked; 16 concurrent first callers received
15 distinct BulkheadThreading instances (15 thread pools)."""
import threading
from _common import SRC, verdict
from agentbus_client.client import resilience
worst = 0
for _ in range(20):
    resilience._SDK_BULKHEAD = None
    ids, gate = set(), threading.Barrier(16)
    def go():
        gate.wait(); ids.add(id(resilience._sdk_bulkhead()))
    th = [threading.Thread(target=go) for _ in range(16)]; [t.start() for t in th]; [t.join() for t in th]
    worst = max(worst, len(ids))
print(f"max distinct bulkheads handed to 16 concurrent first callers over 20 rounds: {worst}")
raise SystemExit(verdict(worst == 1, "lazy singleton races; concurrency cap not enforced"))
