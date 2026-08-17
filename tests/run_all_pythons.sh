#!/bin/sh
# Run the full test suite against every Python version the fleet actually
# installs on. NOT GitHub Actions (house rule: ci.rodmena.co.uk is the CI);
# this is the portable runner that any CI can shell out to.
#
# WHY THIS EXISTS — the SEV-1 that cost this project eleven releases was
# INVISIBLE on the version CI happened to run:
#
#   py3.10  concurrent.futures.TimeoutError is a DISTINCT class,
#           NOT a subclass of OSError.  ->  every `except OSError` guard
#           in the codebase missed it, the watcher died on network blips.
#   py3.11+ CFT became an ALIAS of the builtin TimeoutError, which IS an
#           OSError subclass.  ->  the same guards catch it, bug invisible.
#
# `uv tool install` pins cpython-3.10 by default, so production ran the
# buggy version while every local run was green. Verified empirically:
#
#   py3.10 | CFT is builtin TimeoutError: False | issubclass(CFT, OSError): False
#   py3.11 | CFT is builtin TimeoutError: True  | issubclass(CFT, OSError): True
#   py3.13 | CFT is builtin TimeoutError: True  | issubclass(CFT, OSError): True
#
# 3.10 and 3.11 are the load-bearing legs: they straddle the transition.
# Drop either and the class of bug becomes invisible again.
#
# Usage:  sh tests/run_all_pythons.sh
# Exit:   0 if every version passes, 1 on the first failure.

set -eu

VERSIONS="${AGENTBUS_TEST_PYTHONS:-3.10 3.11 3.13}"
DEPS="--with pytest --with pytest-asyncio --with httpx --with resilient-circuit>=0.5 \
--with bulkman>=2.0 --with segno --with rich --with cryptography>=42"

echo "=== interpreter matrix ==="
for v in $VERSIONS; do
    # shellcheck disable=SC2086
    uv run --quiet --python "$v" --no-project python -c "
import concurrent.futures as cf, sys
print(f'py{sys.version_info.major}.{sys.version_info.minor:<3} CFT-is-builtin={cf.TimeoutError is TimeoutError!s:<5} CFT-is-OSError={issubclass(cf.TimeoutError, OSError)}')
"
done

echo
FAILED=""
for v in $VERSIONS; do
    echo "=== pytest on python $v ==="
    # shellcheck disable=SC2086
    if uv run --quiet --python "$v" $DEPS --with-editable . pytest -q --tb=short; then
        echo "python $v: PASS"
    else
        echo "python $v: FAIL"
        FAILED="$FAILED $v"
    fi
    echo
done

if [ -n "$FAILED" ]; then
    echo "FAILED on:$FAILED"
    exit 1
fi
echo "ALL VERSIONS PASS ($VERSIONS)"
