#!/bin/sh
# Run all SAFE audit probes (local fakes on 127.0.0.1 only; no production bus). Exit non-zero on any FAIL.
# A probe that FAILS is an OPEN finding; a previously-failing probe that PASSES is the fix verification.
set -u
cd "$(dirname "$0")"
PY="${PYTHON:-$(cd ../.. && pwd)/.venv/bin/python}"; [ -x "$PY" ] || PY=python3
ok=1
for probe in probe_*.py probe_*.sh; do
    [ -e "$probe" ] || continue
    case "$probe" in *.py) out=$("$PY" "$probe" 2>&1); rc=$? ;; *.sh) out=$(sh "$probe" 2>&1); rc=$? ;; esac
    if [ $rc -eq 0 ]; then echo "[PASS] $probe"; else echo "[FAIL] $probe"; ok=0; fi
    printf '%s\n' "$out" | sed 's/^/        /' | tail -4
done
[ "$ok" = "1" ] || { echo "AUDIT PROBES FAILED"; exit 1; }
echo "ALL AUDIT PROBES PASS"
