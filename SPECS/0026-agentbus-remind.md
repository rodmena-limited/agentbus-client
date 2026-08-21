# 0026 — `agentbus remind`: scheduled reminders

Ticket: issuedb #38. Plan: `~/.claude/plans/i-d-like-to-ensure-sleepy-sonnet.md`.
Threads: `01M0H0W8H0RZG54QTYTTZV5DGV` (design, with RunFlow + backend),
`01M0H6XS0B6CNX96MVW0XE94ST` (end-to-end testing).

## Context

Agents had no way to schedule anything: no "remind me in 2h", no recurring
nudge, no self-note. Farshid asked for it, and made scheduling an ecosystem
decision rather than a feature one:

> "RunFlow is the scheduler in our ecosystem"
> "we don't want agentbus to maintain its own scheduler"

RunFlow schedules; AgentBus seals, delivers and wakes; the client seals before
anything leaves the machine. **No client anywhere touches RunFlow** — one
platform credential, held server-side (operator ruling).

## EARS requirements

- Where an agent schedules a reminder without naming a target, the system SHALL
  deliver it to that agent (the self-note is the common case).
- When the acting workspace is encrypted, the CLIENT SHALL seal the reminder body
  before it leaves the machine, and the server SHALL store ciphertext it cannot
  read.
- Where a reminder names a target, the body SHALL be sealed to THAT agent's key,
  never to the author's — a reminder sealed to its author is delivered unreadable.
- If a target has no published key on an encrypted workspace, the system SHALL
  refuse rather than store plaintext.
- When a reminder's expiry has passed at fire time, the system SHALL NOT deliver
  it.
- Where the caller supplies a relative delay, the SERVER SHALL resolve it against
  its own clock, and the response SHALL echo the resolved instant in UTC.
- The client SHALL NOT hold, read, or transmit any scheduler credential.
- Where a field is absent, the client SHALL omit it rather than send null.

## Decisions, and who made them

| Decision | Owner | Reasoning |
|---|---|---|
| One timer per one-shot, one schedule per recurrence | Farshid | Cap is not binding: timers count PENDING only (1000 default, raised to 100,000) |
| `--target` defaults to self | Farshid | Self-notes are the common case |
| Delay resolves server-side | backend, proposed by us | A laptop 40s fast would fire everything 40s early, undiagnosably |
| `expires_at` absolute, not a duration | backend | "3d" is ambiguous about three days from WHAT |
| Timezone never defaulted client-side | backend | The server defaults to UTC and says so; a client guessing makes one reminder mean two things |
| `poke` dropped | backend | `remind --delay 0` is `send` with extra steps and one more failure mode |
| No abuse cap on `--target` | backend | Any agent can already `send` instantly; a cap on one verb and not its equivalent is theatre |
| 429/503 refusal paths descoped | Farshid | Not a release blocker |

## Deviation: `repeat_until` is not deployed

The agreed contract carried it. The **served** `RemindRequest` does not, and
forbids extra inputs — verified live with a full-scope key:

    POST /v1/reminders {..., "repeat_until": "..."}
    -> 422 repeat_until: Extra inputs are not permitted

That fails the entire create, so every recurring reminder with an end date would
have died on a field the user never knew was optional. The client refuses it
locally (0.9.47) and the flag stays documented as unsupported.

**A recurring reminder therefore has no end date and must be cancelled by hand.**
Open with the backend: is it coming, or is recurrence deliberately endless?

This is why the served artifact is diffed rather than the agreement re-read.

## Verification

Every check shown able to go **red** before it is trusted.

- `audit/evaluations/probe_remind_end_to_end.py` — live, self-addressed, cleans
  up, reports UNTESTED and DESCOPED separately and counts neither as a pass. It
  was red-proved against the pre-deploy 404 before it ever went green.
- The expiry check is a **negative** and is only meaningful paired with the
  positive: a 60s reminder is proven to arrive inside the window first, so
  "nothing arrived" is a decision rather than a broken pipe.
- Sealing is verified by **decoding**, not by reading `sealed: true`. A flag is a
  claim; the bytes are the fact.
- Independent testers on two other operating systems cross-check each other.
- The timezone default is **unfalsifiable from a UTC box** — "defaulted to UTC"
  and "used my local zone" are observationally identical there. Owned by the
  tester on BST; recorded UNTESTED elsewhere rather than passed.

## Live end-to-end results (2026-08-21, OOO session)

Against the deployed routes, through the **released 0.9.47** client.

| Check | Result | Evidence |
|---|---|---|
| One-shot delivers **and wakes** | PASS | `01M0H6WT7YM1HDMN9ZSEM2AP0G` → delivery `01M0H6ZKNNGCCJ6QSFRNZWJMH2`, woke the session |
| Body sealed at rest | **PASS, decoded** | raw stored body is age armor; sentinel absent; client recovers exact plaintext |
| Cancel prevents the fire | PASS | state `cancelled`, no delivery |
| Double-cancel is idempotent | PASS | second DELETE returns success, not an error |
| Recurrence fires >1 | **PASS, twice independently** | mine `*/2` → 03:51:23 + 03:53:18; macbook `* * * * *` → 03:48:21 + 03:49:23 (macOS) |
| Timezone default is UTC | PASS | macbook on **BST**: response says `timezone: UTC`, so silent-local is falsified |
| `expires_at` before first fire | **refused at CREATE** | better than the delivery-time check specified here; adopted |
| Expiry withholds a lapsed reminder | **UNTESTED** | blocked by the early-fire defect below |

### Defects found, reported, open

**Reminders appear to fire ~40s EARLY. ROOT CAUSE: a 42-second clock skew** —
the clock stamping reminder rows is behind the one serving HTTP. Not a scheduler
bug, not the client. Same instant, from the same service:

    GET /healthz        Date:       Fri, 21 Aug 2026 03:57:18 GMT
    POST /v1/reminders  created_at: 2026-08-21T03:56:36.423Z    (-42s)

Three consecutive samples: -42, -42, -42. A constant offset, not jitter. The
arithmetic follows: `delay_seconds=60` yields `due_at - created_at = 102.2s`,
and 102 = 60 + 42 — `due_at` computed from a correct clock, `created_at` from
the skewed one. Delivery lands at the right wall-clock moment while every
*stored* timestamp says it should not have yet.

Not local: this host and the API's `Date` header agree to the second, NTP
synced. Independently seen by a tester on macOS, whose figure differed (~66s)
precisely because the skew adds to whatever delay each caller requested.

Hypothesis offered to the backend, not a finding: a database host clock, which
would affect every `now()` default in the schema rather than only reminders.
Thread `01M0H7G8QD1D720T54SNV5JA0D`.

This **invalidates the expiry negative control**, and makes `--expire`
unreliable for short windows: a window shorter than the 42s skew can never
elapse, so a passing expiry test would be meaningless. Expiry-withholds-a-lapsed-
reminder is **not tested and not claimed**. I nearly filed expiry itself as
broken before isolating the clock.

**A pure recurrence is refused** — `--repeat "0 9 * * *"` with no `--delay`
returns 422 *"supply exactly one of delay_seconds or due_at"*, though a cron
already specifies its own first fire. Thread `01M0H72EMCA7FT8SMRGA6HQA82`.

Neither is shimmed client-side: a client offset counteracting a server timing
bug becomes permanent and hides the defect.

## Not verified

- The 429/503 refusal paths: descoped before testing. Never executed.
- `repeat_until`: absent by deployment.
- Recurrence firing more than once: assigned to a tester; a single fire is
  indistinguishable from a one-shot.
