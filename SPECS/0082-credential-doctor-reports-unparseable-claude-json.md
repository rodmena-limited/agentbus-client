# 0082 — Credential doctor silently omits an unparseable ~/.claude.json

Ticket: issuedb #82. Found by the #80 round-2 tests.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- If ~/.claude.json exists but cannot be parsed, then doctor_credential_scope shall report it as NOT CHECKED, as it does for opencode (#76), instead of omitting the slot.
DEFECT: _credentials.py step 2 swallows the parse error in a bare except; a full key in that slot goes unreported.
REPRODUCED by tests/test_round2_credential_scope.py (strict xfail).
FOUND BY: #80 round-2 tests.

## FIX

Step 2 reports `user-scope ~/.claude.json: NOT CHECKED — could not parse it (<error>)` instead of omitting the slot.

## VERIFICATION

tests/test_round2_credential_scope.py::test_an_unparseable_claude_json_is_reported_not_omitted (was strict xfail, now passes). Each test was run against the unfixed code and failed there.
