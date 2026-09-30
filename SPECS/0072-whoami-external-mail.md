# 0072 — whoami shows whether mail from outside reaches this address

Ticket: issuedb #72. Server half: agentbus-8dc08d #354 (build fcc0186).

## EARS SPEC

- When `agentbus whoami` prints an address and the server serves `workspace.external_mail`, whoami
  shall print directly under it whether mail from outside is accepted.
- If the policy is null or its `accepts` value is unrecognised, then whoami shall print UNKNOWN
  and shall not imply the address is open.
- If the server sends no `external_mail` field, then whoami shall print no external line.
- Where `--qr` renders and outside mail is refused, the caption shall say so and shall not say
  "scan to mail X directly".

## WHY

On 2026-09-24 two teams read an agent's address, and its QR caption, as a promise that outside
mail would arrive, on a workspace that refuses it.

## CONTRACT (live, rodmena-test-02, 2026-09-30)

    {"accepts": "nobody", "refusal_reason": "encrypted_workspace",
     "refused_mail": "retained on the undeliverable surface, never bounced"}

accepts: anyone | contacts | nobody. refusal_reason: encrypted_workspace | ingress_closed |
not_in_contacts | null. null means unknown, not open.

## ALTERNATIVES

- CHOSEN: render the server's field. The server owns the policy.
- REJECTED: infer refusal from the workspace's encryption state client-side. Encryption is one of
  three reasons, and a client-side guess drifts from the policy the server enforces.

## ABSENT FIELD

Absent renders nothing, following this file's convention for persona. That asserts nothing, where
a guess would assert something false (memory `unserved_field_is_not_a_negative`). A null or
unrecognised value renders UNKNOWN.

## VERIFICATION

`tests/test_whoami_says_whether_outside_mail_arrives.py`, 12 tests, fed the live shape. Each
policy renders as itself; unknown is never open; an absent field prints no line; the QR caption
changes both ways. Mutations — never print the line, render unknown as open, the caption ignoring
a refusal — each fail it. Live on rodmena-test-02: `external:  REFUSED — this workspace is
encrypted, so mail from outside cannot be sealed`.
