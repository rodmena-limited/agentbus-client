#!/usr/bin/env python3
"""CLAIM (resilience._run_with_resilience): the outer deadline (caller timeout + 5s) BOUNDS a
request. FALSIFIED 2026-08-20: the deadline returns control but the retry sequence keeps
running on a NON-DAEMON bulkman thread, so (1) the process cannot exit until it finishes and
(2) the 8-slot bulkhead fills with abandoned work, after which calls against a HEALTHY bus time
out too (a monitor stays deaf after connectivity returns)."""
import os, sys, subprocess, time, threading, json
os.environ["AGENTBUS_SDK_CB_FAILURE_LIMIT"] = "1000"   # the breaker is #24's probe; here we test the bulkhead/deadline
from _common import SRC, stalled_server, json_server, verdict
sp, _held = stalled_server()
hp, served = json_server(json.dumps({"agent": {"name": "probe"}, "unread": {"count": 0}}).encode())

# (1) exit blocking, in a child so we can time the whole interpreter
code = f'''
import sys, time; sys.path.insert(0, {SRC!r})
from agentbus_client.client import AgentBus
t = time.time(); b = AgentBus(api_key="ab_sk_probe", base_url="http://127.0.0.1:{sp}", agent="probe", timeout=2)
try: b.whoami()
except Exception as e: print("caller got %s at %.1fs" % (type(e).__name__, time.time()-t), flush=True)
print("main returned at %.1fs" % (time.time()-t), flush=True)
'''
t0 = time.time(); p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True); wall = time.time() - t0
print(p.stdout.strip().replace("\n", "; "), f"; process exited at {wall:.1f}s")
exit_blocked = wall > 9.0

# (2) clogged bulkhead: 8 stalled callers, then one call against the healthy server, same process
from agentbus_client.client import AgentBus, resilience
resilience._sdk_bulkhead(); resilience._sdk_safety_net()
down = AgentBus(api_key="ab_sk_probe", base_url=f"http://127.0.0.1:{sp}", agent="probe", timeout=2)
def f():
    try: down.whoami()
    except Exception: pass
th = [threading.Thread(target=f) for _ in range(8)]; [t.start() for t in th]; [t.join() for t in th]
ok = AgentBus(api_key="ab_sk_probe", base_url=f"http://127.0.0.1:{hp}", agent="probe", timeout=2)
t1 = time.time()
try:
    ok.whoami(); healthy_ok = True; print(f"healthy call OK after {time.time()-t1:.1f}s")
except Exception as e:
    healthy_ok = False; print(f"healthy call FAILED after {time.time()-t1:.1f}s: {type(e).__name__}; healthy server served {len(served)} requests")
raise SystemExit(verdict(not exit_blocked and healthy_ok,
    f"exit_blocked={exit_blocked} healthy_call_failed={not healthy_ok} — abandoned retries are not cancelled"))
