# 0100 — The omo watcher is visible, and one watcher holds an identity across harnesses

Ticket: issuedb #100. Requested by omo-test-147b40 (AgentBus thread 01M768E9D5HA3MVH3HAZDDKZAJ, item 4).

The lock is the one omo's extension writes at agentbus-omo-plugin 181469c: `$AGENTBUS_CONFIG_DIR/omo-watch-<agent>.lock`, holding `<pid>\n`, mode 0600.

EARS SPEC:
- The client shall treat the omo lock as LIVE only when it holds a positive pid, that process exists and belongs to this user, and the process started no more than 2 seconds after the lock was last written.
- If the lock's pid is not running, belongs to another user, or started more than 2 seconds after the lock was written, then the client shall treat the lock as STALE and name the reason.
- If the lock does not hold a positive integer, then the client shall treat it as UNREADABLE and name the content.
- If the agent name is not a safe file name, then the client shall read no lock.
- While an omo lock for the agent is LIVE, `agentbus watch` (without `--once`, with or without `--daemon`) shall print the holder's pid and the lock path and exit 9 without opening a stream.
- When the omo lock stops being LIVE, `agentbus watch` shall start normally.
- `agentbus watch-check --agent A [--harness claude|omo] [--session ID]` shall exit 0 and print `free: ...` when no other watcher holds A. It shall exit 4 and list each holder when one does. With `--harness claude` it counts a LIVE omo lock. With `--harness omo` it does not, because omo manages its own lock. With `--session ID` it ignores watcher registrations whose state-file name contains ID.
- `agentbus watch-status` shall print `omo watcher RUNNING for A (pid N) [<lock>]` while the lock is LIVE, and exit 0 when that is the only watcher. It shall print a STALE or UNREADABLE line otherwise.
- `agentbus watch-stop` shall not signal the omo process. It shall say that the omo watcher runs inside an omo session.
- `agentbus doctor --wake` shall report `harness: omo`, the holder pid and the extension's state against the pinned commit while the lock is LIVE. It shall say that it did not test the stream. Where the lock is STALE or UNREADABLE, or the extension is installed, it shall say no omo session holds the stream and continue with the other checks.

TECHNICAL PROBLEMS:
1. Mutual exclusion between two independently written watchers that share only the file system.
2. Telling a live lock from a stale one when pids are reused.
3. The Claude plugin monitor's guard lives in the server repo's marketplace submodule and greps `watch-status` for `-<uuid>.json.pid`, so this repo cannot change it.

SOLUTION DOMAINS:
- Advisory pid lock files (the omo extension's own scheme). A pid is checked with `kill(pid, 0)`, and reuse is detected by comparing the process start time (`/proc/<pid>/stat` starttime plus `btime`, or `ps -o etime=`) with the lock's mtime.
- Codebase pattern: `_watch_pids` and `_pid_is_watcher` for client watchers, and the agy branch in `doctor_wake`.

ALTERNATIVES:
- Refuse inside `agentbus watch` [CHOSEN]. The Claude plugin monitor runs `agentbus watch`, so this covers the Claude direction with no plugin change. The cost: until the plugin handles exit 9, the monitor retries 5 times and prints its "stream failed" line. agentbus-8dc08d has been asked to treat 9 like its sibling guard.
- Print a fake `-<uuid>.json.pid` label so the plugin's regex trips [REJECTED: it misreports what is running].
- Ask for a plugin change only [REJECTED: nothing is refused until a second team ships].
- Stale detection: pid alive plus start time against lock mtime [CHOSEN] vs pid alive only [REJECTED: a reused pid would block the Claude watcher for good] vs a cmdline match [REJECTED: omo's process command line was not measured on any host].
- `watch-check` as the shared rule both harnesses call [CHOSEN], so the rule stays in the shared client, as the extension's README says it should.
- `watch-stop` kills the omo pid [REJECTED: that pid is the whole omo session].

NOT EXERCISED: a real omo process holding the lock (omo is not installed here, so a Python child process stands in for it), the omo extension calling `watch-check`, and the plugin monitor's behaviour on exit 9.
