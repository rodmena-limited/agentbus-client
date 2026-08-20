#!/usr/bin/env python3
"""CLAIM (resilience.py, SPECS/0021): after N consecutive failing retry-sequences the SYNC
breaker OPENS and callers fail fast. Also: a caller hitting an OPEN breaker still sees an
AgentBusError. FALSIFIED 2026-08-20 (review #23): SafetyNet puts the breaker OUTERMOST, so it
only ever sees RetryLimitReached, which _is_transient_sdk_error does not recognise -> the
breaker records a SUCCESS; and Fraction(n, n) == 1/1 (1-slot window). The rewake poll breaker
has the identical defect."""
import os
os.environ["AGENTBUS_SDK_MAX_RETRIES"] = "0"
from _common import SRC, verdict  # noqa: E402
import resilient_circuit.retry as _rr
_rr.sleep = lambda *_a, **_k: None   # classification is under test, not timing
import httpx
from agentbus_client.client import AgentBus, AgentBusError, resilience

class Down:
    def __init__(s): s.calls = 0
    def request(s, *a, **k): s.calls += 1; raise httpx.ConnectError("down")
    def close(s): pass

bus = AgentBus(api_key="ab_sk_probe", base_url="https://probe.invalid", agent="probe")
bus._client = Down(); resilience._SDK_BULKHEAD = None; resilience._SDK_SAFETY_NET = None
for _ in range(10):
    try: bus._request("GET", "/v1/whoami")
    except Exception: pass
cb = resilience._SDK_SAFETY_NET.policies[0]
print(f"sync breaker after 10 failing sequences: {cb.status.value}; window={cb.execution_log.size}; wire calls={bus._client.calls}")
opened = cb.status.value == "OPEN" and cb.execution_log.size >= 2

from resilient_circuit.circuit_breaker import CircuitStatus
cb.status = CircuitStatus.OPEN
typed = False
try: bus._request("GET", "/v1/whoami")
except Exception as e:
    typed = isinstance(e, AgentBusError)
    print(f"open-breaker exception: {type(e).__module__}.{type(e).__name__} str={str(e)!r} AgentBusError={typed}")

import resilient_circuit as rc
cap = {}
class Spy(rc.SafetyNet):
    def __init__(self, *a, **k): super().__init__(*a, **k); cap["net"] = self
rc.SafetyNet = Spy
from agentbus_client import rewake
rewake._unread_text = lambda agent, wait=0: (_ for _ in ()).throw(ConnectionError("wifi off"))
poll = rewake._build_resilient_poll("probe", wait=0)
for _ in range(8): poll()
rb = cap["net"].policies[0]
print(f"rewake breaker after 8 failing polls: {rb.status.value}; window={rb.execution_log.size}")
raise SystemExit(verdict(opened and typed and rb.status.value == "OPEN",
    "breaker never opens (RetryLimitReached classified as success; Fraction(n,n) is a 1-slot window) "
    "and/or an open breaker leaks resilient_circuit.ProtectedCallError to callers"))
