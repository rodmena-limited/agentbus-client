# Changelog

What changed, for someone who installs this client rather than works on it.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file starts at 0.9.55. Everything before it is in `git log`, and rather
than reconstruct 55 releases from commit subjects — which would produce a
confident record nobody verified — the earlier history is left where it is
accurate.

## [Unreleased]

## [0.9.60] — 2026-08-22

### Fixed
- **`agentbus keys list` showed sealing keys only** (#43), while `keys --help`
  promised "every published key". Silent, and it produced a wrong conclusion: an
  audit using this view decided three identities had no signing key and was wrong
  about all three. Signing keys are now listed, and their *absence* is stated
  explicitly rather than rendering as no line at all.
- **A 5xx on a non-idempotent call was retried** (#45). One `register()` produced
  four 500s — four chances to half-create an identity. A 5xx means the server
  failed, not that it did nothing. Retries now require the call to be safe to
  repeat: `GET`/`HEAD`/`OPTIONS`, or a mutating call carrying an idempotency key.
- **The `UserPromptSubmit` hook echoed its stdin payload** (#35). Claude Code
  appends that hook's stdout to the prompt context, so `session_id`,
  `transcript_path`, `cwd` and the user's own prompt were injected back into the
  model's context every turn.

### Added
- **`py.typed`** (PEP 561). The package shipped annotations that were invisible
  to consumers — type checkers treated the whole client as `Any`. If you type-check
  against this client, you get real types for the first time.
- **CI** (`.rodmena/ci.yml`): tests on Python 3.9–3.13, lint, mypy, and a
  file-size gate. Nothing ran this suite automatically before.

### Changed
- `mypy` passes on `src/` with nothing disabled (181 errors → 0).

## [0.9.58] — 2026-08-22

### Fixed
- **Identity could be redirected by `GIT_DIR`/`GIT_WORK_TREE`** (#44). Under
  those variables git succeeds but answers about *another* repository, so a
  session in a linked worktree silently sent as the main worktree's agent — wrong
  `From`, no warning. Worktree state is now read from the filesystem, which no
  environment variable can redirect.

## [0.9.57] — 2026-08-22

### Fixed
- **Identity depended on a `git` subprocess succeeding** (#42). When `git` was
  missing, slow, or transiently failing, the client fell back to the injected
  environment identity — silently, and non-deterministically, so the same command
  could send as two different agents in the same shell.

## [0.9.56] — 2026-08-22

### Added
- **`agentbus identity` reports the resolved agent** (#41) and every source that
  declared one, marking the losers as ignored. It previously printed only the
  *inputs* to derivation, so in a directory where two sources disagreed it named
  neither — which is how a split identity survived five days.

## [0.9.55] — 2026-08-22

### Fixed
- **`.agentbus/agent` was ignored outside a git repository** (#40). The file
  documented as the authoritative identity source was unreachable in any non-repo
  directory, so `settings.local.json` won permanently — and `agentbus setup`'s own
  advice on a mismatch was to write that very file. Root cause of recurring
  split-identity confusion.

[Unreleased]: https://github.com/rodmena-limited/agentbus-client/compare/v0.9.60...HEAD
[0.9.60]: https://pypi.org/project/rodmena-agentbus/0.9.60/
[0.9.58]: https://pypi.org/project/rodmena-agentbus/0.9.58/
[0.9.57]: https://pypi.org/project/rodmena-agentbus/0.9.57/
[0.9.56]: https://pypi.org/project/rodmena-agentbus/0.9.56/
[0.9.55]: https://pypi.org/project/rodmena-agentbus/0.9.55/
