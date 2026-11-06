#!/usr/bin/env python3
"""#90: the gate gives a decision on every input, and the guard makes it.

Real subprocess, real HTTP, a local guard that denies everything. On 0.9.105 and
0.9.106 nesting of 1100 and above, and a bad setting, exit 1 with no decision and the
guard is never asked. Run against those with AGENTBUS_CLIENT_SRC to see it fail.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or str(REPO / "src")
PY = os.environ.get("PYTHON") or sys.executable
HOOK = (
    "from agentbus_client.hooks.claude_code import main; raise SystemExit(main(['pre-tool-use']))"
)
SETTINGS = {
    "AGENTBUS_GATE_TIMEOUT": "abc",
    "AGENTBUS_GATE_CONNECT_TIMEOUT": "abc",
    "AGENTBUS_GATE_FAST_FAIL_AFTER": "2.5",
    "AGENTBUS_GATE_FAST_FAIL_COOLDOWN": "nan",
}
hits: list[str] = []


class Guard(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        hits.append(self.path)
        body = json.dumps({"decision": "deny", "reason": "PROBE-DENY"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def nested(depth: int) -> str:
    deep = "[" * depth + "]" * depth
    return '{"tool_name":"Mcp","tool_input":{"command":"ls","x":' + deep + "}}"


def run(env: dict[str, str], payload: str, cwd: str) -> tuple[int, str, int]:
    before = len(hits)
    done = subprocess.run(
        [PY, "-c", HOOK],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
        check=False,
    )
    try:
        last = json.loads(done.stdout.strip().splitlines()[-1])
        decision = last["hookSpecificOutput"]["permissionDecision"]
    except (IndexError, KeyError, ValueError):
        decision = "NONE"
    return done.returncode, decision, len(hits) - before


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Guard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tempfile.mkdtemp(prefix="probe-gate-depth-")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "PYTHONPATH": SRC,
        "AGENTBUS_CONFIG_DIR": f"{home}/cfg",
        "AGENTBUS_API_KEY": "ab_sk_probe",
        "AGENTBUS_AGENT": "probe-agent",
        "AGENTBUS_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
    }
    plain = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    cases: list[tuple[str, dict[str, str], str]] = [("control plain", env, plain)]
    cases += [(f"nesting {d}", env, nested(d)) for d in (50, 900, 1100, 5000, 200_000)]
    cases.append(("not json", env, "{not json"))
    cases += [(f"{k}={v}", {**env, k: v}, plain) for k, v in SETTINGS.items()]
    failed = 0
    try:
        for label, case_env, payload in cases:
            code, decision, asked = run(case_env, payload, home)
            good = code == 0 and decision == "deny" and asked == 1
            failed += 0 if good else 1
            print(f"{'ok  ' if good else 'FAIL'} {label:40} exit={code} {decision} asked={asked}")
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    print(f"{len(cases) - failed} of {len(cases)} reached the guard and took its deny")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
