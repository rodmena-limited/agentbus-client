#!/usr/bin/env python3
"""CLAIM (rewake.py): a delivery wakes the session ONCE (ledger-deduped). Checked 2026-08-20:
two armed monitors (previous turn's + this turn's) polling the same new mail. The seen-set is
loaded once at start and not re-read under the ledger lock."""

import os
import sys
import tempfile
import threading

os.environ["AGENTBUS_REWAKE_WINDOW"] = "0"
os.environ["AGENTBUS_WAKE_DIR"] = tempfile.mkdtemp()
os.environ["AGENTBUS_AGENT"] = "probe"
from _common import verdict

import agentbus_client.hooks.claude_code as hooks
from agentbus_client import rewake

hooks._resolve_agent = lambda: "probe"
gate = threading.Barrier(2)


def poll(agent: str, wait: int = 0) -> str:
    gate.wait()
    return "  peer: hello  (agentbus show 01PROBE)"


rewake._unread_text = poll
rcs: list[int] = []
th = [threading.Thread(target=lambda: rcs.append(rewake._monitor_inner())) for _ in range(2)]
for t in th:
    t.start()
for t in th:
    t.join()
print(
    f"two concurrent monitors, one new delivery -> exit codes {sorted(rcs)} (2 = wake)",
    file=sys.stderr,
)
raise SystemExit(verdict(rcs.count(2) == 1, "the same delivery woke the session twice"))
