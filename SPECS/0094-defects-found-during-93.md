# 0094 — Defects found while fixing #93

Ticket: issuedb #94. Shipped in 0.9.107.

EARS SPEC:
- Each item below shall be fixed, or closed wont-do with a reason, in the same release as #90 to #93.
- If an operator's .gitignore has no trailing newline, then setup shall add its entry on its own line and shall leave the existing last line intact.
- If an error response body is JSON but not an object, then the client shall raise its typed error, not AttributeError.
- If a 2xx response body cannot be decoded, then the client shall raise AgentBusError with code `unreadable_response` and the status, on both twins, and shall not repeat the call.
- Every setting read from the environment shall fall back to its default with one plain warning, not raise.
- If a read-only command meets a broken settings file, then it shall report what it can and shall not exit on the file.

SYNTHESIS: bugfixes. The solution domain is robustness at process boundaries. The concepts are `null_is_absence_not_default` and the total entry point. Two leaf modules were added, `_env_numbers.py` and `_settings_env.py`. Importing the settings reader from `onboarding` costs a hook 219 modules, which breaks the hooks' cheap-import rule.

ALTERNATIVES:
- Item 5, a shared reader: a leaf module [CHOSEN] against importing from `onboarding` [REJECTED: 219 modules on each hook start].
- Item 6, read-only settings file: stop with one line naming the file [CHOSEN]. An atomic replace [REJECTED: it would overwrite a file the operator made read-only].
- Unreadable 2xx: a typed, non-transient error [CHOSEN]. Treat it as a transport failure [REJECTED: the server already answered, and repeating a POST that may have been applied is unsafe].

RESULTS:

| Item | Area | Outcome |
|---|---|---|
| 1 | setup | Fixed. All three writers use `_append_ignore_entries`. |
| 2 | SDK | Fixed, including a valid list nested 200,000 deep. |
| 3 | SDK | Fixed. A queued call cancelled on timeout drops its flag. |
| 4 | SDK | Fixed in `_env_numbers.py`, with one stderr line per distinct value. |
| 5 | identity | Fixed in `_settings_env.py`. |
| 6 | setup | Fixed. `cmd_setup` and `cmd_teardown` report the OSError in one line and exit 1. |
| 7 | setup | Fixed. A refresh that removed the MCP entry and could not add it back says so and writes the helper. |
| 8 | hooks | Fixed. Each stale wake line is handled on its own. |
| 9 | doctor | Fixed. Doctor reports a running monitor process. It no longer claims a stream. |
| 10, 11 | text | Fixed. |
| 12 | signin | Fixed. Exit 1 when the public key publish fails. |
| 13 | identity | Fixed. Non-UTF-8 `.agentbus/agent` reads as absent. |
| 14, 15 | text | Fixed. |
| 16, 17, 18 | tests and probes | Fixed, each with a mutation proof. Three probes also gained a harness failure report. |
| 19 | server | Reported to the server team over AgentBus on 2026-11-06. Their ticket is #420. The fix is ready and waits on the director's word. |
| 20 | SDK | Added in the last pass. MEASURED a 2xx body that is not JSON escaped every `except AgentBusError`. Fixed on both twins. |
| 21 | CLI | Added in the last pass. MEASURED `"env": null` and `"env": "x"` crashed the wiring note in `register` and the identity banner. Fixed. |
| 22 | CLI | Added in the last pass. MEASURED a broken settings file stopped `register` wiring with SystemExit. It is now a NOT WIRED note and the file is left as it was. |
| 23 | hooks | Added in the last pass. MEASURED an unencodable subject in the unread preview raised UnicodeEncodeError. It prints with escapes. |
| 24 | hooks | Added in the last pass. MEASURED a wake line with none of sender, subject, delivery_id printed `None: None`. It is now a bad line. |
| 25 | audit | Added in the last pass. `run_all.sh` printed PASS for a probe that skipped. It prints SKIP, and its header names the probe that needs network. |
