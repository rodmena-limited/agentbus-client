# 0073 — mutation testing, round 1 (security-critical modules)

Ticket: issuedb #73. Method follows https://farshid.co.uk/entry/mutation_testing_a_localisation_service:
mutmut, selective scope, once per development phase.

## EARS SPEC

- The mutation harness shall generate mutants for the 11 scoped modules and no others:
  `sealing.py`, `_signing.py`, `identity.py`, `client/sync_verify.py`, `client/_reply_guard.py`,
  `client/attachments.py`, `client/resilience.py`, `hooks/_gate.py`, `cli/_sigline.py`,
  `cli/_read.py`, `onboarding/_credentials.py`.
- Before any mutation result is reported, the harness shall demonstrate a planted known-killable
  mutant reported killed, and that the imported `agentbus_client` resolves under `mutants/`.
- If any test fails in >=1 of 5 baseline runs or in isolation, then it shall be quarantined from
  the mutation run and get its own ticket.
- If a test reads source text, AST, or repo files mutmut does not copy, then it shall be
  deselected, with the reason recorded here.
- When the run completes, 100% of survived and no-tests mutants shall be classified gap /
  equivalent / out-of-reach / real-bug; 0 unclassified.
- The report shall state per module: mutants, killed, survived, timeout, no-tests,
  score = killed / (total - equivalent), and total wall time.
- Round 1 has no score threshold (baseline). Round-2 target: >=90% for `sealing.py`,
  `_signing.py`, `hooks/_gate.py`, `cli/_sigline.py` after their gap tickets close.
- Adding mutmut shall leave the OSV lockfile probe at 0 vulnerable pins.

## TECHNICAL PROBLEMS

1. Test-adequacy measurement by fault injection (oracle strength, not line coverage).
2. Isolation: the mutated copy, not the editable install, must be the code under test.
3. Kill-signal integrity: flaky, order-dependent and source-reading tests must not count as kills.
4. Triage and durable record of survivors.

## SOLUTION DOMAINS

- Mutation analysis; mutmut 3.8.0 (per-function trampolines, test-to-function mapping).
- Codebase pattern: `audit/evaluations/probe_signature_render_mutations.py` (hand-authored
  mutants; an unapplied anchor is HARNESS-FAILED, never SKIP).

## ALTERNATIVES

- Tool: mutmut 3.8.0 **[CHOSEN]**: maps each mutant to its covering tests, py>=3.10, used in the
  article. cosmic-ray **[REJECTED]**: whole suite per mutant unless filtered by hand. Extending the
  hand probe **[REJECTED]**: finds only faults the author already suspects.
- Scope: security-critical subset **[CHOSEN]**. All of `src/` **[REJECTED]**: ~7x the mutants,
  mostly rendering code; triage cost exceeds value.
- Isolation: proven by canary **[CHOSEN]**. Trusting mutmut's `sys.path` ordering **[REJECTED]**:
  the editable `.pth` points at the original `src/`.
- Process isolation (synthesis change during the run): `forkserver` **[CHOSEN]**. `fork` (mutmut default)
  **[REJECTED]**: the parent holds built singletons and live bulkman threads; 190 verdicts differed, 10/10 sampled
  flips matched forkserver under plain pytest.

## HARNESS CHECKS

- `tests/test_mutation_run_imports_the_mutants.py`: skipped outside mutmut. Inside a run it fails
  the clean run unless `agentbus_client.__file__` is under `mutants/`. Shown to go red with
  `MUTANT_UNDER_TEST=x` set outside mutmut (imports the original `src/`, fails).
- `audit/mutation/canary.py`: two mutmut runs on
  `_safe_attachment_name` in a scratch copy. Measured 2026-10-03:

      strong (tests/test_attachment_verb.py):     killed 13, survived 8
      weak   (calls the function, asserts none):  killed 6,  survived 15

  Both directions are reportable and the verdict depends on the tests.

## DESELECTED FROM MUTATION RUNS

14 tests reaching `watch.py:370` (`stream: Any = sys.stderr` default, ticket #74): 1 in test_audit_sweep_0929,
8 in test_persisted_backoff, 5 in test_watcher_survives_cft_outage. They fail with no mutant active once mutmut's
earlier session closes that stream. No source-reading test failed the clean run.
Removed after #74 fixed the cause (2026-10-04); the mutation run now deselects nothing.

## QUARANTINED (FLAKY)

None. 5/5 full-suite runs green (2981 passed); 114/114 files green in isolation under 8-way parallel load.

## RESULT

`audit/mutation/2026-10-round1.md`: 2778 mutants, killed 1155, score 43.4 % (killed / (total − equivalent));
1622 survived/no-tests mutants classified, 0 unclassified. Defects ticketed: #74, #75 (critical), #76.
