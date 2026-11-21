# 0104 — watch-status exits 1 while printing RUNNING for a scan-found watcher

Ticket: issuedb #104. Found while fixing #103 (2026-11-21).

## EARS SPEC

- If watch-status finds no pidfile watcher and no live omo lock, but the process scan finds a live watcher for the agent, then it shall print `watcher RUNNING for A (pid N) — pidfile missing` and exit 10.
- While the wake path is broken, watch-status shall exit 3 whether or not a watcher is found (unchanged; 3 outranks 0 and 10).
- If no watcher is found by any check, then watch-status shall exit 1 (unchanged).
- The `watch-status` help text shall list exit codes 0, 1, 3 and 10.

## Synthesis

Solution domain: CLI exit-status contracts. Codebase pattern: exit 3 for a broken wake path, where stdout and status had disagreed before.

ALTERNATIVES:
- New distinct code, 10 [CHOSEN]. A script can tell "running but watch-stop cannot stop it" apart from both "running" and "not running". Exit 5 was considered first and rejected because the CLI already returns 5 for ServiceUnavailable (`cli/_parser.py`). Codes 0-5, 7, 8 and 9 are taken, and 6 is the monitor's stream-killed code.
- Exit 0 [REJECTED]. A supervisor that restarts on non-zero would accept a watcher it cannot stop or re-adopt.
- Keep exit 1 and reword stdout [REJECTED]. The status code would still say "no watcher" while a watcher runs, and a script that starts one on exit 1 would start a duplicate.

Consumers checked: the plugin monitor (`agentbus/marketplace/plugins/agentbus/scripts/agentbus-monitor.sh`) reads watch-status output with `|| true` and does not branch on its exit code.

## Verification (2026-11-21)

- `tests/test_watch_argv_identity.py::test_watch_status_exit_code_for_a_scan_found_watcher` covers (wake ok, orphan) -> 10, (broken, orphan) -> 3, (ok, none) -> 1, (broken, none) -> 3.
- End to end: a live `[python, <tmp>/agentbus, watch, --agent A]` process with no pidfile, real CLI in an isolated config dir -> RUNNING line, exit 10.
- Red first: with `return 1` restored, both tests fail.
