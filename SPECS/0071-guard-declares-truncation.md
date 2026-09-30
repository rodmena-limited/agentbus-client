# 0071 — the guard request declares truncation

Ticket: issuedb #71. Server half: agentbus-8dc08d #367, thread 01M2BXDQD0SFWB0WARNJZ2SJKC.

## EARS SPEC

- When the gate shortens any tool_input string before POST /v1/guard/check, the request shall
  carry `"truncated": true`; when nothing was shortened, the field shall be omitted.
- The client shall not ship this until the LIVE endpoint accepts it; a version number is not proof.
- If the guard answers 413, then the gate shall keep allow-with-UNVETTED-warning under operator
  directive #107 until the operator rules otherwise.

## THE ORDERING HAZARD, AND HOW IT WAS CLOSED

Measured 2026-09-29: the server answered 422 "truncated: Extra inputs are not permitted". The gate
maps every non-verdict to allow-unvetted, so sending the field early would have turned every
shortened input from head+tail-evaluated into not evaluated at all. By agreement, the server first
shipped the schema change alone (accept and echo, build fcc0186).

Verified live 2026-09-30, before this release:

    {tool_name, tool_input}                   -> 200 allow, no truncated in the verdict
    {tool_name, tool_input, truncated: true}  -> 200 allow, truncated: true echoed
    {tool_name, tool_input, elided: true}     -> 422 Extra inputs are not permitted   (control)

The control proves the endpoint still refuses unknown fields, so the second line is a real
acceptance and not a server that has stopped validating.

## DESIGN

`truncated` is set when `fit_to_guard_limit(tool_input) != tool_input`; shortening only ever
changes strings, so an unshortened structure compares equal and carries no flag.

## OPEN FOR THE OPERATOR

413 -> ask/deny versus #107's allow-with-warning. Recommended: keep #107, and express "elided
content must not pass silently" as a guard rule matching `truncated: true` that returns a verified
deny or a Futex approval — a real answer the gate honours.

## VERIFICATION

`tests/test_gate_checks_oversized_tool_input.py`. Its fake server now enforces the live contract:
it accepts only `tool_name`, `tool_input` and a boolean `truncated`, and rejects anything else
exactly as the real endpoint does. A known-positive test proves that strictness. Mutations — never
send the flag, or always send it — each fail the suite.
