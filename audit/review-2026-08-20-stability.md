# agentbus-client 0.9.43 — stability, concurrency and security review

**Date** 2026-08-20 · **Ticket** issuedb #23 · **Findings** #24–#34 · **Spec** `SPECS/0023-stability-security-review.md`
**Method** mission-critical-audit (falsify, don't confirm). Every CONFIRMED item was reproduced on this
machine against fake servers on 127.0.0.1 or in-process stubs; **the production bus was never touched**.
Each reproduction is persisted as a probe in `audit/evaluations/` — run `audit/evaluations/run_all.sh`;
a FAIL is an open finding, and a probe that later PASSES is the fix verification.

**Baseline on 0.9.43 (main @ db88af9):** 482 unit tests pass, 4 skipped. Probe harness: 3 PASS (pre-existing),
**9 FAIL (new)**.

---

## Why the monitor goes stale after a network drop — the mechanism

The reports ("stale monitor", "crash on network drop/instability") are explained by three defects that compound:

1. **The SDK's outer deadline does not cancel work (#26).** `_run_with_resilience` returns `TransportError`
   to the caller after `timeout+5` s, but the retry sequence keeps running on a **non-daemon** bulkman thread
   for up to ~110 s (4 attempts × 30 s + backoff). Those zombies (a) occupy the 8-slot bulkhead so that
   **calls against a reachable bus fail** once the network is back, and (b) **block interpreter exit**.
2. **Both circuit breakers are decorative (#24).** `SafetyNet` applies policies in reverse, so the breaker is
   *outermost* and only ever sees `RetryLimitReached`; neither `_is_transient_sdk_error` nor
   `_is_transient_rewake_error` recognises it, so each exhausted sequence is recorded as a **success**. Nothing
   ever fails fast, so every call during an outage runs the full ladder and leaves a zombie.
3. **The Stop-hook monitor writes its ledger before it returns exit 2.** With zombies holding the process past
   the 600 s hook timeout, the harness kills it: the user sees a failed hook, and a wake already recorded in
   the ledger is never re-raised (SUSPECTED S1 — the harness half was not reproduced; the two halves were).

Separately, a cleanly-closed SSE stream triggers an **unthrottled, unlogged reconnect loop** (#27) — a
watcher that looks RUNNING and hammers the bus at ~18 connects/s.

---

## CONFIRMED findings (reproduced; probes in `audit/evaluations/`)

### SEV-1

#### #26 — Outer deadline abandons retry work on non-daemon threads; bulkhead clogs; exit blocked
- **Where** `client/resilience.py:_run_with_resilience` (`future.result(timeout=…)`), bulkman `ThreadPoolExecutor`.
- **Scenario** Bus stalls (no RST). `AgentBus(timeout=2).whoami()` → caller gets `TransportError` at **7.1 s**;
  attempts 3 and 4 still fire at 5.8 s and 9.9 s; **process exits at 15.2 s**. Then: 8 stalled callers, followed
  by one call to a *healthy* server in the same process → **fails at +14.1 s** ("did not complete within 7s"),
  healthy server served **0** requests. With the default `timeout=30`, each zombie lives ~110 s.
- **Customer harm** CLI hangs ~90 s after printing an error; hooks (`session-start`, `pending`) overrun their
  10 s harness timeout; the 540 s Stop-hook monitor overruns its 600 s timeout; a watcher/monitor stays deaf
  for minutes *after* connectivity returns.
- **Smallest fix** A cancellation flag checked before each retry attempt (set when the outer deadline fires)
  and per-attempt httpx timeouts bounded by the remaining budget; make the pool not block exit
  (`atexit` → `shutdown(wait=False, cancel_futures=True)`, or daemon workers). Prefer driving httpx's own
  timeout as the single source of truth rather than racing it with `future.result`.
- **Class** "timeout returns control but not resources". Also present (by design, but unaccounted) in the async
  client: `asyncio.wait_for` cancels the work but **bypasses breaker accounting** (S9).
- **Probe** `probe_outer_deadline_cancels_work.py`

#### #24 — Neither circuit breaker ever opens
- **Where** `client/resilience.py:_sdk_safety_net`, `rewake.py:_build_resilient_poll`; `resilient_circuit/failsafe.py`
  (`for policy in reversed(policies): func = policy(func)` → breaker outermost), `retry.py`
  (`raise RetryLimitReached from last_exception`), `circuit_breaker.py` (`should_handle` False → `mark_success()`).
- **Scenario** 10 consecutive failing sequences through `bus._request` → breaker **CLOSED**, execution log `[True]`.
  8 failing rewake polls → **CLOSED**. Second defect in the same line: `Fraction(5, 5)` reduces to `1/1`, giving a
  **1-slot window** — bulkman's own source warns about exactly this. Fixing the classifier alone would make the
  breaker trip on *any single* transient failure and fail-fast everything for 30 s: fix both together.
- **Customer harm** No fail-fast during outages; compounds #26. The 0.9.43 note "sync breaker honours CB env
  knobs" tuned a breaker that cannot trip. `tests/test_sdk_resilience.py` has **no breaker-opens test**; the
  async breaker tests exercise a hand-rolled breaker that is fine — the suite is self-confirming here.
- **Smallest fix** `should_handle` accepts `RetryLimitReached` (inspect `__cause__`); window `Fraction(n-1, n)`;
  add a stub-driven "10 failures → OPEN → fast fail → cooldown → HALF_OPEN → close" test.
- **Probe** `probe_sync_breaker_opens.py`

#### #34 — Every `AsyncAgentBus.read()` / `thread()` crashes with `NameError`
- **Where** `client/async_messaging.py:383` — `return AgentBus.unseal_message(self, message)`; `AgentBus` is never
  imported in that module. Introduced by the `client.py` split (commit 009fc41, issuedb #19).
- **Scenario** `AsyncAgentBus(...).unseal_message({"text_body": "plain"})` → `NameError: name 'AgentBus' is not
  defined`. `read()` and `thread()` both call it unconditionally.
- **Customer harm** The async SDK cannot read mail at all on 0.9.4x. `tests/test_async_sync_parity.py`
  compares method names and signatures only — the archetype-4 self-confirming check.
- **Smallest fix** `from .sync_client import AgentBus` is circular; move `unseal_message` to `_Base` (it does no I/O)
  and drop both copies. Enforce `ruff --select F821,F811` in CI.
- **Probe** `probe_async_read_unseals.py`

#### #25 — An open breaker leaks `resilient_circuit.ProtectedCallError('')` to callers
- **Where** `resilience.py:_run_with_resilience` (`cause = exc.__cause__ or exc.__context__; raise`), bulkman
  `_execute_wrapper` (`error.__cause__ = e`).
- **Scenario** Force the breaker OPEN → `bus._request(...)` raises `resilient_circuit.exceptions.ProtectedCallError`
  with an **empty message**; `isinstance(e, AgentBusError)` is False. `cli.main()` has no handler → traceback,
  exit 1. The watcher survives (`except Exception`) but logs `stream dropped ()`.
- **Customer harm** Latent today *because of #24*; the moment #24 is fixed every SDK caller that follows the
  documented `except AgentBusError` contract crashes during a cooldown. Fix #25 in the same change as #24.
- **Smallest fix** In `_wrapped`, `except ProtectedCallError: raise TransportError("agentbus SDK breaker open …")`.
- **Probe** `probe_sync_breaker_opens.py` (second check)

### SEV-2

#### #27 — Clean EOF on the SSE stream → immediate, unlogged reconnect loop
- **Where** `watch.py:_stream_once` returns normally when `iter_lines()` ends; `run()`'s `while True` loops with
  no sleep and no message. Same path for `event: unauthorized` judged transient (`break`).
- **Scenario** Fake bus answers `200 text/event-stream` and closes: **217 stream opens in 12 s, 0 log lines**
  (143 in 8 s in the probe). A proxy/LB that closes idle upstreams, a draining uvicorn worker, or an overloaded
  gateway that sends headers then drops produces exactly this shape.
- **Customer harm** One watcher can consume the server's 30 QPS bulkhead; `watch-status` says RUNNING; nothing
  in any log explains it. Also: `_failures` is reset to 0 on every successful `raise_for_status`, so a server that
  accepts-then-drops defeats the ladder even on the exception path.
- **Smallest fix** Treat a normal return from `_stream_once` as a drop (log + `_backoff_and_drain`); reset
  `_failures` only after the stream has been open ≥ 30 s.
- **Probe** `probe_stream_eof_backs_off.py`

#### #28 — `watch-status` / `watch-stop` trust a pidfile PID; after PID reuse they report and kill a foreign process
- **Where** `cli.py:_watch_pids/_watch_pid/cmd_watch_stop` (`os.kill(pid, 0)` only); pidfiles are never removed on
  exit and persist across reboots.
- **Scenario** Pidfile naming a `sleep 300` → `watch-status` prints **RUNNING (rc 0)**; `watch-stop` **SIGTERMs
  the sleep**. After a reboot PIDs restart low, so a stale file from yesterday points at something real today.
- **Customer harm** False-green wake status; the house rule "kill only by PID" is satisfied in letter while the
  PID is the wrong process. (`session_end` uses a `ps` argv match instead — better, but the doctor-scope form
  `monitor-{agent}` is a prefix match: S8.)
- **Smallest fix** Verify `/proc/<pid>/cmdline` (or `ps -o args= -p`) contains `agentbus … watch … --agent <name>`
  before reporting or signalling; store start time beside the PID; remove the pidfile on clean exit.
- **Probe** `probe_watch_stop_verifies_pid.sh`

#### #29 — `Watcher._drain` has no progress guard: a non-advancing page hot-loops forever under the drain lock
- **Where** `watch.py:_drain` (`while True: batch = inbox(cursor) … cursor = max(cursor, seq)`); `follow()` same.
- **Scenario** Page whose rows have `agent_seq` null/0 or ≤ cursor → **1.97 M inbox calls in 2 s**, drain never
  returns, `_drain_lock` held forever → `_drain_async` becomes a permanent no-op → watcher RUNNING but deaf.
- **Customer harm** Silent total wake death plus a self-inflicted DoS on the bus. The trigger needs a server/shape
  regression (today's server returns monotonic seqs) — the client behaviour is CONFIRMED, the trigger SUSPECTED.
- **Smallest fix** If the page did not advance the cursor, log once and return.
- **Probe** `probe_drain_progress_guard.py`

#### #30 — `sealing.ensure_keypair` first-use race overwrites a key another process already holds
- **Where** `sealing.py:ensure_keypair` (`exists()` → `generate` → `write_text` → `_harden`); same shape in
  `ensure_signing_keypair`, `identity.device_id`, `onboarding._write_private`.
- **Scenario** 8 processes call `ensure_keypair("t")` at once on a fresh config dir → **8 distinct keys returned,
  7 processes hold a private key that no longer exists on disk.** A key one of them published or sealed to is
  unreadable forever (no recovery, by design).
- **Customer harm** Permanent loss of readability for mail sealed in that window. Realistic trigger: `setup`
  publishing while a hook/monitor/first send races it; `send-batch` alongside a watcher reply on a fresh box.
  Also, every one of these sites writes the secret with umask perms and `chmod`s afterwards (S2).
- **Smallest fix** `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)`; on `FileExistsError` re-read and return the
  key on disk.
- **Probe** `probe_keypair_first_use_race.py`

#### #32 — `setup opencode` writes the bound bearer key into `./opencode.json` mode 0644
- **Where** `onboarding._setup_opencode` → `_dump_json` (plain `write_text`). On this box: `opencode.json` contains
  `Bearer ab_sk_…` and is `-rw-r--r--`.
- **Customer harm** Any local user can read the agent's key; on shared CI boxes or mounted volumes the file is
  the credential.
- **Smallest fix** Write credential-bearing files through `_write_private`; tighten an existing file's mode.

#### #31 — `_sdk_bulkhead` lazy singleton is unlocked
- **Scenario** 16 concurrent first callers → **15 distinct `BulkheadThreading` instances** (15 pools).
- **Customer harm** The "one concurrency lane per process" guarantee is void under concurrent first use (thread
  pools, `asyncio.to_thread` fan-out); `_sdk_safety_net` / `_async_circuit_breaker` have the same pattern and
  only won the race by constructor speed.
- **Smallest fix** A module-level `threading.Lock` around the three lazy inits.
- **Probe** `probe_bulkhead_singleton.py`

### SEV-3

#### #33 — Stop-hook re-wake ledger is not re-checked under the lock → double wake
- **Where** `rewake.py:_monitor_inner` loads `seen` once; `_append_ledger` locks the append but not the decision.
- **Scenario** Two armed monitors (previous turn's still polling + this turn's) see the same new mail → both exit
  **2**, ledger holds the id twice.
- **Smallest fix** Under `flock`, re-read the ledger, recompute `fresh`, append, release.
- **Probe** `probe_rewake_ledger_single_wake.py`

---

## SUSPECTED (reasoned from code; not reproduced — ranked by harm)

- **S1** Stop-hook monitor overruns the 600 s hook timeout because #26 zombies hold the process; the ledger is
  written *before* `return 2`, so a harness kill loses that wake permanently. Both halves are confirmed; the
  harness interplay is not.
- **S2** Private keys and `.env` credentials are written with umask permissions then `chmod 600` (`sealing.py:177,419`,
  `identity.py:62`, `onboarding.py:201`) — a world-readable window. Fix with #30.
- **S3** No SIGTERM handler in the watcher: `watch-stop`, `session-end` and the plugin monitor all SIGTERM it, so
  `cmd_watch`'s `finally: coalescer.close()` never runs; a buffered envelope (≤ 2.5 s window) is lost while the
  cursor is already persisted past it.
- **S4** `_STOP_CMD` / `_SESSION_START_CMD` hardcode `$HOME/.config/agentbus/…` while `_config_dir()` honours
  `AGENTBUS_CONFIG_DIR` — with the override set, setup installs `stop-rewake.sh` where the hook never looks.
- **S5** `resilient_circuit.storage` calls `load_dotenv()` at import. Any `.env` up-tree from site-packages (the
  project's `.env` for a project venv) is loaded into `os.environ`; only the `AGENTBUS_API_KEY` stomp was mitigated
  (disk-wins). `AGENTBUS_BASE_URL` can still be stomped → the bearer is sent to a foreign host.
- **S6** `_key_really_revoked`'s last-resort classifier (`"401" in text`) turns any non-`AgentBusError` whose text
  contains "401" into a terminal exit 8.
- **S7** `rewake._unread_text` builds a new `AgentBus` (httpx client + pool) per poll and never closes it — socket
  churn across a 540 s window; use `with AgentBus(...) as bus`.
- **S8** `_monitor_pids(agent)` (doctor scope) substring-matches `monitor-{agent}` → `agentbus` matches
  `agentbus-ui…` → false green for prefix-sharing names.
- **S9** Async breaker: `raise last` re-raises one shared exception instance across callers (traceback growth,
  frame retention); half-open admits unlimited concurrent probes; `asyncio.wait_for` timeouts never reach
  `breaker.on_failure`, so a *slow* outage never opens it even after #24 is fixed.
- **S10** `_save_cursor` shares one `.tmp` path between the drain thread and the main thread; concurrent saves
  race (`FileNotFoundError` swallowed) and `watch-status`/doctor can read a torn file.
- **S11** `hooks.pending()` echoes the raw hook stdin JSON (`session_id`, `transcript_path`, `prompt`) to stdout on
  every prompt; for a UserPromptSubmit hook, stdout is appended to context.
- **S12** Architectural: the cursor is advanced and persisted *before* `on_message` runs → wake is at-most-once
  while delivery is at-least-once. A handler failure (exec timeout, hung notify-send) drops the wake for good;
  the Stop-hook monitor is the only backstop, and only on Claude Code.
- **S13** PreToolUse gate is fail-open by directive #107 (accepted); its fast-fail state file is read-modify-write
  without a lock, so parallel tool calls lose counts.
- **S14** `requires-python >= 3.9` but the matrix runs 3.10/3.11/3.13; `watch.py:459` uses parenthesised
  `with (…)` (officially 3.10+). 3.9 is declared and untested.

## Code quality

- **Q1 File-size cap (500 soft / 550 hard)** — violated by `cli.py` **4750**, `onboarding.py` **2544**,
  `hooks/claude_code.py` **2019**, `watch.py` **831**, `client/sync_misc.py` **523**.
- **Q2 ruff** — 150 findings with the repo's own config: 63 W293, 55 E402, 13 F401, 6 RUF100, 4 F541, 3 I001,
  plus **F821** (#34), **F811** (`base.py:24` re-import), F841, B007. Lint is not gating anything.
- **Q3 Post-refactor duplication** — every `client/*.py` re-declares `DEFAULT_BASE_URL`, `_ConcurrentFuturesTimeout`,
  `_log` and the same docstring; `models.py` defines `_SEAL_INFLATION_FACTOR` and `_DEFAULT_SERVER_MAX_ATTACHMENT_BYTES`
  twice; `base.py` imports `_server_max_attachment_bytes` twice.
- **Q4 Resilience bypasses** — `raw()` and `attachment()` call `_client` directly (no retry/breaker/bulkhead);
  `_as_message_id` performs a full GET **and decrypt** just to map a delivery id.
- **Q5 Self-confirming tests** — no sync "breaker opens" test; async/sync parity checks names only; chaos script
  covers ECONNREFUSED but not accept-then-close or stall.
- **Q6 Comment volume** — multi-screen narrative comments hide the control flow (e.g. `_save_cursor` "misnamed but
  preserved"); consider moving incident history to `SPECS/`.

## Known-open tickets, judged

- **#20** (monitor script park/retry) — the client-side half (`cmd_watch` exit-code normalisation) is in place;
  the script half lives in the agentbus repo. Adequate for its scope; does not address #26/#27.
- **#21** (reliability audit) — its EARS line "breakers trip under sustained failures and enter half-open" is
  **falsified** (#24); "without crashing, leaking unhandled exceptions, or deadlocking" is not met (#25, #26, #34).
  Recommend keeping #21 open until #24–#26 and #34 close.

---

## Closing statement

**What was exercised.** Single-threaded code reading of the full wake path (`watch.py`, `rewake.py`,
`hooks/claude_code.py`, `client/*`, `cli.py` watch/status/stop, `onboarding.py` setup paths, `sealing.py`,
`_signing.py`, `identity.py`) and of the two third-party resilience libraries' source. Eleven live reproductions
against fake servers on 127.0.0.1 (stalled, healthy, accept-then-close) and in-process stubs, at concurrency up
to 16 threads / 8 processes, durations ≤ 15 s each; the full unit suite (482 pass / 4 skip, 73 s); ruff with the
repo's config; the probe harness end to end.

**What was not tested.** Nothing ran against the production bus or a real harness: Claude Code's Stop-hook
timeout behaviour (S1), opencode's plugin monitor (lives in the agentbus repo), `session-end` reaping on a live
session, real SSE semantics of `/v1/stream` (keepalive cadence, `Last-Event-ID` replay, the 100-row backlog cap),
`doctor --wake`, encrypted-workspace sends against real recipient keys, attachment paths, the systemd/launchd/rc.d
service units, Python 3.9/3.10 behaviour (only 3.13 here), sustained multi-hour outages, and any multi-tenant or
authorisation surface (out of scope for a client review).

**What remains uncertain.** Whether #29's trigger (a non-advancing page) can occur with today's server; how often
#30's race window is hit in practice (it needs two first-use creators within a few ms); whether Claude Code
appends `pending()`'s echoed stdin to context (S11) or discards it; and the real-world frequency of
accept-then-close streams (#27) on the production edge — the mechanism is certain, the exposure is not.
