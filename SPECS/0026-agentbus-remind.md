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

### Timing — resolved, and how

The 42s clock skew was real and is **FIXED**. Verified twice, and the second
measurement is the one that counts:

**Create path** — `Date` == `created_at`, `due_at` exactly `+delay`, six samples.

**Delivery path** — a reminder *watched to its fire*, polled every 8s:

    created_at  04:14:20.272   due_at 04:15:50.272   (exactly +90s)
      04:15:11  scheduled
      04:15:36  scheduled   <- 14s BEFORE due; a 42s-early scheduler
      04:15:44  scheduled      could not produce this observation
      04:15:52  FIRED       <- ~2s late; the delay is RunFlow's tick,
                                not an AgentBus sweep (there is none)

The negative half is what makes it trustworthy: still scheduled with 14 seconds
to go. The check could have failed and did not.

**Method note.** The original "-41s" finding was computed from two *stored*
timestamps, both of which were themselves skewed. Only watching the state flip
against a wall clock is immune to that. A stored timestamp cannot audit the
clock that wrote it.

### A third clock — the record is wrong even though the timing is right

Two of the three clocks are fixed; the one that writes the permanent record is
not. Reproduced independently on two machines:

    reminder due 04:15:50.272  ->  delivery.created_at 04:15:09.205   -41.1s
    reminder due 04:18:16.494  ->  message.created_at  04:17:36.193   -40.3s

| clock | what it stamps | state |
|---|---|---|
| create-request | `reminders.created_at` | FIXED |
| scheduler-evaluation | when it actually fires | FIXED |
| **message-write** | `deliveries.created_at` | **still ~-40s** |

**This is why the investigation took three rounds and why two correct testers
disagreed.** Computing `delivery.created_at - due_at` reads the third clock;
watching the state flip against a wall clock reads the second. Both methods are
sound, they answer different questions, and the answers legitimately conflicted.

The human-facing behaviour is CORRECT — reminders arrive on time and the wake
fires on time. What is corrupted is the **record**: anything ordering or
auditing by `deliveries.created_at` reads a reminder as arriving ~40s before
messages that genuinely preceded it. Silent and permanent. A wrong behaviour
gets noticed; a wrong timestamp gets trusted.

**ROOT CAUSE — fleet-wide, not a reminder bug.** The backend measured all four
production Postgres hosts directly (the one check no product interface can
answer):

    pg-nano-01   -6s    ntpd_enable=NO
    pg-nano-02  -32s    ntpd_enable=NO
    pg-nano-03  -42s    ntpd_enable=NO      <- agentbus, red9, mail_api
    pg-nano-04   -2s    ntpd_enable=NO

**No NTP daemon has ever run on any of them.** Never enabled, not
misconfigured — so every timestamp Postgres has written on these hosts is
suspect, not only recent ones.

Correcting an earlier claim of ours: this is NOT a residual of a partial fix.
The backend fixed one table (`scheduled_messages`) to stop mixing two clocks;
`deliveries` and everything else still stamp from Postgres, so clock (c) is the
**original defect, untouched, seen through a different field**. Our "residual"
hypothesis had the right location and the wrong story, and would have sent
someone hunting a regression that does not exist.

Scope: every Rodmena platform's database is on a free-running clock and they
have drifted up to 40s apart *from each other*, so cross-service correlation is
already unreliable fleet-wide. Each host is internally consistent, which is
exactly why nothing reports it.

**`deliveries.created_at` must not be trusted for ordering or audit until NTP
lands.** Not compensated client-side: an offset would be tuned to one host's
drift while four drift independently, wrong for three platforms and stale the
moment ntpd starts. There is no correct client-side fix.

Pending operator approval (Futex `dec_e18b364a0afc4b9a9aa8c44b144fef99`).

### Open defects

**Cron day-of-week was +1 — RunFlow's, not AgentBus's. NOW FIXED.**

Confirmed 4/4 here originally; the backend isolated it by removing their own
service from the path and querying RunFlow directly, reproducing the same +1.
Root cause was APScheduler's `CronTrigger.from_crontab`, whose `day_of_week` is
0=Monday..6=Sunday against standard cron's 0=Sunday..6=Saturday.

**Re-verified against the original reproduction after the fix**, Friday
2026-08-21:

    dow=0 want Sun got Sun   dow=1 want Mon got Mon
    dow=5 want Fri got Fri   dow=6 want Sat got Sat     4/4 PASS

It was the worst defect of the set while it lasted, because it was **silent**:
the reminder still arrived, just on the wrong day forever, so it read as the user
misremembering. Never compensated client-side — subtracting one from a user's
cron would have made the client lie about what it scheduled, and would have
broken at exactly this moment.

### `repeat_until` — settled: it does not exist and will not

**`--expire` IS the end date for a recurrence.** A separate `repeat_until` would
be a second name for a field that already exists, and the backend confirmed it is
not coming.

Correcting our own earlier framing: this is **not** "a recurrence has no end
date". It has one. Documenting the absence would have sent people building a
cancellation cron they do not need.

Demonstrated rather than asserted — the backend exercised it live after noticing
they had claimed it twice without ever running it, and we would have written that
unverified claim into this spec as documentation:

    repeat "* * * * *", expires_at 150s out
      fire 1 -> delivered
      fire 2 -> delivered
      expires_at passes
      state = EXPIRED, RunFlow schedule list 1 -> 0

**`--expire` does both halves**, which is what makes it a real end date rather
than a filter: the fire after expiry is not delivered, *and* the schedule is
cancelled upstream so it stops firing at all. A version that only withheld would
leave a recurrence firing into a void forever, consuming quota on both sides.

Reproduced independently here before documenting it, on a different reminder:

    repeat "* * * * *", expires_at 05:04:02 (150s out)
      05:02  fire 1 delivered
      05:03  fire 2 delivered
      05:04  fire 3 delivered
      05:04:02  expires_at passes
      05:04:20  state still `scheduled` — A DEFECT, see below
      05:05:10  state = EXPIRED
      05:05:24  deliveries still 3 on a per-MINUTE cron

The last line is the one that matters: a minute after expiry the count had not
grown, so the recurrence genuinely stopped rather than being withheld while
still firing.

#### That 05:04:20 line was a bug report, and it was filed as a caveat

It was recorded here as testing methodology — "the sweep had not run yet, so
sample with margin". **Wrong twice:**

1. **There is no sweep.** `deliver()` was the only place a row became `expired`,
   and AgentBus owns no clock by design — the state was waiting for the NEXT
   FIRE, not a timer. A plausible mechanism was invented to explain an
   observation, and the investigation stopped because the invention was
   satisfying. Identical failure to the "residual of a half-landed fix" story
   about the clock earlier the same night.

2. **The lag scaled with the cron interval.** A per-minute cron is the one
   cadence where it is nearly invisible:

       * * * * *      up to 60s      <- what was measured, read as a curiosity
       0 9 * * *      up to 24 HOURS
       0 9 * * mon    up to 7 DAYS

   A weekly reminder would report `state: scheduled`, and appear in the
   scheduled list, for a **week** after expiring — so anyone polling state to
   decide whether to re-arm gets the wrong answer for seven days.

   Every tester used a per-minute cron all night, because it is the only cadence
   observable inside a test run. **We were all testing in the blind spot.**

**Fixed** (backend `ec506da`): expiry is evaluated **on read**, so state is
truthful within seconds of the deadline rather than at the next fire.
`expires_pending_reap` exposes the remaining asymmetry honestly — the read is
truthful about the deadline while the upstream schedule stays armed until the
next fire reaps it.

An hourly cron with a short `--expire` is refused at create (*"expires_at is not
after the first fire"*), so the long-interval case cannot be exercised without
waiting hours. Read-on-access makes it correct regardless — **a guarantee should
not depend on being observable.**

Verified independently against the fix, on a per-minute recurrence expiring
05:14:48 — read 5 seconds later, well before the next fire at 05:15:

    state: expired   pending_reap: True

Under the old code that row read `scheduled` until 05:15.

**The lesson is not "sample with margin."** It is that an unexplained
observation should be investigated rather than narrated.

CLI, quickref and the `--repeat-until` refusal now redirect to `--expire`.

## Not verified

- The 429/503 refusal paths: descoped before testing. Never executed.
- `repeat_until`: absent by deployment.
- Recurrence firing more than once: assigned to a tester; a single fire is
  indistinguishable from a one-shot.
