#!/bin/sh
# CLAIM (cli.py watch-status/watch-stop): a pidfile names a live agentbus watcher. FALSIFIED
# 2026-08-20: only os.kill(pid, 0) is checked, so after PID reuse (e.g. a reboot) watch-status
# reports a foreign process as RUNNING and watch-stop SIGTERMs it. SAFE: the victim is a `sleep`
# this probe starts. AGENTBUS_BIN selects the CLI under test.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BIN="${AGENTBUS_BIN:-$HERE/../../.venv/bin/agentbus}"; [ -x "$BIN" ] || BIN=agentbus
export AGENTBUS_CONFIG_DIR=$(mktemp -d)
mkdir -p "$AGENTBUS_CONFIG_DIR/watchers"
sleep 300 & SP=$!
echo $SP > "$AGENTBUS_CONFIG_DIR/watchers/probe-agent-probe.json.pid"
"$BIN" watch-status --agent probe-agent >/dev/null 2>&1; ST=$?
"$BIN" watch-stop --agent probe-agent >/dev/null 2>&1
sleep 0.5
if kill -0 $SP 2>/dev/null; then kill $SP; rm -rf "$AGENTBUS_CONFIG_DIR"; echo "PASS"; exit 0; fi
rm -rf "$AGENTBUS_CONFIG_DIR"
echo "FAIL: watch-status rc=$ST reported 'sleep 300' (pid $SP) as a RUNNING watcher and watch-stop killed it"
exit 1
