from __future__ import annotations

import atexit
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or str(ROOT / "src")
PY = os.environ.get("PYTHON") or sys.executable
HOOK = (
    PY,
    "-c",
    "from agentbus_client.hooks.claude_code import main; raise SystemExit(main(['pre-tool-use']))",
)
mode = {"v": "down"}
hits: list[tuple[float, str, str]] = []


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        hits.append((time.time(), mode["v"], self.path))
        if mode["v"] == "down":
            body, code = b'{"detail":"down"}', 503
        else:
            body, code = (
                json.dumps(
                    {"decision": "deny", "reason": "GUARD-DENY rm -rf needs approval"}
                ).encode(),
                200,
            )
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
home = tempfile.mkdtemp(prefix="gate-repro-")
atexit.register(shutil.rmtree, home, ignore_errors=True)
atexit.register(srv.shutdown)
COOLDOWN = 5
env = {
    "PATH": "/usr/bin:/bin",
    "HOME": home,
    "PYTHONPATH": SRC,
    "AGENTBUS_CONFIG_DIR": f"{home}/.config/agentbus",
    "AGENTBUS_API_KEY": "ab_test_key",
    "AGENTBUS_AGENT": "repro-agent",
    "AGENTBUS_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
    "AGENTBUS_GATE_FAST_FAIL_COOLDOWN": str(COOLDOWN),
}
payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})


def call(label: Any) -> Any:
    before = len(hits)
    out = subprocess.run(
        HOOK,
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=60,
        check=False,
    )
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])["hookSpecificOutput"]
        dec, why = d["permissionDecision"], d["permissionDecisionReason"][:70]
    except Exception:  # noqa: BLE001
        dec, why = "UNPARSED", (out.stdout + out.stderr)[:200]
    print(
        f"t={time.time() - t0:5.1f}s {label:22} decision={dec:5} "
        f"server_hits+={len(hits) - before} | {why}",
        flush=True,
    )
    return dec


t0 = time.time()
print("== phase 1: guard returns 503")
for i in range(3):
    call(f"down #{i + 1}")
mode["v"] = "healthy-deny"
print(f"== phase 2: guard healthy, denies everything; calls every 2s (cooldown {COOLDOWN}s)")
decs = []
for i in range(8):
    decs.append(call(f"healthy #{i + 1}"))
    time.sleep(2)
print(f"== control: wait {COOLDOWN + 2}s with no calls, then call once")
time.sleep(COOLDOWN + 2)
ctrl = call("control after gap")
print(f"RESULT phase2 allow={decs.count('allow')}/8 deny={decs.count('deny')}/8 ; control={ctrl}")
if ctrl != "deny":
    print("HARNESS-FAILED: the control call did not reach the guard's deny; NOT a result")
    sys.exit(2)
if "deny" not in decs[3:]:
    print("FAIL: the circuit never re-probed the healthy guard during continuous calls (#75)")
    sys.exit(1)
print("PASS")
