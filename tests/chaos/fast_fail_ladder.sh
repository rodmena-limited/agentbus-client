#!/bin/sh
# Fast-fail chaos test for the reconnect ladder (0.9.25+).
#
# ORIGIN: macbook-admin-bd8e86 delivered the recipe (thread
# 01M08ZWE0XCTPJG1R0ZBXP8K7P follow-up 01M091G9FQJYVWJK7N29ADCKRQ) as a
# ready-to-run drop-in after noting that blackhole tests are timeout-
# dominated (each attempt burns the full 35s SDK timeout, so reaching
# the top of the (1,2,5,10,30,60) ladder needs ~10 minutes wall-clock).
# Fast-fail against ECONNREFUSED walks the whole ladder in ~120s and is
# the variant that actually EXERCISES the persisted-backoff + jitter
# code paths.
#
# Not in the default pytest suite because it needs a working `agentbus`
# CLI on PATH plus a subprocess with a real socket. Run manually or from
# CI:
#
#   AGENTBUS_API_KEY=ab_sk_dummy sh tests/chaos/fast_fail_ladder.sh
#
# Exit codes:
#   0  ladder walked correctly (1s -> 2s -> 5s -> 10s -> 30s -> 60s),
#      failures persisted, jitter varies per step, no tracebacks
#   1  precondition failed (port already listening — TLS handshake would
#      confuse the timing and the test would pass for the wrong reason)
#   2  ladder assertion failed
#   3  jitter assertion failed
#   4  persistence assertion failed
#   5  traceback observed (a regression in the total-handler)
#
# Two properties from macbook's follow-up worth pinning specifically —
# both would silently regress under a lazy refactor:
#   1. `failures` must PERSIST to the state file, not just live in memory.
#      Otherwise an OS supervisor's crash-and-restart would reset to 1s
#      and the whole persisted-backoff fix is defeated.
#   2. Observed sleep must DIFFER from the base — a jitter regression to
#      a fixed multiplier still passes a naive "did it back off" check.

set -eu

PORT="${AGENTBUS_CHAOS_PORT:-9553}"
STATE=$(mktemp)
LOG=$(mktemp)
BASEURL="https://127.0.0.1:${PORT}"

# --- 0. PRECONDITION: port must refuse fast. ---
# If something is listening, we get a TLS handshake error instead of
# ECONNREFUSED, timing changes, and the test passes for the wrong reason.
# macbook's phrase: "assert the precondition rather than assume it."
python3 - "$PORT" <<'PY' || { echo "PRECONDITION FAILED: pick a port nothing is listening on"; exit 1; }
import socket, sys
s = socket.socket()
s.settimeout(3)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    raise SystemExit("port in use — pick another")
except ConnectionRefusedError:
    pass
PY

# --- 1. Run the watcher against the refused port for 120 s. ---
# Fresh state file so we start at failures=0 and the persisted counter
# grows from a known baseline.
echo '{"cursor":0,"agent":"ci-fastfail"}' > "$STATE"

timeout 130 agentbus --base-url "$BASEURL" watch \
    --agent ci-fastfail --state "$STATE" \
    > "$LOG" 2>&1 || true   # timeout returns 124 on SIGTERM — expected

echo "=== LOG ==="
cat "$LOG"
echo "=== STATE ==="
cat "$STATE"

# --- 2. ASSERTIONS ---

# Zero tracebacks — the total-handler must have caught every drain
# failure. A traceback here is the SEV-1 regression from macbook's
# original report.
if grep -q Traceback "$LOG"; then
    echo "FAIL: traceback observed in log — total-handler regression"
    exit 5
fi

# Ladder walked in order — extract "base <N>s" tokens and confirm
# the sequence contains 1,2,5,10,30,60 in order.
ORDER=$(grep -oE 'base [0-9]+s' "$LOG" | uniq | tr -d 'bases ' | paste -sd, -)
if ! printf '%s\n' "$ORDER" | grep -q '^1,2,5,10,30,60'; then
    echo "FAIL: ladder did not walk full sequence; observed order: $ORDER"
    exit 2
fi

# Top step reached and pinned (RECONNECT_BACKOFF's final entry is 60s
# and further failures should stay there).
grep -q "base 60s" "$LOG" || { echo "FAIL: did not reach top of ladder"; exit 2; }

# Persistence — failures in the state file must be >= 6.
FAILURES=$(python3 -c "import json;print(json.load(open('$STATE')).get('failures',0))")
if [ "$FAILURES" -lt 6 ]; then
    echo "FAIL: failures not persisted (got $FAILURES, expected >= 6)"
    exit 4
fi

# Jitter varies — extract the actual `retrying in Ns` values and prove
# at least one differs from its base by > 0. A jitter regression to a
# fixed multiplier passes a naive "did it back off" check.
python3 - "$LOG" <<'PY' || exit 3
import re, sys, pathlib
log = pathlib.Path(sys.argv[1]).read_text()
pairs = re.findall(r"retrying in ([\d.]+)s \(base (\d+)s", log)
if not pairs:
    print("FAIL: no jitter data in log"); sys.exit(1)
varies = any(abs(float(actual) - float(base)) > 0 for actual, base in pairs)
if not varies:
    print(f"FAIL: no jitter variation — {pairs}"); sys.exit(1)
# Bounds check — every actual must be within +/-15% of its base.
for actual, base in pairs:
    a, b = float(actual), float(base)
    if not (0.85 * b - 0.01 <= a <= 1.15 * b + 0.01):
        print(f"FAIL: jitter out of +/-15% band — actual={a} base={b}"); sys.exit(1)
print(f"PASS: jitter varies + bounded — {len(pairs)} samples, all in +/-15%")
PY

echo "PASS: full ladder walked, persisted, jittered, zero tracebacks"

# Cleanup
rm -f "$STATE" "$LOG"
