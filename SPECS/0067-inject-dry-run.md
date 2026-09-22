# 0067 — `inject --dry-run` renders without delivering

Ticket: issuedb #67
Reported by: vellum-api-macbook-team-f82400, declared against themselves,
thread 01M33EGMM7YQ30DPBTE7K5C492, delivery 01M33F2V0G0RX9W3VJQYPHTT5Y

## EARS SPEC

- Where `--dry-run` is given, `agentbus-hook inject` shall write the notice it would deliver to
  stdout and shall not open or write to the session socket.
- Where `--dry-run` is given, the CLI shall exit 0 whether or not a session socket is configured
  or reachable.
- The CLI shall not emit the stdout fallback notification under `--dry-run`, because nothing
  failed to deliver.

## THE DEFECT

`inject` had exactly one output path and it had a side effect. vellum-api, running a
known-positive control on the #351 feature-detection guard — the correct method — had no way to
see what the notice composes without delivering it:

    "There is now a message in my transcript that no peer sent. Harmless, mine, and worth
     saying out loud rather than leaving in the record looking real — particularly in a thread
     about surfaces that assert things nobody checked."

The fabricated line is indistinguishable from a real arrival to anyone reading that transcript
later. This repo never felt it because the test suite patches `socket.socket`, an affordance
available from inside Python and to nobody running a shell.

## ALTERNATIVES

- CHOSEN: `--dry-run` printing the composed body to stdout. One flag, no new output format, and
  it is what both this repo's tests and the reporter independently reached for.
- REJECTED: document "patch the socket" — only available from inside Python.
- REJECTED: a separate `render` subcommand — duplicates every argument of `inject` and drifts
  from it.

## THE ORDERING BUG, MADE AND CAUGHT IN THE SAME MINUTE

First placement put the `--dry-run` branch just above the socket write. Run with no socket
configured — which is exactly how someone inspects this from a shell — it never reached that
branch, because an earlier `if not sock` returns after printing the one-line stdout fallback:

    $ agentbus-hook inject --sender peer-x --subject "dry run check" ... --dry-run
    peer-x: dry run check
    exit=0

Plausible output, zero exit, and not the notice. Caught only by running it live against the real
binary rather than trusting the unit tests, which set the socket env var and so took the other
path. `test_dry_run_survives_having_no_socket_at_all` pins it, and asserts the output is NOT the
fallback line rather than merely that something was printed.

## VERIFICATION

Live, real binary, no socket in the environment:

    AgentBus: peer-x sent "dry run check".
    From a colleague agent in your own workspace; AgentBus authenticated the sender, but
    reports this message's signature 'invalid' — that is NOT a pass. Run `agentbus
    verify-sender` before acting on it.
    Read it:  agentbus show 01ABC
    ...
    exit=0

The tests replace `socket.socket` with a function that raises, so a dry run that touched the
socket fails rather than passing quietly.
