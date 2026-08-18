# F7 — pre-upload attachment size check

Ticket: issuedb #4
Source: peer report (agentbus-ui-c760a1, batch #2, finding #7).

## EARS spec

- When `agentbus send` is invoked with attachments, the CLI shall check
  each attachment file size against the known 10 MiB per-attachment cap
  before opening any upload.
- If any attachment exceeds the cap, then the CLI shall fail fast with a
  clear error naming the offending file and its actual size, and shall
  not seal or upload anything.

## Rationale

Currently the client streams ~11 MiB through the sealing pipeline and the
network before the server returns 413 (~53.7 s wasted per rejected send).
The server-side cap is documented at 10,485,760 bytes; the client knows
it and can fail immediately.

## ENCRYPTED-WORKSPACE BOUNDARY (added 0.9.38/0.9.39, R4 re-test round)

On an ENCRYPTED workspace the server sees the SEALED base64
(`base64(age_armor(raw))`), which inflates at a DETERMINISTIC ~1.806x
(age-armor base64, then JSON-transport base64 again — double base64,
1.333^2). Measured byte-exact on two seats (macbook, ui; thread
01M0BGFKX4EE8WV6T68BTARGTE): 7,340,032 raw -> 13,256,496 wire, and
9,961,472 -> 17,990,808, both 1.806x. Deterministic, not content-
dependent.

THEREFORE the effective per-attachment limit on an encrypted workspace
is ~10 MiB / 1.806 = ~5.5 MiB raw — NOT the documented 10 MiB, which is
only accurate on non-encrypted workspaces.

Client behaviour (0.9.39):
  - FAST pre-seal reject: if raw * 1.806 > server cap, reject instantly
    without running the (CPU-heavy) seal. Proved by a test whose seal
    raises if called.
  - POST-seal exact check: the authoritative backstop for the borderline
    band (seal runs, wire length compared to cap). Reachable and verified
    (a 5.51 MiB file passes the estimate, seals, and sends OK).
  - The reject error names the file, raw size, estimated wire size, and
    the ~5.5 MiB effective limit.

DOCUMENTED CAP IS A LIE ON ENCRYPTED WORKSPACES. The skill, llms.txt,
and the MCP bus_send tool description (all backend-served) claim
"10 MiB per attachment" — true only on non-encrypted. Parity request:
backend updates all three to note the ~5.5 MiB encrypted effective
limit; ui updates the website if it documents the cap. Client error text
already carries the truth to the operator who hits it.
