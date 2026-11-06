# 0096 — SDK pool workers are daemon threads on Python 3.14

Ticket: issuedb #96. Shipped in 0.9.107.

EARS SPEC:
- Where the interpreter is Python 3.10 to 3.14, the SDK pool shall run its workers as daemon threads.
- If a worker is blocked in a read when the process exits, then the interpreter shall exit within 30 seconds of the main thread finishing.
- If the stdlib's `_worker` has a shape the client does not know, then the client shall use the stock executor and shall still run the work.

TECHNICAL PROBLEMS:
1. `concurrent.futures.thread._worker` went from four parameters to three in 3.14, so the shape check rejected it and the pool used stock workers.
2. `resilience.py` sat at 498 lines, so the change needs a new home.

SOLUTION DOMAINS:
- Python's thread pool, read from the 3.14 stdlib: `_worker(executor_reference, ctx, work_queue)`, with the context from `self._create_worker_context()` and `prepare_context` as the 3.14 marker.
- Issuedb #26, which introduced the daemon pool.

ALTERNATIVES:
- Handle both shapes in `_adjust_thread_count` [CHOSEN]. It is the smallest change and keeps the checked fallback.
- Keep the stock fallback on 3.14 [REJECTED: the hang from #26 is back, and `requires-python` allows 3.14].
- Write a pool that does not use private stdlib names [REJECTED: bulkman reads the executor's shape, and this is a larger change for one release].
- Module: `client/_daemon_pool.py` [CHOSEN, because `resilience.py` would pass the 500 line cap].

RESULTS, measured 2026-11-06: on Python 3.14.7 before the fix, `test_a_worker_blocked_at_exit_does_not_hold_the_interpreter` timed out at 30 seconds and two tests in `test_round2_resilience_bulkhead.py` failed. After the fix the full suite passes on 3.10.20, 3.11.16, 3.12.13, 3.13.14 and 3.14.7 from a clean clone with locked dependencies, 4,713 tests on each and 4,714 on 3.10.
