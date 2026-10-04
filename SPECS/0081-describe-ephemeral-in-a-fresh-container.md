# 0081 — describe() reports a fresh container as not ephemeral

Ticket: issuedb #81. Found by the #80 round-2 tests.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- When describe() runs in a container (/.dockerenv present) with no persisted device file, it shall report ephemeral=True.
DEFECT: identity.py describe() calls device_id() (which creates the device file) before is_ephemeral(), whose container rule is 'no persisted device file' (identity.py:104); the rule can never fire through describe().
REPRODUCED by tests/test_round2_identity_device_and_ephemeral.py (strict xfail): is_ephemeral() True, describe()['ephemeral'] False.
FOUND BY: #80 round-2 tests.
SYNTHESIS (localised bugfix): evaluation order; concept: evaluate the predicate before the side effect that falsifies it.

## FIX

describe() evaluates is_ephemeral() before device_id() persists the device file.

## VERIFICATION

tests/test_round2_identity_device_and_ephemeral.py::test_describe_in_a_fresh_container_is_ephemeral (was strict xfail, now passes). Each test was run against the unfixed code and failed there.
