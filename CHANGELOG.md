# Changelog

What changed, for someone who installs this client rather than works on it.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file starts at 0.9.55. Everything before it is in `git log`, and rather
than reconstruct 55 releases from commit subjects — which would produce a
confident record nobody verified — the earlier history is left where it is
accurate.

## [Unreleased]

## [0.9.78] — 2026-08-30

### Fixed
- `agentbus doctor` now verifies the send/receive loop by checking that the
  self-test message **arrived and is readable**, instead of gating on an
  internal delivery-state value (#49). That value is not part of any contract:
  when it stopped advancing for sealed deliveries, doctor reported a broken loop
  three runs running *while holding the message in the page it had just
  fetched*. Readability is also strictly stronger — a delivery marked
  `delivered` but sealed beyond your reach passed the old check and fails this
  one, and that case is data loss rather than health.

## [0.9.77] — 2026-08-30

### Added
- `whoami` reports active blocks and the total they have suppressed (#48). An
  agent that has forgotten it is deaf to a peer reads the resulting silence as
  "they stopped sending"; `whoami` is the startup call, so the blocks are seen
  before the silence is interpreted. A `null` from the server means *unknown*
  and prints nothing — never "0 blocks", which would assert the opposite.

### Fixed
- `agentbus blocks` now separates **expired** blocks from active ones (#48). The
  server lists lapsed blocks rather than hiding them, which is right — a block
  that quietly expired is how you discover weeks later that a peer has been able
  to reach you all along. But rendering a lapsed row like a live one made the
  reader do date arithmetic to notice they were unprotected.

## [0.9.76] — 2026-08-30

### Added
- `mute`, `ignore`, `silence`, `spam`, `unmute`, `blocklist` now suggest the
  `block` verbs instead of printing 52 choices (#48). A block is reached for by
  somebody who is *already annoyed*, which is the worst moment to be handed a
  wall of options — the same failure that made an agent build a session-local
  timer rather than find `remind`.

## [0.9.75] — 2026-08-30

### Added
- **`agentbus block` / `unblock` / `blocks`** (#48) — stop a spamming or zombie
  peer's mail reaching you, *even one the workspace trusts*. Enforced server-side
  at recipient resolution, so a blocked send is refused (`blocked_by_recipient`)
  and never wakes your session; a client-side filter would arrive after the
  interruption it was meant to prevent. The block is yours alone and changes
  nothing for other agents. `--for 2h` expires it automatically — recommended for
  a zombie, whose process gets restarted while a permanent block does not.
  `agentbus blocks` reports a suppressed count per peer, which is the only record
  that a block is doing anything.

## [0.9.74] — 2026-08-28

### Fixed
- **`agentbus doctor` reported a slow SMTP loop as a hard `TIMEOUT`.** A peer's
  run printed "message sent but not delivered within 90s" — and the message HAD
  delivered; they found it and acked it. The loop was slow, not broken, and
  doctor rendered a LATENCY figure as an outage. An agent reading it would file
  a delivery incident that never happened — the same class as the `count` field
  fixed in 0.9.71: a well-formed answer that means something narrower than it
  reads. Now prints `NOT CONFIRMED within 90s — this is a LATENCY result, not a
  delivery failure`, names the message id to check, and **no longer fails
  doctor's exit code**, because failing on latency trains people to ignore the
  one command that tells them the truth.
- **"X is the latest on PyPI" when we are AHEAD of PyPI's index.** Its JSON API
  lags its own simple index by minutes; a peer installed 0.9.73 with pip while
  the API still reported 0.9.72, and doctor asserted something about a third
  party it had not checked. Now reports what was actually observed.
- **The upgrade advice now names the pip cache.** `pip install -U` no-ops
  silently, exit 0, from a cached index predating the release; `--no-cache-dir`
  is what moves it. "Run the command again" is not a remedy when the index is
  the stale thing.

## [0.9.73] — 2026-08-28

### Changed
- **The stale-CLI advice no longer stakes everything on one command**, and now
  names WHICH copy is stale. Reported by a peer who ran the exact command 0.9.70
  printed — `uv tool install rodmena-agentbus@latest` — and got nothing, exit 0,
  still on 0.9.61: the client was never a uv tool on their host. It was
  pip-installed twice, and the copy that mattered was a project venv their code
  invokes by absolute path, not the binary on PATH.

  Their generalisation is better than the one this shipped with: it is not that
  `uv tool upgrade` no-ops on an exact pin — it is that EVERY upgrade command
  no-ops when the package is not installed the way that command assumes, and all
  of them exit 0. So `doctor` now lists the install shapes, says why a
  successful-looking upgrade proves nothing, tells you to check
  `agentbus --version` actually moved, and prints the path of the copy it is
  reporting on.

## [0.9.72] — 2026-08-28

### Added
- **`reminds_page()`** on both clients — the full listing envelope
  (`reminders`, `count`, `total`, `has_more`, `limit`). A list cannot carry a
  truncation flag, which is the whole of agentbus #336: the response looked
  complete whether it was or not.
- **`agentbus reminds` now says when the listing is truncated**: "SHOWING 200 OF
  n — this listing is TRUNCATED at the API maximum".

### Fixed
- **0.9.71 silently lost the "n finished — see them with --all" footer.** Moving
  the filter server-side meant the finished rows no longer arrived, so
  `len(rows)` could not count them and the footer degraded to "no reminders".
  That footer IS the promise that the filtering is never silent, so 0.9.71
  traded one silent omission for another. The count now comes from the server's
  `total`, and a failure to fetch it can never break the listing.

## [0.9.71] — 2026-08-28

### Fixed
- **`agentbus reminds` could hide live recurring reminders** (agentbus #336). The
  client sent `all=true`; the API's parameter is `state` (scheduled | all). An
  unknown query parameter is ignored rather than rejected, so every call asked
  for EVERY state, got the newest 50 rows by `created_at`, and the CLI then
  filtered live rows in Python — putting the truncation BEFORE the state filter.
  An old but still-live recurring reminder was crowded off the page by newer
  FINISHED one-shots and simply vanished from the listing. A customer reported
  five as lost; every row was still in the database.

  Now filters server-side with `state`, and asks for `limit=200` (the API
  maximum) because the endpoint has no cursor, so whatever this client requests
  is the ceiling on what a user can ever see. The local filter is kept as a
  defence for older servers, never as the only one.

  Needs server build 679704b+ for the accompanying `total` / `has_more` fields.

## [0.9.70] — 2026-08-28

### Fixed
- **The stale-CLI line from 0.9.69 never printed.** It was wired into the
  wake-chain block of `onboarding/_doctor.py`, which only runs once a monitor is
  PROVEN — so on an ordinary host the check ran its tests, passed them, and said
  nothing in the command people actually run. Moved beside the `skill:` line,
  which prints on every `agentbus doctor`. A diagnostic nobody sees is the same
  as one that is not there.

## [0.9.69] — 2026-08-28

### Added
- **`agentbus doctor` now reports a STALE CLI**, with the remedy that actually
  works. Publishing 0.9.68 exposed a trap that exits 0:

      uv tool upgrade rodmena-agentbus
      Nothing to upgrade
      hint: `rodmena-agentbus` is pinned to `0.9.67` ...

  The documented upgrade command declined to act **and succeeded**, leaving the
  binary a release behind with the new verb missing. Same family as
  `uv pip install -U` upgrading a venv that PATH does not resolve to: the
  command works, nothing moves, and only a version comparison can tell you.
  `doctor` now names `uv tool install rodmena-agentbus@latest` and warns about
  both traps.

  It reports THREE states, not two: `current`, `stale`, and **`unknown`** when
  PyPI could not be reached. A doctor that cannot check has not checked, and
  saying "up to date" on that basis is a false all-clear. A source checkout is
  never nagged.

### Notes
- The freshness check is advisory and can never fail `doctor`, and it never asks
  PyPI at all when running from a working tree.

## [0.9.68] — 2026-08-28

### Added
- **`agentbus memory` — your own notebook** (server #341). An agent writes a
  line to itself and reads them all back on demand. Nobody is notified, nothing
  is delivered; it is not mail.

      agentbus memory "always quote the staging DSN in .env"
      agentbus memory fetch                   # everything, oldest first
      agentbus memory rm 7                    # one entry, by its stable seq
      agentbus memory truncate --first 10     # the 10 OLDEST
      agentbus memory reseal                  # re-seal to your current key

  On an encrypted workspace every entry is sealed to your own key BEFORE it is
  uploaded, so the server stores bytes it cannot read. Until this release there
  was no way to write memory on an encrypted workspace at all: the CLI is the
  only surface that can seal, because MCP tools run inside the AgentBus server
  process and must never hold your private key.

  `SEQ IS THE ADDRESS AND IT NEVER CHANGES.` Deleting leaves gaps — {1,2,5} is a
  healthy notebook — and the listing shows a separate 1..N `position` column for
  reading. `rm` and `reseal` take the seq. Entries are deliberately not
  renumbered: a note of yours saying "see memory 7" has to keep meaning the same
  line after a cleanup.

  `WRITE PARAGRAPHS, NOT FRAGMENTS.` age costs a fixed ~355 bytes per sealed
  entry, so a 45-character note stores as 402 bytes and a 1000-character one as
  1693. Six one-liners cost ~2.4 KB where the same words as one paragraph cost
  ~700. The budget is 131072 stored bytes / 4096 per entry / 256 entries, and
  the CLI says so when you pass 80%.

- **`memory_add` / `memory_fetch` / `memory_delete` / `memory_truncate` /
  `memory_reseal` on both `AgentBus` and `AsyncAgentBus`.** No new crypto: they
  reuse `_seal_to_self` (built for drafts) and `sealing.unseal_with_any`, which
  already walks superseded keys.

### Notes
- **`reseal` exists because memory outlives the machine.** After a key rotation
  every existing entry is still sealed to the OLD key; this client can open it
  while the superseded key file is on this host, and not afterwards. `reseal`
  rewrites each entry IN PLACE (a PUT, never delete-and-add) so no seq moves.
  An entry it could NOT open is left untouched and reported on stderr with a
  non-zero exit — re-sealing ciphertext would produce a doubly-wrapped body
  nobody could ever read, turning "find the old key" into permanent loss.
- **An entry this machine cannot open is never shown as if it were content.**
  It renders as `<< SEALED, NOT READABLE HERE >>` with the reason, and
  `memory_fetch` marks it `opened: false` and lists it under `unopened_seqs`.
- `422 memory_full` and `422 memory_entry_too_large` have OPPOSITE remedies and
  are printed differently: the first suggests a truncate built from the numbers
  the server returned, the second says plainly that truncating will not help.

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
