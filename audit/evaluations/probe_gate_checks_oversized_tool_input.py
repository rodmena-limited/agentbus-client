#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or str(REPO / "src")
PY = os.environ.get("PYTHON") or str(REPO / ".venv" / "bin" / "python")
LIMIT = 4096


def longest(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return max((longest(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return max((longest(v) for v in value), default=0)
    return 0


class Guard(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if longest(body.get("tool_input")) > LIMIT:
            self._send(422, {"code": "validation_error", "detail": f"a field is over the {LIMIT}"})
        elif "rm -rf /" in json.dumps(body.get("tool_input")):
            self._send(200, {"decision": "deny", "reason": "destructive command needs approval"})
        else:
            self._send(200, {"decision": "allow", "reason": "permitted"})

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def decide(base_url: str, command: str, home: str) -> dict:
    env = {
        "HOME": home,
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": SRC,
        "AGENTBUS_API_KEY": "ab_sk_probe",
        "AGENTBUS_AGENT": "probe-agent",
        "AGENTBUS_BASE_URL": base_url,
        "AGENTBUS_CONFIG_DIR": os.path.join(home, ".config", "agentbus"),
    }
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    code = "import sys; from agentbus_client.hooks.claude_code import main; sys.argv=['agentbus-hook','pre-tool-use']; raise SystemExit(main())"
    out = subprocess.run(
        [PY, "-c", code], input=stdin, capture_output=True, text=True, env=env, timeout=60
    ).stdout
    line = next(chunk for chunk in out.splitlines() if "hookSpecificOutput" in chunk)
    return json.loads(line)["hookSpecificOutput"]


def main() -> int:
    import tempfile

    server = HTTPServer(("127.0.0.1", 0), Guard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    home = tempfile.mkdtemp()
    padded = decide(base, "echo " + "x" * 10_000 + " ; rm -rf /", home)
    small = decide(base, "rm -rf /", home)
    harmless = decide(base, "echo " + "y" * 20_000, home)
    ok = True
    for label, got, want in (
        ("padded destructive command", padded, "deny"),
        ("small destructive command", small, "deny"),
        ("large harmless command", harmless, "allow"),
    ):
        good = (
            got["permissionDecision"] == want and "UNVETTED" not in got["permissionDecisionReason"]
        )
        ok &= good
        print(
            f"  {'ok  ' if good else 'FAIL'}    {label}: {got['permissionDecision']} ({got['permissionDecisionReason'][:70]})"
        )
    server.shutdown()
    if os.environ.get("AUDIT_ALLOW_LIVE") == "1":
        sys.path.insert(0, SRC)
        import urllib.request

        from agentbus_client.hooks._gate import fit_to_guard_limit

        data = json.dumps(
            {"tool_name": "Read", "tool_input": fit_to_guard_limit({"note": "a" * 10_000})}
        ).encode()
        request = urllib.request.Request(
            (os.environ.get("AGENTBUS_BASE_URL") or "https://agentbus.rodmena.co.uk")
            + "/v1/guard/check",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['AGENTBUS_API_KEY']}",
                "X-AgentBus-Agent": os.environ["AGENTBUS_AGENT"],
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            live = response.status == 200
        ok &= live
        print(
            f"  {'ok  ' if live else 'FAIL'}    live guard accepts the shortened payload (HTTP 200)"
        )
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
