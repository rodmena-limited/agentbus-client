# 0075 — SECURITY: gate fast-fail circuit never re-probes under continuous tool calls (gating stays off)

Ticket: issuedb #75. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- While the gate fast-fail circuit is open, when the cooldown has elapsed since the circuit OPENED (not since the last fast-fail), the gate shall send the next tool call to POST /v1/guard/check (half-open probe).
- When that probe returns a real verdict, the gate shall enforce it and clear the degraded record.
- If tool calls keep arriving more often than the cooldown, then the gate shall still probe the guard at least once per cooldown interval (default 30 s).
- A fast-fail shall still be counted in the degraded record (telemetry) without extending the open window.

DEFECT: hooks/_gate.py:296-300 — on every fast-fail, record_gate_degraded (hooks/_state.py:249) rewrites last_at=now. The cooldown check compares now to last_at, so with calls < cooldown apart the circuit never half-opens. Only a real verdict clears it (_gate.py:434), but the fast-fail returns before any network call, so none can arrive.

REPRODUCED 2026-10-03 through the product entry point (.venv/bin/agentbus-hook pre-tool-use), fake guard on 127.0.0.1, AGENTBUS_GATE_FAST_FAIL_COOLDOWN=5:
  phase 1: 3 calls, guard 503 -> allow (degraded), circuit opens
  phase 2: guard healthy and denying; 8 calls every 2 s over 15 s -> allow 8/8 FAST-FAILING, 0 requests reached the guard
  control: 7 s gap, 1 call -> deny 'GUARD-DENY rm -rf needs approval' (the deny path is reachable)
With the default 30 s cooldown, 3 transient 503s (a rolling deploy) disable gating for any session making a tool call at least every 30 s, indefinitely.
Probe: audit/evaluations/probe_gate_circuit_reprobes.py (FAILS while open).

FOUND BY: #73 mutation triage (hooks/_gate.py mutants 181-183 drop the refresh and deny again from t+40; flagged real-bug by triage, reproduced independently here).

SYNTHESIS: circuit-breaker state machine (closed / open / half-open; Nygard, Release It!). Concept missing: half-open after a fixed open window. Alternatives: (a) keep opened_at separate from last_at, probe when now - opened_at >= cooldown [recommended]; (b) stop refreshing last_at on fast_fail [simplest, loses 'last degraded' timestamp semantics used by doctor/watch-status]. Decision on fail-open policy (#107) is the operator's.

## FIX

`record_gate_degraded` writes `opened_at`: now on a real failure, carried over on `fast_fail`. The gate measures the cooldown from `opened_at` (falls back to `last_at` for state files written before this change).

## VERIFICATION

tests/test_gate_circuit_reprobes_after_cooldown.py (4 tests, clock-controlled); audit/evaluations/probe_gate_circuit_reprobes.py end to end through agentbus-hook: before 8/8 allow and 0 guard requests, after 3 fast-fails inside the 5 s cooldown then 5/5 deny enforced. Each new test was run against the unfixed code and failed there.
