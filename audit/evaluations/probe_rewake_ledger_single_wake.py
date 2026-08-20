#!/usr/bin/env python3
"""CLAIM (rewake.py): a delivery wakes the session ONCE (ledger-deduped). Checked 2026-08-20:
two armed monitors (previous turn's + this turn's) polling the same new mail. The seen-set is
loaded once at start and not re-read under the ledger lock."""
import os, sys, threading, tempfile
os.environ["AGENTBUS_REWAKE_WINDOW"] = "0"; os.environ["AGENTBUS_WAKE_DIR"] = tempfile.mkdtemp(); os.environ["AGENTBUS_AGENT"] = "probe"
from _common import SRC, verdict
from agentbus_client import rewake
import agentbus_client.hooks.claude_code as hooks
hooks._resolve_agent = lambda: "probe"
gate = threading.Barrier(2)
def poll(agent, wait=0):
    gate.wait(); return "  peer: hello  (agentbus show 01PROBE)"
rewake._unread_text = poll
rcs = []
th = [threading.Thread(target=lambda: rcs.append(rewake._monitor_inner())) for _ in range(2)]
[t.start() for t in th]; [t.join() for t in th]
print(f"two concurrent monitors, one new delivery -> exit codes {sorted(rcs)} (2 = wake)", file=sys.stderr)
raise SystemExit(verdict(rcs.count(2) == 1, "the same delivery woke the session twice"))
