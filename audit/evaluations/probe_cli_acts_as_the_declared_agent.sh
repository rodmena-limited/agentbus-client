#!/bin/sh
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PY="${PYTHON:-$REPO/.venv/bin/python}"
SRC="${AGENTBUS_CLIENT_SRC:-$REPO/src}"
HOME_DIR=$(mktemp -d)
mkdir -p "$HOME_DIR/proj/.agentbus" "$HOME_DIR/empty"
echo "probe-declared-agent" > "$HOME_DIR/proj/.agentbus/agent"
fail=0
( cd "$HOME_DIR/proj" && env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin PYTHONPATH="$SRC" AGENTBUS_CONFIG_DIR="$HOME_DIR/.config/agentbus" "$PY" -m agentbus_client.cli service --manager systemd ) > "$HOME_DIR/out" 2> "$HOME_DIR/err"
rc=$?
if [ $rc = 0 ] && grep -q "probe-declared-agent" "$HOME_DIR/out"; then
    echo "  ok      service in a declared checkout acts as probe-declared-agent with no --agent"
else
    echo "  FAIL    service rc=$rc: $(head -2 "$HOME_DIR/err")"; fail=1
fi
( cd "$HOME_DIR/empty" && env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin PYTHONPATH="$SRC" AGENTBUS_CONFIG_DIR="$HOME_DIR/.config/agentbus" "$PY" -m agentbus_client.cli watch-status ) > "$HOME_DIR/out" 2> "$HOME_DIR/err"
rc=$?
if [ $rc = 2 ] && grep -q "agentbus setup" "$HOME_DIR/err"; then
    echo "  ok      an undeclared directory refuses once, rc 2, naming the fix"
else
    echo "  FAIL    undeclared directory rc=$rc: $(head -2 "$HOME_DIR/err")"; fail=1
fi
rm -rf "$HOME_DIR"
[ $fail = 0 ] && echo "PASS" || echo "FAIL"
exit $fail
