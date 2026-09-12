# 0060 — The `agentbus` CLI on Click: grouped help, short errors, proven parse parity

Ticket: issuedb #60

## EARS spec

- When `agentbus` is run with no arguments, the CLI shall print a grouped command overview to stdout and exit 0.
- The overview shall list every non-deprecated verb exactly once, under one of at most 9 named groups, each with a one-line summary of at most 60 characters, name the deprecated verbs on a single footer line, and fit in at most 85 lines.
- When `agentbus <verb> --help` or `-h` is run, the CLI shall print that verb's usage, description, arguments and options, and exit 0.
- If a command line has an argument error, then the CLI shall print at most 6 lines to stderr naming the problem and `agentbus <verb> --help`, shall not print the verb list, and shall exit 2.
- If an unknown verb is typed, then the CLI shall suggest the intent-mapped verb (#50) or a difflib close match at cutoff 0.75 (#52), print no suggestion otherwise, and exit 2.
- For every command line in the oracle captured from the 0.9.94 argparse parser, the CLI shall produce an identical parsed namespace (every attribute, value and type, and the same handler) or the same exit code, except the deltas below.
- While stdout is not a TTY, or NO_COLOR is set, the CLI shall render help and errors without ANSI escape sequences.
- The CLI shall keep exit code 2 for usage errors (the emitted systemd unit's `RestartPreventExitStatus=2 3 8` depends on it) and 3/4/5/8 for AgentBusError/QuotaExceeded/ServiceUnavailable/AuthError.
- The CLI shall install and pass the full test suite on Python 3.9 (Click 8.1.x) and Python 3.13 (current Click).
- `agentbus --version` p50 startup over 15 runs shall not exceed the 0.9.94 baseline (144 ms p50, measured 2026-09-12) by more than 30 ms.
- `agentbus quickref --verbs` shall remain exactly the sorted verb set, unchanged from 0.9.94.
- `agentbus_client.cli.build_parser().parse_args(argv)` shall keep returning the parsed namespace, because an external probe (`agentbus/audit/evaluations/client/probe_cli_identity_matches_hooks.py`) calls it.

### Amendment (before implementation completed)

The first draft set the overview budget at 75 lines. Counted rather than guessed: 55 non-deprecated verbs, one per line, in 8 groups with a blank line between groups, is 70 lines before a header or footer. The budget is 85, and the two deprecated verbs (`as`, `sibling`) move to one footer line instead of a group.

## Deltas from 0.9.94, deliberate and tested

1. **No verb.** `agentbus` alone, or with only global flags, prints the overview and exits 0. It used to print the 57-choice usage wall and exit 2.
2. **Long-option prefix abbreviation.** argparse accepted `--subj` for `--subject`; Click does not, and says `Did you mean`. A scan of `src/`, README and both installed SKILL.md copies found no abbreviated use.
3. **Error wording.** Messages are Click's, reshaped into three lines (`error`, `usage`, `help`). Exit codes are unchanged.

## Technical problems

1. Behavioural parity of a declarative grammar: 57 verbs, 239 parameters, 2 nested groups (`keys`, `sibling`), 1 pass-through (`as`), 1 mutually exclusive pair, `--agent`/`--json` accepted on both sides of 44 verbs.
2. Equivalence verification between two parser implementations.
3. Human help presentation: grouping, short vs full help, TTY vs piped rendering.
4. Short usage errors with a remedy, keeping the #50/#52 suggester.
5. Dependency compatibility across Python 3.9–3.14.

## Solution domains

- CLI frameworks: Click, argparse, Typer.
- Characterization (golden-master) testing: capture the legacy system's observable behaviour as an oracle and test the replacement against it.
- Terminal rendering: `rich`, already a dependency; the `ui.py` plain-when-piped contract (#119).
- Codebase pattern: 57 handlers take `argparse.Namespace` and tests call them directly, so the adapter sits at the boundary.

## Alternatives

| Concept | Chosen | Rejected, and why |
|---|---|---|
| Parser | Click | argparse + rich-argparse: the subparsers action is a single argument, so verbs cannot be grouped or given separate short/long help without private-API overrides — the wall is structural. Typer: needs type-annotated signatures on all 57 handlers, breaking the Namespace contract tests call directly. |
| Help renderer | Click formatter plus `rich`, plain when piped | rich-click: overrides Click's formatting internals while this client must run on Click 8.1 and current Click at once, and draws box panels when piped. |
| Handler boundary | Namespace adapter in the Click command | kwargs handlers: 57 handlers and their direct-call tests change for no user-visible gain. |
| Parity proof | oracle JSON captured from 0.9.94 argparse before any code change, recaptured from a pristine `cbaae48` worktree | a live argparse differential kept in tests (a dead second grammar in the repo); hand-written spot tests (a fraction of 239 parameters). |
| Layout | `cli/_cmds_<family>.py` beside each handler module | decorators beside handlers: `_register.py` 499, `_directory.py` 482, `_keys.py` 463 lines, already at the 500 cap. |
| Abbreviations | dropped, with Click's did-you-mean | a parser subclass: Click's option parser is not public API. |
| Untyped options with a non-string default | a pass-through parameter type | Click's inferred type: `undeliverable --limit` defaults to 20 with no type, so argparse yields the string `"7"` where Click would yield the int `7`. |

## Verification (2026-09-12)

- **Parity:** 1,751 command lines captured from the 0.9.94 argparse parser at `cbaae48`, captured a second time from a pristine git worktree with byte-identical cases. Every case reproduces except the documented deltas (`tests/test_cli_parses_exactly_like_0_9_94.py`).
- **Suites:** Python 3.13 / Click 8.5.0 and Python 3.9.25 / Click 8.1.8, both green; final counts are on ticket #60.
- **Mutations**, each confirmed applied, run against a green baseline, and restored byte-identical:

  | Mutation | Result |
  |---|---|
  | `none_when_empty` ignored | 65 parity failures |
  | `--json` after the verb overrides the global | 45 parity failures |
  | `as` pass-through keeps the leading `--` | 1 parity failure |
  | mutually exclusive check disabled | 1 parity failure |
  | pass-through type dropped from `undeliverable --limit` | 2 parity failures |
  | suggestion cutoff loosened to 0.6 | 2 unknown-verb failures |
  | overview drops the first row of each group | 1 readability failure |
  | styling forced on when piped | 3 readability failures |
  | trailing padding not stripped | 1 readability failure |

- **Startup**, 25 interleaved subprocess runs per command, 0.9.94 vs Click, p50 change: `--version` −8.7 ms, `whoami --help` −9.9 ms, `send --help` −4.5 ms, `send` (usage error) −17.9 ms, bare −20.2 ms. The threshold was +30 ms on `--version`.
- **External consumers:** `agentbus/audit/evaluations/client/probe_cli_identity_matches_hooks.py` passes on 0.9.94 and on this tree with identical output. `agentbus/tests/test_skill_documents_the_cli.py` fails closed at line 140 against the new CLI, because it parses argparse's `choose from` list out of an unknown-verb error; reproduced, and announced to agentbus-8dc08d before publishing (message `01M2BVPEYQTEPHC7AH3DFWRX5A`). Against 0.9.94 that guard was already red, for an undocumented `sent` verb.
