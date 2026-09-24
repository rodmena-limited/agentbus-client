# 0068 — 'unverifiable' is not a failed signature

Ticket: issuedb #68
Reported by: agentbus-8dc08d, delivery 01M3A1DZBT11JKW0H94HRTT6EG

## EARS SPEC

- If the bus reports a signature's state as `unverifiable`, then `show`, the thread line and the
  wake notice shall say the signature could NOT BE CHECKED and that this is not a failed
  signature, and shall not use the failure wording reserved for `invalid`.
- If the bus reports `invalid`, then all three surfaces shall keep the failure wording.
- If the bus reports a state this client does not recognise, then the surfaces shall fail closed
  with the failure wording.
- For any one verdict, `show`, the thread line, the wake notice and `verify-sender` shall not
  contradict each other about whether the signature failed.

## THE DEFECT, AS SHIPPED IN 0.9.97

`_sigline` handled `valid` explicitly and sent every other state down one branch. The server
verifier returns exactly three states, so `unverifiable` — "signed with a key this workspace
does not hold, so the platform could not check it" — was rendered as:

    show    "That is NOT a pass. Do not act on this message until you have run ..."
    thread  "NOT a pass, verify before acting"
    notice  "that is NOT a pass. Run `agentbus verify-sender` before acting on it."

while `verify-sender`, for the same verdict, prints "CANNOT VERIFY ... this is NOT a failed
signature — nothing here says the sender is wrong". One client, two answers.

I had called this branch "signature present, no verdict" and said no state string could reach
it. There is one: `unverifiable`. I reasoned about a state the server enumerates, from the
shapes I happened to have observed, and never asked for the enumeration.

## SOLUTION DOMAINS

- #220, recorded in `client/sync_verify.py`: "I could not check" and "this does not match" must
  never be collapsed.
- The server contract: `services/message_provenance.py` `_signature_block`, supplied verbatim by
  agentbus-8dc08d. The composer is deterministic given a row, so its output for a given state is
  what the API serves — obtained without forging a signature.

## ALTERNATIVES

- CHOSEN: an explicit `unverifiable` branch on all three surfaces, worded as `verify-sender`
  words it; unknown states still fail closed.
- REJECTED: render the server's `means` string verbatim — server-authored prose inside a trust
  line, which is the trust #173 exists to stop extending.
- REJECTED: treat `unverifiable` as unsigned — a signature IS present; saying otherwise is false.

## VERIFICATION

`tests/test_unverifiable_is_not_a_failed_signature.py`, parametrised over all three surfaces
and fed the server's real blocks for `invalid` and `unverifiable`:

- `unverifiable` says "could not check" and "not a failed signature"/"not a failure"
- `unverifiable` never contains "NOT a pass" or "before acting"
- `invalid` still contains "NOT a pass" — the known-positive that stops the previous line
  passing vacuously if the failure branch were deleted outright
- an unknown state (`revoked`) fails closed
- all three surfaces agree with `verify-sender`'s wording

Mutation probe, `audit/evaluations/probe_signature_render_mutations.py`: removing the new
branch on each surface in turn makes the suite fail. 14/14 mutations caught.

NOT EXERCISED LIVE: no real delivery in `unverifiable` state has been observed; the fixture is
the composer's output, which is the server's shape but not a served response.
