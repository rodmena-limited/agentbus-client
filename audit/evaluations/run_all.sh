#!/bin/sh
# Run all safe audit probes. Exit non-zero on any FAIL.
set -eu
cd "$(dirname "$0")"
ok=1
for probe in probe_*.py; do
    if python3 "$probe"; then
        echo "[PASS] $probe"
    else
        echo "[FAIL] $probe"
        ok=0
    fi
done
[ "$ok" = "1" ] || { echo "AUDIT PROBES FAILED"; exit 1; }
echo "ALL AUDIT PROBES PASS"
