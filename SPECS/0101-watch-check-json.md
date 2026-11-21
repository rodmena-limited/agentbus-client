# #101: watch-check --json: holder kinds as fields

Ticket: issuedb #101

EARS SPEC:
- Where --json is given (after the verb or before it), agentbus watch-check shall print {agent, free, holders: [{kind, pid, path|state_key, session}]}, where kind is one of omo, session_monitor, watch.
- The client shall classify a registration as session_monitor when its state-file name ends in -<uuid>.json(.pid), and session shall be that uuid. Every other registration, the legacy slot included, is watch with session null. An omo holder carries path (the lock file) and session null.
- --harness omo and --session ID shall filter holders in the JSON exactly as in the text output.
- The text output and the exit codes (0 free, 4 held) shall not change.

Requested by agentbus-8dc08d (thread 01M7K0R7V99SD3YQ72KQ0K9JFE, message 01M7K6J657BQ4C724GDVNVZSKS): plugin 0.6.33 greps watch-check text for '^  omo session' and the session-uuid label, because exit 4 alone also counts a daemon watch, which wakes no Claude session.

SYNTHESIS: localised. The solution domain is a machine-readable CLI contract, using the codebase's common-verb --json flag and the shared `_print(..., as_json=True)`. The uuid pattern is the one the plugin monitor already uses (SESSION_LABEL in agentbus-monitor.sh). No new component.

ALTERNATIVES:
- A kind field on each holder [CHOSEN]. The plugin can branch on kind without parsing text.
- Separate exit codes per kind [REJECTED]. Several holders can coexist, and one exit code cannot carry a set. It would also change the 0/4 contract.

## Verification (2026-11-21)

- tests/test_watch_check_json.py, 9 tests, using real live processes and real pidfiles and an omo lock in an isolated config dir. With `_watch_omo.py` reverted, 8 fail and 1 passes. The one that passes is the text-unchanged test, which is meant to hold either way.
- Against this host's real config dir: agentbus-client-c70fbf -> one session_monitor with session 61cc04b6-499d-4a88-b46e-b623fa4d2835, exit 4. bid-desk-af6c6b -> one omo holder with the lock path, exit 4. An agent with no watcher -> free true, exit 0.
