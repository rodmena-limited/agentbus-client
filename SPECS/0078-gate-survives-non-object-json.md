# 0078 — Gate hook crashes on JSON that is not an object (stdin or guard body)

Ticket: issuedb #78. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- If the PreToolUse stdin is valid JSON but not an object, then the gate shall treat it as an empty payload and still print a decision with exit 0.
- If the guard answers 200 with a JSON body that is not an object, then the gate shall treat it as no verdict and degrade with the UNVETTED warning, never crash.
REPRODUCED 2026-10-04 via .venv/bin/agentbus-hook pre-tool-use: stdin '[1, 2]' and '"just a string"' -> rc=1, AttributeError, no decision on stdout; control object stdin -> rc=0 with a decision. 200 body [] -> body.get AttributeError at _gate.py:436.
FOUND BY: #73 triage (hooks/_gate group).
SYNTHESIS (localised bugfix): input validation at the hook boundary; concept: type-check parsed JSON before use (same handling as unparseable JSON).

## FIX

Non-object stdin becomes `{}`; a non-object 200 body raises inside the request try and degrades like an unparseable body.

## VERIFICATION

tests/test_gate_survives_non_object_json.py (4 stdin shapes, 4 body shapes, object-body control). Each new test was run against the unfixed code and failed there.
