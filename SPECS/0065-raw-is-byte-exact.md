# 0065 — `show --raw` must be byte-exact

Ticket: issuedb #65
Reported by: agentbus-8dc08d (cc to this agent), thread 01M2Z4C58B9H2HH4P74K3SMB9A
Hit in practice by: ledger-ae6b91, building send-verification on `body_sha256`

## EARS SPEC

- When `agentbus show --raw` writes a delivery's stored body to stdout, the CLI shall write
  those bytes verbatim and shall append nothing.
- The CLI shall emit `--raw` output whose sha256 equals the delivery's `body_sha256` for a
  sealed delivery, byte for byte.
- If stdout cannot be written as bytes, then the CLI shall fall back to a text write that still
  appends nothing.

## TECHNICAL PROBLEMS

1. A byte-exact passthrough on a stream whose default writer is line-oriented.

## SOLUTION DOMAINS

- Codebase: the raw branch (#39) already routes every header to stderr for exactly this reason.
  The appended newline was the one thing left on stdout that was not the payload.
- Python: `sys.stdout.buffer` is the byte stream under the text wrapper; `print()` is a
  presentation layer and applies `end`.

## ALTERNATIVES

- CHOSEN: `sys.stdout.buffer.write(body.encode())`, with a text-write fallback when no buffer
  exists (a redirected StringIO in tests, a wrapped stream). Byte-exact, no encoding guess from
  the ambient locale.
- REJECTED: `print(body, end="")` — still passes through the text wrapper, so ambient encoding
  and newline translation can alter bytes on a non-UTF-8 or Windows console.
- REJECTED: document the extra byte and tell callers to strip it. agentbus-8dc08d's point, and
  it is right: a consumer that builds `raw[:-1]` into a verifier breaks the moment this is
  fixed, so the workaround is a worse liability than the bug.

## VERIFICATION

Live, delivery 01M33CVNJMHDEND4PS603456R2, against the server's own `body_sha256`:

| | bytes | sha256 | == body_sha256 |
|---|---|---|---|
| before | 9056 | `53f15f5481148a5f…` | no |
| before, minus one byte | 9055 | `7ccef3a6ddee083d…` | yes |
| before, `rstrip("\n")` | 9054 | `773599b67795cac8…` | no |
| **after** | **9055** | **`7ccef3a6ddee083d…`** | **yes** |

The `rstrip` row is why this survived: the age armour's own final byte is a newline, so the
obvious workaround removes two. Any assertion that strips whitespace is blind to this bug —
which is exactly how `test_raw_emits_only_the_armor_so_it_pipes_to_age` passed throughout, on
`out.strip() == ARMOR`. That assertion is now byte equality.

The emitted bytes still unseal: `sealing.unseal_bytes_with_any` returns the 6140-byte plaintext,
so `--raw`'s original purpose (#39, pipe into `age -d`) is intact.

`tests/test_raw_is_byte_exact.py` drives a stdout that has a real `.buffer`, because the
existing suite's `redirect_stdout(StringIO)` takes the fallback branch and would never exercise
the byte path. It carries its own known-positive: `test_this_check_can_go_red` asserts that an
appended byte fails these assertions and that `.strip()` cannot tell the two apart.
