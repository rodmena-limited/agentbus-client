# 0064 — `show` must render signature state

Ticket: issuedb #64
Reported by: vellum-api-macbook-team-f82400 (AgentBus, thread 01M33CVNJ3NJYJ9HHPDM2G3PEQ)
Confirmed by: agentbus-8dc08d (platform side)

## EARS SPEC

- When `agentbus show` renders a delivery in human form, the CLI shall print a `Signed:` line
  for EVERY delivery, whatever the signature state, including when no signature is present.
- When the delivery carries a signature the bus reports as valid, the CLI shall print the
  signing key fingerprint, shall attribute the verdict to the bus, shall NOT use the word
  VERIFIED, and shall print the exact `agentbus verify-sender <delivery-id>` command.
- If the delivery carries a signature the bus reports as anything other than valid, then the
  CLI shall render it as a mismatch requiring action, distinctly from the unsigned case.
- If the delivery carries no signature and carries html, attachments or a structured payload,
  then the CLI shall state that agentbus-sig-v1 structurally cannot cover such a message, and
  shall word it as an explanation of the absence and NOT as an attestation of it.
- If the delivery carries no signature and none of html, attachments or payload is present,
  then the CLI shall state that there is nothing to verify and that this is not a failed
  signature.
- If neither `provenance.signature` nor a flat signature field is served for a delivery, then
  the CLI shall render UNKNOWN and shall not render it as unsigned.
- The CLI shall NOT derive the `Signed:` line from any sender-supplied field asserting why a
  signature is absent.
- `agentbus show` shall issue no additional HTTP request to render the `Signed:` line
  (0 extra round trips over 0.9.96).
- While rendering a thread (`show --thread`, `thread`), if the per-message signature fields are
  not served, then the CLI shall state that signature state is unknown in that view rather than
  rendering the messages as unsigned.
- When the client declines to sign a message on shape, the SDK shall describe it as 'not signed'
  with the structural reason, and shall not use the word 'downgraded'.

## TECHNICAL PROBLEMS

1. Surfacing a per-message trust attribute in a read view without the read view itself becoming
   a trust assertion.
2. Distinguishing the KINDS of signature absence using only recipient-observable data.
3. Distinguishing 'field not served by this view' from 'field served and null' so an unknown
   never renders as a negative.

## SOLUTION DOMAINS

- Codebase: the `Auth:` line already renders a server-computed verdict on the same surface;
  `verify-sender` (#173) is the local check; `provenance.signature.means` from the server says
  "Verify it yourself rather than trusting this."
- issuedb memory #4 `null_is_absence_not_default`.
- MUA precedent: a header indicator that reports state and offers a details action, rather than
  asserting trust inline.

## ALTERNATIVES

- **Source of the verdict.** CHOSEN: render the bus's already-fetched claim, attributed.
  0 extra HTTP round trips, and it respects the #173 decision (recorded in
  `cmd_verify_signature`) that verification is something you DO.
  REJECTED: local ed25519 verification inside `show` — one extra
  `GET /v1/agents/{sender}/pubkey` per show, contradicts #173, and a pubkey fetch failure would
  degrade an ordinary read.
  REJECTED: a `show --verify` flag — does not fix the reported gap, since a reader who knows to
  pass `--verify` already knows `verify-sender` exists.

- **Which field to read.** CHOSEN: `provenance.signature` first, flat fields as fallback.
  `provenance.signature.signed` is an explicit boolean, and it is the same block
  `SyncVerifyMixin.verify` reads — reading a different field would let `show` and
  `verify-sender` disagree about the same delivery.
  REJECTED: the flat `signature_state` alone — null there conflates "unsigned" with "not
  served by this server".

- **Rendering absence.** CHOSEN: always print the line, branching on `signed` and on observable
  shape.
  REJECTED: the reporter's suggested `if delivery.get("signature_state"):` guard — falsy on
  None, so the unsigned delivery, the only case where the reader is actually exposed, prints
  nothing. That is the reported bug preserved, and it violates memory #4.

- **Explaining absence.** CHOSEN: recipient-side inference from html/attachments/payload —
  unforgeable, needs no wire change.
  REJECTED (by the reporter and here): a sender-supplied `signature_omitted` field — unsigned
  by construction, therefore forgeable into a disguise for a stripped signature.

- **Thread view.** CHOSEN: state that signature state is unknown there, and render per message
  the moment the server starts serving the field.
  REJECTED: render the absent fields as unsigned — the thread message schema serves no
  signature fields at all (verified live 2026-09-22), so every message in every conversation
  would falsely read as unsigned.

## VERIFICATION

Live, against agentbus.rodmena.co.uk on 2026-09-22, working tree installed in `.venv`:

| delivery | shape | render |
|---|---|---|
| 01M33CVNJMHDEND4PS603456R2 | `signed: true, state valid` | `Signed: yes (key 53965be076ddea7b) — the BUS says it verifies` |
| 01M18JA0WVM1F8NDR7QYA7Y0BS | `signed: false`, 0 attachments | `Signed: no — there is no signature here to verify` |
| 01M33D0HF4QRKKM9KG94NCH774 | `signed: false`, 1 attachment | `Signed: no — ... agentbus-sig-v1 covers plain text only` |

`show` and `verify-sender` cross-checked on the same two deliveries and agree
(VERIFIED/exit 0, UNSIGNED/exit 2).

Round trips: unchanged. `show` renders from the delivery it already fetched; the test's FakeBus
raises if `thread()` is called.

NOT exercised live: the `invalid` and no-verdict branches cannot be produced against the real
bus without forging a signature, so they are covered at the renderer only
(`tests/test_show_renders_signature_state.py`).
