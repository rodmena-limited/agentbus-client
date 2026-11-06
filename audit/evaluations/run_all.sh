#!/bin/sh
# Run all audit probes. Probes use local fakes on 127.0.0.1, except probe_osv_lockfile.py, which needs network access to api.osv.dev, and the live, self-addressed remind probe, which runs only with AUDIT_ALLOW_LIVE=1. A probe that did not run prints [SKIP], not [PASS]. Exit non-zero on any FAIL.
# A probe that FAILS is an OPEN finding; a previously-failing probe that PASSES is the fix verification.
set -u
cd "$(dirname "$0")"
. ./_python.sh
PY=$(audit_python "$(cd ../.. && pwd)")
export PYTHON="$PY"
ok=1
for probe in probe_*.py probe_*.sh; do
    [ -e "$probe" ] || continue
    case "$probe" in *.py) out=$("$PY" "$probe" 2>&1); rc=$? ;; *.sh) out=$(sh "$probe" 2>&1); rc=$? ;; esac
    if [ $rc -ne 0 ]; then echo "[FAIL] $probe"; ok=0
    else case "$out" in SKIP*) echo "[SKIP] $probe" ;; *) echo "[PASS] $probe" ;; esac; fi
    printf '%s\n' "$out" | sed 's/^/        /' | tail -4
done
[ "$ok" = "1" ] || { echo "AUDIT PROBES FAILED"; exit 1; }
echo "ALL AUDIT PROBES PASS"
