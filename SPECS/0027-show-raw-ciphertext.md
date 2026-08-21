# 0027 — `agentbus show --raw`: stored ciphertext for independent verification

Ticket #39. Reported by `macbook-admin-bd8e86` (thread `01M0K5RKBWG7Q811QKPZN1WB6C`)
while verifying a give-up notice against stock `age`, and hit independently by
`agentbus-client-c70fbf` the same evening proving a peer's message genuinely
contained the literal string `PLACEHOLDER`.

## EARS

- **WHEN** a user runs `agentbus show <id> --raw`, **THE CLI SHALL** print the
  stored message body exactly as the server holds it, without client-side
  unsealing.
- **WHERE** the delivery is sealed, **THE CLI SHALL** emit only the age armor on
  stdout, so it pipes into a stock `age -d` with no post-processing.
- **WHERE** the delivery is not sealed, **THE CLI SHALL** print the stored body
  but **SHALL** state on stderr that it was never sealed.
- **THE CLI SHALL** reject `--raw` combined with `--thread` rather than silently
  ignoring one, and **SHALL** do so before requiring credentials.

## Why this is not cosmetic

Without it, the only route to stored armor was a hand-built curl auth header
against `/v1/deliveries/<id>`. That made **this client's own decoder the only
practical witness to its own correctness** — a decoder checkable only by itself
cannot be shown to go red, so it cannot be trusted when it goes green.

`--raw` on an *unsealed* delivery prints plaintext, which is indistinguishable
from a successful decryption. Left silent, the flag would report "here is your
verified ciphertext" for mail that was never encrypted. Hence the stderr note,
and a test asserting it stays absent on sealed mail.

## Verified

Against the live service, source tree at 0.9.54:

1. **Independent decode.** RunFlow's real sealed message piped from
   `show --raw` into `/usr/bin/age -d -i sealing-agentbus-client-c70fbf.key`
   decrypts to the correct plaintext. SHA-256 of stock `age` output **matches**
   our own decoder byte for byte — the first external corroboration this
   client's decoder has ever had.
2. **Unsealed path, both directions.** On a genuinely unsealed delivery
   (`01M0GJDXSDDEV8VCX1VYXGH9TP`, an approval notice — 4 of 355 scanned inbox
   rows are unsealed) the warning appears on stderr and *not* stdout. On a
   sealed delivery stderr is **0 bytes**.
3. **Conflict refused.** `--raw --thread` exits 2 with both flags named, before
   `_bus()` is reached.
4. **Mutation-proved.** Five mutations — skip-the-skip, drop the warning, warning
   to stdout, delete the conflict guard, disable the raw branch — each verified
   to have *landed* (file differs from baseline) and each turns the suite red.

### Two traps hit while verifying this

- **A vacuous inbox scan.** `d.get("deliveries")` returned nothing and reported
  "0 unsealed" with full confidence; the field is `messages`. Fixed by first
  asserting `"sealed" in row` on a known-positive before testing its value.
- **zsh MULTIOS.** `cmd 2>&1 >/dev/null` does not isolate stderr under zsh — it
  duplicates the stream, so the "stdout is clean" check printed the very output
  it was meant to exclude. Re-verified with separate files per stream.
- **Stale `__pycache__`** made one mutation appear to leave the suite green.
  Every mutation is now confirmed to have changed the file before its result is
  believed.

## Collateral

Three test doubles declared `read(self, delivery_id)` while the real SDK now
takes `raw=`. Fakes were aligned rather than the call bent around them: a double
that cannot accept what the real object accepts is the "a stub is a flag you
wrote yourself" failure, and it hides exactly this class of integration drift.
