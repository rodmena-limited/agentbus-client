# 0090 — The gate carries every call to the guard

Ticket: issuedb #90.

Operator ruling, 2026-11-06: "agentbus must be the most compatible and proper tool. it's a
communication tool not a decision tool." It extends directive #107 and is recorded as issuedb memory
`gate_is_a_messenger`.

EARS SPEC:
R1. The PreToolUse gate shall block a tool call only on a verified `deny` from the guard. An answer with no `allow` or `deny` is not a verdict.
R2. If tool input is nested deeper than 512 levels, then the gate shall send the guard the call with the deeper levels replaced by a marker and `truncated: true`, and shall follow the guard's answer.
R3. If the hook input does not decode to a JSON object, then the gate shall send the guard the raw text under `unreadable_tool_call`, with the tool name where one can be found, and shall follow the guard's answer.
R4. If an `AGENTBUS_GATE_*` setting does not parse or is out of range, then the gate shall use the default, still ask the guard, and name the setting in the decision reason. An empty value means unset and gets no warning.
R5. If the guard cannot be reached or gives no verdict, or the gate fails in a way nobody foresaw, then the gate shall allow the call with an UNVETTED warning and a degraded record.
R6. The gate shall exit 0 on every path, including when it cannot print a decision.
R7. If AGENTBUS_BASE_URL is not an http or https URL, or does not parse, then the gate shall not open it and shall treat the bus as unreachable (#93 item 13).

REPRODUCED on published 0.9.105 and 0.9.106 (sha256 4ce6f1847bb108aa...), real subprocess, real HTTP, deny-all guard:
  plain call, nesting 50, nesting 900     exit 0, deny, guard asked once
  nesting 1100, 5000, 200000              exit 1, no decision, guard not asked
  AGENTBUS_GATE_TIMEOUT=abc               exit 1, no decision, guard not asked
  AGENTBUS_GATE_CONNECT_TIMEOUT=abc       exit 1, no decision, guard not asked
  AGENTBUS_GATE_FAST_FAIL_AFTER=2.5       exit 1, no decision, guard not asked
  AGENTBUS_GATE_FAST_FAIL_COOLDOWN=(empty) exit 1, no decision, guard not asked

Real harness, Claude Code 2.1.319 in headless mode, with a proof file written by the tool call:
  0.9.106, deny-all guard                 tool did not run, guard asked once
  0.9.106, deny-all guard, TIMEOUT=abc    tool RAN, guard not asked

So a crash in the gate let a denied call through. Deep nesting was not driven through the harness,
because the model builds the tool input. It crashes with the same exit 1.

TECHNICAL PROBLEMS:
1. The entry point was not total: three separate causes reached the process boundary as a traceback.
2. A value the client cannot read has to reach the party that decides.
3. Env values that parse can still break the transport (`nan`, `inf`, `-1`, `0`, `1e30`).
4. Truncation has to stay bounded through three recursive steps: the fit, the comparison, `json.dumps`.

SOLUTION DOMAINS:
- Process-boundary error handling: a total entry point.
- Directive #107, whose table already lists "unparseable / unforeseen -> ALLOW, with a warning".
- The server's guard contract, read at `src/agentbus/services/tool_guard_match.py`: rules match an exact tool name and a regex over rendered input, and `tool_input` is `Any`.
- The live guard, measured with a harmless `ls`: nesting of 900 accepted, malformed JSON answered 422.

ALTERNATIVES:
- Undecodable input: send it to the guard as text [CHOSEN]. It keeps the decision with the guard, and a rule still matches a tool name or a pattern in the text.
  Deny it in the client [REJECTED: the client would be deciding, against the ruling, and a garbled call would block].
  Allow it with a warning [REJECTED: skips the guard when it could have been asked].
- Depth: an iterative walk bounded at 512 [CHOSEN]. The bound sits well inside the 900 the live guard accepted, and `json.dumps` and the comparison, which recurse in C, stay below the recursion limit on 3.10 to 3.14.
  A recursive walk bounded at 512 [REJECTED after measurement: the first version of this fix. A comprehension costs a second frame per level on 3.10 and 3.11, because 3.12 inlined them, so the fit raised RecursionError near 490 levels and the crash handler ran a denied call UNVETTED. The matrix on 3.10 and 3.11 caught it before release].
  An iterative walk with no bound [REJECTED: `json.dumps` and the comparison recurse anyway].
- Bad setting: fall back to the default and say so in the reason [CHOSEN].
  Degraded record, as the first draft of this spec said [REJECTED: the guard's verdict clears the record in the same call, so it would never be seen, and counting it would move the fast-fail circuit towards skipping the guard].
- Answer with no verdict: treat as no answer [CHOSEN], per #107. It was a client-side deny.
  Keep the deny [REJECTED: only a verified deny blocks. A future blocking verdict must be added to this client on purpose].
- Module: move the input side to `hooks/_gate_input.py` [CHOSEN], because `_gate.py` would have passed the 500 line cap.

RESULTS, measured 2026-11-06:
- `probe_gate_survives_deep_nesting.py` fails on 0.9.106 and passes 11 of 11 cases on the fix.
- Real harness, Claude Code 2.1.319, with a local guard that denies everything. On 0.9.106 with no bad setting the tool did not run and the guard was called once. On 0.9.106 with `AGENTBUS_GATE_TIMEOUT=abc` the tool ran and the guard was called 0 times. On the fix the tool did not run in either case. An allow control ran. With the guard unreachable the tool ran UNVETTED.
- The live guard accepted nesting of 900 with a harmless `ls`, and answered 422 to malformed JSON.
- Not driven through the harness: deep nesting. A live guard deny was not exercised.
- Existing tests changed on purpose: `not json{` is now delivered as text, and `{"decision":"maybe"}` and `{}` left the deny table. `test_an_answer_with_no_verdict_never_blocks` covers them.
- `json.loads` parsed 994 levels on 3.10 and 3.11, 9,997 on 3.12 and 57,930 on 3.14. Input deeper than that reaches the guard as text, with `truncated` set only when the text itself was cut. Input the interpreter can parse is cut at 512 levels and flagged. The guard gets the call either way.
