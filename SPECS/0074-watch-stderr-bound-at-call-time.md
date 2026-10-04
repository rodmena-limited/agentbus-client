# 0074 — watch: _backoff_and_drain binds sys.stderr at import time

Ticket: issuedb #74. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- When Watcher._backoff_and_drain is called without an explicit stream, the watcher shall write to the sys.stderr current at call time.
- If the import-time stderr has since been closed, then the reconnect path shall not raise ValueError.

FOUND BY: #73 mutation harness clean run. src/agentbus_client/watch.py:370 has 'stream: Any = sys.stderr' as a default argument, evaluated once at import. mutmut runs stats and clean sessions in one process; the module captured the first session's capture stream, which was closed by the time tests/test_audit_sweep_0929.py::test_backoff_does_not_block_forever_on_an_in_flight_drain ran -> 'ValueError: I/O operation on closed file' at watch.py:413. The test passes in a normal run (5/5 baselines, 2981 passed).

SYNTHESIS (localised bugfix): Python default-argument evaluation semantics; concept: late binding via 'stream=None' -> resolved to sys.stderr in the body.

Until fixed, the test is deselected from mutation runs only ([tool.mutmut] pytest_add_cli_args).

## FIX

`Watcher._backoff_and_drain(stream=None)` resolves `sys.stderr` in the body.

## VERIFICATION

tests/test_watch_backoff_writes_to_the_current_stderr.py (call-time stderr; explicit stream wins). The 14 mutmut deselections are removed. Each new test was run against the unfixed code and failed there.
