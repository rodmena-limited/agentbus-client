# 0093 — Smaller defects found during the #89 refactor

Ticket: issuedb #93. Shipped in 0.9.107.

EARS SPEC:
- Each item below shall be fixed, or closed wont-do with a reason. None are in #89, which changes no behaviour.
- If an operator file, an environment value or a server body has an unexpected shape, then the client shall report it in one plain line and shall not raise a traceback.
- Where a test claims to catch a bug, reintroducing that bug shall turn the test red.

SYNTHESIS: bugfixes. The solution domain is robustness at process boundaries. The concepts are `null_is_absence_not_default` (issuedb memory) and the total entry point. Items 17 and 21 add a location or a module, recorded below.

ALTERNATIVES:
- Item 15, annotate `_request` honestly [REJECTED: a union costs 155 mypy errors in 16 files and `Any` costs 37 casts plus noqas, for 108 call sites, and the public methods stay as untrue]. Left as it is [CHOSEN].
- Item 17, the opencode skill path: opencode's own skills directory [CHOSEN, where opencode 1.19.3 discovers it]. Keep `~/.claude/skills` [REJECTED: `skill_state` compares that file against the Claude flavour].
- Item 21, `--exec` values with shell meaning: environment variables with a reference in the command [CHOSEN, it holds in any quote context of the template]. Refuse a quoted placeholder [REJECTED: the tool would be deciding what a template may say]. Escape per quote context [REJECTED: more parsing of a shell grammar, with the same limit].

RESULTS. Items marked READ in the ticket were reproduced before they were fixed.

| Item | Area | Outcome |
|---|---|---|
| 1 | test | Fixed. The oversize test records opens through the interpreter's audit hook. |
| 2 | test | Fixed. New tests cover the pending wake-file fallback, including the real hook commands against a refused connection. |
| 3 | doctor | Fixed. "matching what is installed" prints only after two release versions compared equal. |
| 4 | doctor | Fixed. The unreachable text is replaced by a line printed when the Stop re-waker probe ran clean. |
| 5, 6, 7 | operator files | Fixed. Null reads as absent. A wrong type or an unparseable file is refused by name before provisioning. |
| 8 | doctor | Fixed. A hook printing JSON that is not an object is a plain failure. |
| 9 | setup | Fixed. OSError and timeouts from `claude mcp add/remove` take the non-zero exit path. |
| 10 | signin | Fixed. The credential is stored and the run exits 1. |
| 11 | gate | Fixed. A state file holding JSON that is not an object reads as empty, and the degraded record is written. |
| 12 | hooks | Fixed. rewake's poll lets KeyboardInterrupt and SystemExit through. |
| 13 | gate | Fixed. A base URL that is not http or https counts as unreachable and the call runs UNVETTED. |
| 14 | SDK | Fixed. `AGENTBUS_SDK_MAX_RETRIES` is read once in `client/_repeat.py`. A negative value means 0 retries. |
| 15 | SDK | Wont-do, reason above. |
| 16 | SDK | Fixed. Policies and the bulkhead are built before the cancel flag is registered. |
| 17 | opencode | Fixed, and moved to `$XDG_CONFIG_HOME/opencode/skills/agentbus/SKILL.md`. Listing depends on the server fix in #94 item 19. |
| 18 | gitignore | Fixed. Both setups use the git root's `.gitignore`. |
| 19, 20 | text | Fixed. |
| 21 | watch | Fixed in `_watch_exec.py`. A template that hands a placeholder to a second shell (`sh -c`, `eval`, `ssh`) still evaluates it there, as the old code did. |
| 22 | CLI | Fixed. `_explain_limit` reads `exc.body`. Whether the live server sends `oldest_seq` was not checked. |
| 23 | CLI | Fixed. Each `_read_body` failure prints one line and exits 1. |
| 24 | CLI | Fixed. `join` accepts only http and https URLs with a host. |
| 25 to 32 | tests and tools | Fixed. Mutation proof for each test item: the bug reintroduced by hand, the test went red, `src` restored. |
