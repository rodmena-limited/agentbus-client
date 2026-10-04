# 0083 — A broken project settings.local.json aborts the credential doctor (SystemExit)

Ticket: issuedb #83. Found by the #80 round-2 tests.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- If .claude/settings.local.json in the project is not valid JSON, then doctor_credential_scope shall report that slot as NOT CHECKED and still report the other slots.
DEFECT: step 1 reads it through _paths._load_json, which raises SystemExit on invalid JSON (_paths.py:344); except Exception does not catch SystemExit.
FOUND BY: #80 round-2 tests (identity/credentials group).

## FIX

Step 1 catches the SystemExit from _load_json, reports the slot as NOT CHECKED, and the doctor continues to steps 2 and 3.

## VERIFICATION

tests/test_credential_doctor_survives_broken_project_settings.py (broken file reported and the user-scope finding still produced; valid file still names the agent). Each test was run against the unfixed code and failed there.
