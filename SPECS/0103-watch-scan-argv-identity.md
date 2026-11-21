# 0103 — watch-status counts the caller's own shell as a running watcher

Ticket: issuedb #103. Reported by procurement-desk-95465d (found by bid-desk-af6c6b), AgentBus thread 01M7Y0SP9AG1S9FXVV9T0Z8CNR, 2026-11-21.

Reproduction before the fix (agent with no watcher):

    bash -c "agentbus watch-status --agent zz-probe-nowatch; echo agentbus watch --agent zz-probe-nowatch >/dev/null"
    -> watcher RUNNING for zz-probe-nowatch (pid <that bash>), exit 0

EARS SPEC:
- The agentbus client shall treat a process as a watcher for agent A only when its argv runs the agentbus program (argv[0] basename 'agentbus', or a python interpreter whose first script argument has basename 'agentbus', or 'python -m agentbus_client.cli'), its first argument after the program is exactly 'watch', and the last --agent value in that argv (--agent A or --agent=A) equals A.
- If a process's command line mentions agentbus, watch and --agent A only inside another program's arguments (a shell -c string, an echo, a comment), then watch-status shall not report it as a watcher, and shall print 'watcher NOT running' and exit 1 when nothing else is found.
- The pidfile identity check (_pid_is_watcher) shall apply the same argv rule, so watch-stop never signals a shell that merely names the agent.
- Where /proc/<pid>/cmdline is readable and its space-join equals the ps args for that pid, the scan shall use the exact NUL-separated argv; otherwise it shall split the ps args on whitespace.
- The scan shall still find real watchers in the shapes this client launches: '<python> <bin>/agentbus watch --agent A ...' (uv tool, monitor), '<python> -m agentbus_client.cli watch --agent A' (daemon, service fallback), 'agentbus watch --agent=A', and the #234 quoted form.
- Name prefixes shall not match: a scan for A shall not claim a watcher for A-x (unchanged from #160).
- No threshold applies to scan latency; the scan stays one ps call plus at most one /proc read per candidate line that contains both 'agentbus' and 'watch'.

TECHNICAL PROBLEMS:
1. Process identity from a command line: deciding which program a process IS, not which words its command line contains.
2. Lossy argv on hosts without /proc (FreeBSD, macOS ps output is a space-joined argv).

SOLUTION DOMAINS: process argv parsing (POSIX exec argv, /proc/<pid>/cmdline NUL-separated, Python interpreter command-line options); codebase pattern _pid_is_watcher (#23/#28) and _scan_watch_process (#160/#234).

ALTERNATIVES:
- Positional argv parse of program + subcommand + --agent [CHOSEN: rejects any shell or other program regardless of what its arguments say; works on space-split ps output too].
- Exclude own process tree (self + ancestors) from the substring scan [REJECTED: a sibling shell or another terminal running the same check still matches; the false positive only moves].
- Keep substring match, add a regex anchor on 'agentbus watch' [REJECTED: 'bash -c "agentbus watch --agent A"' still matches; it is still text matching on a string the shell owns].

## Verification (2026-11-21)

- Red first: with the old `_watch_runtime.py` restored and the new tests in place, 6 of 29 tests in `tests/test_watch_argv_identity.py` failed (scan of a shell line, live naming shell, CLI from a naming shell). With the fix, 39 of 39 pass (new file plus `tests/test_watch_status_scan.py`).
- End to end, real CLI, real processes: `bash -c "<venv>/agentbus watch-status --agent zz-probe-nowatch; rc=$?; echo agentbus watch --agent zz-probe-nowatch >/dev/null; exit $rc"` prints `watcher NOT running` and exits 1. Before the fix it printed `watcher RUNNING ... (pid <that bash>)` and exited 0.
- Known positive: a live process with argv `[python, <tmp>/agentbus, watch, --agent, A]` is still found by the scan, by `_pid_is_watcher`, and by `watch-status` run from a naming shell.
- Real watchers on this host (uv tool argv shape): prism-dev-24cdee, website-60c8ec, futex-923257, agentbus-client-c70fbf are each found by both the scan and the pidfile check at the PID ps shows; the prefix `prism-dev` matches none.
- Not exercised: FreeBSD and macOS (the ps-split fallback is covered only by unit tests with faked ps output), and the `python -m agentbus_client.cli` daemon shape against a live process (unit test only).

## Out of scope, filed separately

- issuedb #104: a watcher found only by the scan prints RUNNING and exits 1. That predates this change.
- Full suite on the working tree (this venv's Python only): 4812 passed, 9 skipped. One existing fixture, `tests/test_omo_watcher.py::test_watch_check_reads_real_pidfiles`, faked a watcher as `python -c "<sleep>" agentbus watch --agent A`. That is the shape this fix rejects, so it now uses a real `[python, <tmp>/agentbus, watch, --agent, A]` process.
- Not yet run: the 3.10 to 3.14 clean-clone matrix. Not released: the uv tool on this host is still 0.9.108.
