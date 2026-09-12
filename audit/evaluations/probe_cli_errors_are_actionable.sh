#!/bin/sh
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
PY="${PYTHON:-$REPO/.venv/bin/python}"
SRC="${AGENTBUS_CLIENT_SRC:-$REPO/src}"
HOME_DIR=$(mktemp -d)
mkdir -p "$HOME_DIR/work"
fail=0
run() {
    ( cd "$HOME_DIR/work" && env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin PYTHONPATH="$SRC" "$@" ) > "$HOME_DIR/out" 2> "$HOME_DIR/err"
    echo $?
}
check() {
    if [ "$1" = "ok" ]; then echo "  ok      $2"; else echo "  FAIL    $2"; fail=1; fi
}
rc=$(run "$PY" -m agentbus_client.cli inbox)
lines=$(wc -l < "$HOME_DIR/err")
[ "$rc" = 8 ] && [ "$lines" -le 3 ] && grep -q "agentbus setup" "$HOME_DIR/err" && check ok "no credential: rc 8, $lines lines, names setup" || check fail "no credential: rc $rc, $lines lines: $(head -1 "$HOME_DIR/err")"
rc=$(run AGENTBUS_API_KEY=ab_sk_fake AGENTBUS_BASE_URL=http://127.0.0.1:9 "$PY" -m agentbus_client.cli whoami)
[ "$rc" = 3 ] && grep -q "http://127.0.0.1:9" "$HOME_DIR/err" && check ok "unreachable bus: rc 3, names the URL" || check fail "unreachable bus: rc $rc: $(head -1 "$HOME_DIR/err")"
rc=$(run AGENTBUS_API_KEY=ab_sk_fake AGENTBUS_BASE_URL=http://127.0.0.1:9 "$PY" -m agentbus_client.cli remind -m hi --delay soonish)
! grep -q Traceback "$HOME_DIR/err" && [ "$rc" = 2 ] && check ok "malformed duration: rc 2, no traceback" || check fail "malformed duration: rc $rc: $(tail -1 "$HOME_DIR/err")"
rc=$(run "$PY" -m agentbus_client.cli --json inbox)
"$PY" -c "import json,sys; json.load(open(sys.argv[1]))['error']['code']" "$HOME_DIR/err" 2>/dev/null && check ok "--json error is JSON" || check fail "--json error is not JSON: $(head -c 120 "$HOME_DIR/err")"
rc=$(run "$PY" -m agentbus_client.cli help send)
[ "$rc" = 0 ] && grep -q "Usage: agentbus send" "$HOME_DIR/out" && check ok "help send works" || check fail "help send: rc $rc"
rc=$(run "$PY" -m agentbus_client.cli)
[ "$rc" = 0 ] && grep -q "Send mail:" "$HOME_DIR/out" && check ok "bare agentbus prints the grouped overview" || check fail "bare agentbus: rc $rc"
rm -rf "$HOME_DIR"
[ $fail = 0 ] && echo "PASS" || echo "FAIL"
exit $fail
