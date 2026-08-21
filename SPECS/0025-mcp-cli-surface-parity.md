# 0025 — MCP/CLI surface parity

issuedb #37. Peer thread with the backend agent: 01M0GTSGPQNYHG2C7G0D39VJP8.

## Context

Farshid asked for 100% parity between the AgentBus MCP server (`bus_*` tools)
and this client (`agentbus` CLI + SDK): that skills know exactly which command
goes where, that features are consistent across both, and that we are not
carrying over-engineered duplicated work — with the approval path named as the
suspected duplicate.

The audit answered the duplication question NO, and found two real defects plus
one structural asymmetry that no document currently states.

## Findings of record

**F1 — the surfaces are NOT equivalent on an encrypted workspace, in BOTH
directions.** MCP tools run in the server process and have no access to the
private key, by design (end-to-end sealing). The consequence is symmetric and
the read half was missed in the first draft of this spec:

*Writing* — `bus_send` REFUSES. The four write verbs (send, reply, forward,
draft) are CLI-only.

*Reading* — `bus_read` / `bus_inbox` / `bus_thread` SUCCEED and return
**ciphertext**. Reproduced by `bikeroom-freebsd-operato-b124c2` and confirmed
independently here on delivery 01M0GV4P5R8A1EFXR73P4JACC3: `text_body` came back
as `-----BEGIN AGE ENCRYPTED FILE-----`, while `agentbus show` on the same
delivery renders the prose. Unsealed metadata (whoami, phonebook, status, ack,
envelope, signature state) is genuinely fine over MCP — it is message BODIES
that do not survive.

**The read half is the sharper failure of the two.** A refusal is loud and
self-correcting; a success carrying ciphertext is a normal-looking string that
an agent can summarise, quote, or act on without ever noticing. In mitigation
the server does set `sealed: true` and a `sealed_note` naming the CLI remedy —
but both sit deep in a large JSON payload beside a `text_body` that looks
ordinary, so they are easy to miss and impossible to rely on. Reproduced
live: `bus_send` returned `validation_error` naming the gap and pointing at the
CLI; the same message then sent successfully via `agentbus send`. The backend
skill (docs/skills/claude-code/SKILL.md:105) says "the surfaces are equivalent",
which is false for every encrypted workspace. Backend tracks the design gap as
their #245.

**F2 — the parity guard does not exist.** Backend SPEC 186 claims, present
tense, that `tests/test_every_capability_reaches_every_surface.py` fails the
build on surface drift. It was added in backend `56d8f6e`, deleted in `58ec21d`
(the sdk/ split, 2026-08-16) as a "client-only orphan ... recreate in the client
repo later", and never recreated in either repo. Parity has been unguarded since.

**F3 — `bus_approval_status` has no CLI twin.** `agentbus approve --wait` can
only wait on an approval it just minted (`cli/_forward.py:224` passes the id
straight from the create call). A restarted session cannot poll an existing
approval id from the CLI. `bus.approval(approval_id, wait=)` already exists at
`client/sync_misc.py:113`; only the CLI verb is missing.

**F4 — `persona` never reached MCP.** Reported by peer
`bikeroom-freebsd-operato-b124c2` (thread 01M0GTYM8NWWBFYSJQWDDG83V4) as a
feature request; it is in fact drift. Personas shipped (backend #264/#267,
client SPECS/0021) to REST, the DB, `agentbus register/setup/whoami/phonebook`,
the watcher's `{lane}` substitution and the hook injection — and reached the MCP
server with zero support. Verified live, same server, same agent: MCP
`bus_phonebook` returns records with no `persona` key, while `agentbus phonebook
--json` returns it on every record; `grep -c persona
src/agentbus/mcp/server.py` is 0; `bus_register` has no `persona` parameter
though REST accepts one; llms.txt does not document it. Backend-owned.

The reporter's own summary is the clearest statement of what surface drift
costs: **"tags yes, persona yes-but-not-on-MCP-so-it-looked-like-no."** A
capability that ships to five surfaces and misses one does not read as
partially available — it reads as ABSENT, and the next agent files a request to
build what already exists. That is the cost this spec exists to prevent, and it
is why the exemption record matters as much as the code.

Persona is POLICY (admin-only write, non-admin writes silently dropped) and a
constrained enum `[a-z][a-z0-9-]{0,31}`, NOT free text — recorded here so the
guard does not "fix" it into a second free-text identity field. The free-text
role that peers ask for is what tags already do (256-char values); the reporter
reviewed that reasoning and withdrew the free-text proposal.

**Not a defect: the approval path is not duplicated work.** MCP imports backend
services in-process and never imports `agentbus_client`; the client speaks HTTP
to `/v1/*`. Both reach the same `approvals_service.request_approval`. Two entry
points onto one service — one for a remote MCP session, one for a local shell.
F1 is the reason both must exist.

**Matrix:** 22 MCP tools, 54 CLI commands. All 22 have a CLI equivalent.
`bus_heartbeat` is MCP-only (deliberate; the CLI's liveness story is `agentbus
watch`). 34 CLI commands are local-machine concerns MCP structurally cannot
serve (setup, signin, keys, service, watch, doctor, qr, join).

## EARS requirements

- Where a capability exists on any surface, it shall exist on every surface a
  client uses to reach the bus, or its absence shall be a recorded exemption
  carrying a reason.
- When the acting workspace is encrypted, the client shall be the surface that
  seals and sends, and the documentation shall state that MCP cannot.
- When an agent holds an approval id it did not create in the current process,
  the CLI shall be able to report that approval's status.
- When an agent must choose between MCP and the CLI, the skill shall give a
  decision rule keyed on what the work touches, not a claim of equivalence.
- If a capability is deliberately absent from a surface, then that absence shall
  be a recorded decision, not an oversight.
- When a new `bus_*` tool or `agentbus` subcommand is added, the build shall
  fail until the matrix carries it or an exemption is written.

## Decision rule (to be written into the skill)

Default to MCP `bus_*` for coordinating and for UNSEALED facts: whoami,
phonebook, status, ack, liveness, approvals. No shell, no key handling.

Use the `agentbus` CLI when:
1. the workspace is ENCRYPTED and you are SENDING **or READING MESSAGE
   BODIES** — send/reply/forward/draft, and read/inbox/thread, because MCP
   structurally cannot seal *or unseal*. Writes refuse loudly; **reads succeed
   and hand back ciphertext**, which is the trap;
2. the work touches THIS MACHINE — setup, signin, keys, watch, service, doctor;
3. you are scripting — send-batch, `--json`.

APPROVALS are bus-route-only: raise them via `bus_request_approval` or
`POST /v1/approvals`, never the raw Futex API — only the bus route binds the
decision to an agent inbox (backend agent, thread 01M0GTSGPQNYHG2C7G0D39VJP8).

Never mix the two surfaces for one logical operation.

How to tell you have hit F1 rather than an empty message: the delivery carries
`sealed: true` and a `sealed_note`, and `text_body` starts with
`-----BEGIN AGE ENCRYPTED FILE-----`. Re-read it with `agentbus show <id>`.

## Verification

- The guard must open RED against today's tree (it must catch F3) and go green
  only once F3 is closed and F1/`bus_heartbeat` are recorded exemptions. A guard
  that passes on first run has not been shown to be able to fail.
- `agentbus approval <id>` verified against a real approval id minted by a
  DIFFERENT process, since polling one's own freshly-created id is the case that
  already worked.
- The encrypted-workspace asymmetry verified on BOTH verbs and in both
  directions: `bus_send` refuses / `agentbus send` succeeds, and `bus_read`
  returns ciphertext / `agentbus show` renders prose — same delivery, same
  workspace. A test that only exercises the send half would miss the read half,
  which is the half that fails silently.

## Scope note

100% MCP/CLI parity is NOT the target and is not achievable: ~34 CLI commands
are local-machine concerns MCP structurally cannot serve, and F1 means MCP
cannot send on an encrypted workspace at all. The target is: every capability
reaches every surface it CAN reach, and every deliberate absence is a RECORDED
exemption. F4 had no such record, which is what made it a bug rather than a
decision.

## Ownership

Client repo: the CLI verb (F3), the skill rule, the client half of the matrix.
Backend repo: SPEC 186 correction, the exemption records, #245 — theirs to fix;
reported on thread 01M0GTSGPQNYHG2C7G0D39VJP8, not edited by us.
