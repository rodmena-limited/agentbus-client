# 0076 — Credential doctor never reports the opencode agentbus key: JSONC comment strip cuts https:// URLs

Ticket: issuedb #76. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- When ~/.config/opencode/opencode.json(c) holds an mcp.agentbus entry as written by 'agentbus setup opencode', doctor_credential_scope shall report that entry's scope.
- If a // or /* */ comment appears inside a JSON string (for example 'https://...'), then the JSONC comment stripping shall leave the string intact.
- If the file cannot be parsed, then the doctor shall say it could not read the file, not omit it silently.

DEFECT: src/agentbus_client/onboarding/_credentials.py:155 re.sub(r'//.*', '', text) removes everything after '//' on each line, including inside string literals. Every opencode.json written by setup contains 'https://.../mcp' (onboarding/_opencode_setup.py:80), so json.loads raises, the bare except at :163 swallows it, and the opencode credential line is never produced. setup opencode (:177) filters doctor_credential_scope output for FINDING, so an over-scoped inherited opencode key is never warned about.

REPRODUCED 2026-10-03, fake HOME, doctor_credential_scope(base_url='http://127.0.0.1:9'):
  opencode.json with url 'https://agentbus.rodmena.co.uk/mcp' -> no opencode line at all
  control, same file without url -> 'opencode opencode.json agentbus MCP: unknown'

FOUND BY: #73 mutation triage (doctor_credential_scope mutant 83 classed real-bug: the mutant parses the file, the original does not).

SYNTHESIS (localised bugfix): JSONC parsing; concept: a string-aware comment stripper (tokenise strings first) or a JSONC-tolerant parser. Alternatives: string-aware regex/tokeniser [recommended, no dependency] vs adding a json5/jsonc dependency [rejected for a one-call-site need].

## FIX

`_strip_jsonc` removes // and /* */ comments and trailing commas only outside string literals; an unparseable file is reported as NOT CHECKED instead of omitted.

## VERIFICATION

tests/test_credential_doctor_reads_setup_written_opencode_config.py (setup-shaped file with https URL, JSONC, unparseable, markers inside strings). Each new test was run against the unfixed code and failed there.
