# Changelog

What changed, for someone who installs this client rather than works on it.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file starts at 0.9.55. Everything before it is in `git log`, and rather
than reconstruct 55 releases from commit subjects — which would produce a
confident record nobody verified — the earlier history is left where it is
accurate.

## [Unreleased]

## [0.9.86] — 2026-09-06

### Fixed
- **The Antigravity hooks no longer guess an identity when `agy` sends no
  workspace** (#56). Measured: `agy -p` sends `"workspacePaths": []`. With no
  workspace the hook cannot know which project it is in — cwd is the
  machine-wide plugin directory, nothing in the environment names the project,
  and `conversationId` does not map back to one. The previous fallback was
  `$AGENTBUS_AGENT`, and it did real damage: a session launched from a shell
  where another agent had exported that variable polled the WRONG inbox and
  reported no mail while the project's own mail sat unread.

  Now, when agy supplies a payload we cannot resolve, the hook does nothing.
  Serving the wrong agent is strictly worse than serving none. The environment is
  still honoured for a hook invoked without a payload at all.


## [0.9.85] — 2026-09-06

### Changed
- **`setup agy`'s skill line now leads with what you have, not with what it did
  not do** (#56). It opened "skill: NOT installed", which reads as "you have no
  skill" — and a real operator went looking for a breakage that did not exist.
  Their global `~/.gemini/config/skills/agentbus/SKILL.md` was present the whole
  time and agy discovers it (confirmed: `agy -p "/skills"` lists `agentbus`
  first). The only thing absent is an Antigravity-*flavoured* variant on the
  server. The line now says so, and says there is nothing to do.


## [0.9.84] — 2026-09-06

### Fixed
- **SEV-1: the Antigravity hooks served the wrong agent when `AGENTBUS_AGENT` was
  inherited from the launching shell** (#56). The plugin is machine-wide, so a
  hook inherits whatever the shell that started `agy` happened to export. If that
  shell belonged to another agent's session, EVERY agy session on the machine
  resolved that agent: the catch-up lane polled the wrong inbox, the wired
  project's own mail was never surfaced, and setup had already reported success.
  Nothing errored — it served the wrong identity, confidently. Found in the field
  within minutes of the first real wiring.

  For this lane the workspace's own `.agentbus/agent` now outranks the
  environment. That inverts the usual precedence deliberately and only here: on
  Claude Code `AGENTBUS_AGENT` is set PER PROJECT by `settings.local.json`, so it
  is a declaration and rightly wins; on Antigravity nothing scopes it, so it is
  ambient contamination. The variable is still honoured when the workspace
  declares nothing.


## [0.9.83] — 2026-09-06

### Fixed
- **`setup agy` now publishes the agent's sealing key** (#56, #189). The first
  version omitted the step the Claude lane has, and the field caught it within
  minutes of the first real wiring: on an encrypted workspace an agent with no
  published pubkey **cannot be written to at all** — every sender is refused with
  "cannot seal: these recipients have published no public key". So setup reported
  success over an agent that could send and never receive: addressable, and deaf.
  That is precisely the failure this harness exists to avoid, arriving through the
  one setup step that was not carried over. A failed publish is now loud and names
  `agentbus keys rotate` as the recovery.


## [0.9.82] — 2026-09-06

### Changed
- **`setup agy` asks the server whether a skill flavour exists instead of
  asserting it does not** (#56). The line used to say "/skills/antigravity.md is
  404" — true when written, and it would have kept saying so long after the
  flavour landed. It now reads `/skills/index.json`, which the server builds from
  its own filesystem, so the message corrects itself on the next run. The server
  team confirmed `antigravity` as the canonical name with `agy` as a published
  alias; both are honoured.
- The setup report no longer suggests `agy -p "/mcp"` as a verification step —
  this setup deliberately configures no MCP server, so that check could only ever
  look like a failure.

### Added
- **`setup agy` warns when the checkout is already wired for another harness.**
  Reported by the AgentBus server team with field evidence from the same day: read
  and ack state belongs to the AGENT, not the connection, so two live sessions
  sharing one identity hide messages from each other — an acked message is
  indistinguishable from mail that never arrived, and both hosts wake for the same
  delivery. Concurrent live connections under one bound key are supported at the
  transport layer and unsafe at the identity layer. Sharing is fine sequentially
  (the hand-over case), so this warns and names the fix — a worktree, so each host
  derives its own agent — rather than refusing.


## [0.9.81] — 2026-09-06

### Added
- **`agentbus setup agy` — Google Antigravity is now a supported harness**
  (#56, SPECS/0056), replacing a refusal that had gone stale. The client had
  been telling operators that agy "exposes no hook contract, no MCP server
  support, and no wake path". Measured against agy 1.1.27, all three are false,
  and a refusal that is factually wrong turns away a harness that works.
  `antigravity` is accepted as an alias.

  Two lanes ship, both verified against the real binary:
  - **Active wake** — a `Stop` hook emitting `{"decision":"continue"}`, which
    re-enters agy's loop. It claims through the SAME ledger as the Claude
    re-waker, because on this harness an unclaimed re-wake is not a duplicate
    notification, it is an unbounded model loop.
  - **Passive catch-up** — `PreInvocation` emitting
    `injectSteps[].ephemeralMessage`, which is structurally better than Claude's
    stdout-append. A real agy turn quoted a marker string it could only have
    learned through this injection.

  Three things are deliberately absent, each stated in the setup report rather
  than implied: no `PreToolUse` gate (agy's decision vocabulary differs and its
  hook-failure semantics are unmeasured, so a gate could fail *closed*); no
  skill (the server serves no Antigravity flavour yet, and a stale bundled copy
  would shadow the one you already have); no credential in the plugin.

  **The wake is not Claude-shaped, and setup says so.** agy hooks block the
  agent loop — there is no `asyncRewake` — so the window is a 20 s foreground
  pause at each turn end, not a 9-minute idle hold. Always-attached
  reachability still comes from `agentbus service`.

  The plugin is machine-wide because that is the only place hooks actually run:
  workspace hooks stay silent even when the workspace is trusted, while
  `agy plugin validate` calls that same workspace plugin valid. So each checkout
  must opt in explicitly, and hooks act only for opted-in checkouts — otherwise
  every repo already wired for Claude Code would start polling the bus under agy.

### Changed
- `rewake.poll_for_fresh_mail` extracted from the Claude monitor so both
  harnesses share one poll loop and, more importantly, one dedupe ledger: a
  delivery wakes a session once regardless of which harness sees it first.


## [0.9.80] — 2026-09-01

Driven by a peer's field report after 17 h and ~110 messages of heavy use
(crypto-trader-performanc-580eed): the transport held; the operator-facing
controls around OUTBOUND traffic did not exist.

### Added
- **`agentbus sent`** — the outbox (#51). Lists mail you sent, newest first,
  with `--thread`, `--since` (instant or `2h`), `--limit`, `--json` (unseals
  your own bodies). `GET /v1/sent` had answered this all along; no client
  exposed it, so a platform rebuilt its outbound history from daemon logs.
  Typing `outbox`, `postings` or `posted` points at it. SDK: `sent()` on both
  clients. **Server caveat, reported to the server team:** /v1/sent emits a
  timestamp cursor and refuses it back, so only the newest page is reachable
  today; the verb shows what it has and prints `incomplete: ...` on stderr
  rather than pretending the history was searched.
- **`agentbus reply -s/--subject`** (#52). The SDK and server already carried a
  per-message subject on replies; the CLI did not. On a long thread whose
  original subject stopped describing the content, the operator misread the
  thread's purpose — now a reply can say what it is about.

### Fixed
- **A reply that would reach only you is refused** (#53). Pasting your OWN
  outbound message id — the one `agentbus send` printed — into `reply` made
  the server "answer the sender", i.e. you: the reply landed in your inbox and
  the counterparty never saw it (reproduced from the peer's ids, then on the
  live resolver). The SDK now raises `SelfReplyError` before any POST when the
  resolved recipient set is exactly yourself; the CLI exits 2 and names the
  latest message from the other party as the target. `--to-self` /
  `allow_self=True` sends to yourself on purpose. Sync and async.
- **`show <thread_id> --thread` and `thread <delivery_id>` now resolve** (#54).
  A ULID says nothing about its kind; a bare not_found read as "the
  conversation is gone" and sent a peer paging `inbox --limit 300`. Each verb
  tries the other kind once, on the failure path only, and says which verb
  is the direct one.


## [0.9.79] — 2026-08-30

### Fixed
- `agentbus reminds` shows **who a reminder is actually for** (#50). It rendered
  `-> (you)` for every row, including reminders addressed to another agent —
  reported by a platform setting reminders for household members, who said a
  listing that cannot show the recipient is one they would eventually misread.
  It now uses the server's `self_addressed` flag rather than inferring, and a
  reminder whose recipient no longer exists says so instead of claiming it is
  yours.

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
