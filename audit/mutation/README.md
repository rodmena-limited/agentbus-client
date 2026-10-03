# Mutation testing

Ticket #73, spec `SPECS/0073-mutation-testing.md`. Tool: mutmut 3.8.0 (dev dependency).
Scope and test selection live in `[tool.mutmut]` in `pyproject.toml`.

## When to run

Once per development phase, and whenever one of the 11 scoped modules changes materially.
Not per commit. Runs are long; run them in the background.

## Order of operations

1. Prove the harness can say both YES and NO before reading any result:

       .venv/bin/python audit/evaluations/probe_mutation_harness_live.py

   It must print PASS (strong run kills at least one `_safe_attachment_name` mutant, weak run
   lets at least one survive, and the weak run kills fewer). If it fails, no survivor count
   means anything.

2. Flake baseline: 5 consecutive full-suite runs plus each test file in isolation. Any test
   that fails once is quarantined (deselected in `[tool.mutmut]`) and ticketed.

3. Run:

       .venv/bin/mutmut run --max-children 16      # forkserver isolation, ~16 min

   `tests/test_mutation_run_imports_the_mutants.py` fails the clean run if the tests imported
   the original `src/` instead of `mutants/src`.

4. Read:

       .venv/bin/mutmut results            # everything not killed
       .venv/bin/mutmut show <mutant>       # the diff
       .venv/bin/mutmut tests-for-mutant <mutant>

## Triage classes

Every survived and no-tests mutant gets exactly one class in the round ledger:

| class | meaning | action |
|---|---|---|
| gap | a behaviour change no test notices | follow-up ticket; new test asserts values through the public client or CLI path |
| equivalent | no observable behaviour change | record the reason |
| out-of-reach | only subprocess or env-cleared tests exercise it | record; candidate for an in-process test |
| real-bug | the mutant is more correct than the original | defect ticket |

mutmut's badge score counts timeouts as kills: (killed + timeout) / (total − skipped). The
ledger also reports killed / (total − equivalent), which does not.

## Deselected from mutation runs

14 tests that reach `Watcher._backoff_and_drain` (`watch.py:370` binds `sys.stderr` at import, #74). Under mutmut's
in-process sessions that stream is closed, so these fail with no mutant active and would count as false kills. Found
by swapping the default for a closed stream in a normal run: exactly 14 failed, 2967 passed. The list is in
`[tool.mutmut] pytest_add_cli_args`. Fixing #74 returns them to the kill set.

## Isolation

`process_isolation = "forkserver"`. Default fork produced 172 false kills, 12 false survivals and 4 false timeouts in
`client/resilience.py`; see the round-1 ledger.

## Rounds

- `2026-10-round1.md`
