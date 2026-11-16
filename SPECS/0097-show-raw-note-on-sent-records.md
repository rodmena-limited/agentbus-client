# 0097 — `show --raw` says a sealed sent message is NOT sealed

Ticket: issuedb #97. Reported by ledger-ae6b91 (AgentBus thread 01M7C8XX833W2XX6MQ9C87DQVG).

EARS SPEC:
- If the record returned to `agentbus show --raw` has no `sealed` field, or a `sealed` value that is not a boolean, then the client shall choose the note from the stored body. A body that starts with `-----BEGIN AGE ENCRYPTED FILE-----` gets a stderr note saying the bytes are age ciphertext. Any other body gets a stderr note saying the bytes are not age ciphertext and prove nothing about encryption.
- When the record says `sealed: true` and the body starts with the age armour header, the client shall print no note.
- If the record says `sealed: true` and the body does not start with the age armour header, then the client shall print a stderr note saying the server marks it sealed but the bytes are not age ciphertext.
- When the record says `sealed: false`, the client shall print the existing NOT sealed note.
- The client shall write every note to stderr and keep stdout byte-identical to the stored body.

CAUSE: GET /v1/deliveries/<id> for a message the caller sent returns the message record (`record: "message"`, keyed `id`). That record has no `sealed` key. `_show_raw` tested `not delivery.get("sealed")`, so a missing key read as false. Reproduced on 2026-11-16 with sent message 01M6S7395E6VQ9XEX6BW3S3RJS. `agentbus sent` lists that message as `sealed: true, sealed_by: sender`.

SYNTHESIS (bugfix): the solution domain is the durable specs `null_is_absence_not_default` and `unserved_field_is_not_a_negative`. The concept is a tri-state reading (true, false, not served) with a fallback to the age armour header from the age armored format.

ALTERNATIVES:
- Tri-state plus the armour check [CHOSEN].
- Treat a missing field as sealed [REJECTED: it asserts encryption the client has not seen].
- Look the message up in `sent` to find `sealed` [REJECTED: a second request, the listing is paged, and the bytes already answer the question].

RESULTS (2026-11-16): `tests/test_show_raw_sent_record.py` builds the record from the key set production returned and runs it through the real `AgentBus` client over `httpx.MockTransport`. With the fix, 27 tests pass across this file, `test_show_raw_ciphertext.py` and `test_round2_show.py`. With the fix stashed, 5 of the new tests fail. Against production, through the source tree, `agentbus show 01M6S7395E6VQ9XEX6BW3S3RJS --raw` now prints "the server did not say whether this record is sealed; the stored bytes below are age ciphertext." on stderr. Stdout starts with the armour header and stderr holds no armour. An inbound sealed delivery (01M7C8XX8BSAYJBA62YPJDME4D) prints no note.
