#!/usr/bin/env bash
# AgentBus client production-readiness audit — live probe harness.
# Runs the SAFE set (no network writes, no destruction) and reports PASS/FAIL.
# Exits non-zero on any FAIL.
set -u
cd "$(dirname "$0")"
fail=0
for probe in probe_*.py; do
  [ -e "$probe" ] || continue
  echo "== $probe =="
  if uv run python3 "$probe"; then
    echo "PASS $probe"
  else
    echo "FAIL $probe"
    fail=1
  fi
done
exit $fail
