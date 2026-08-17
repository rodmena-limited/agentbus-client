# Setup sealing-key publish: retries + loud failure

Ticket: issuedb #15
Source: backend report from `agentbus-8dc08d` (thread
`01M08QS3M10M49WKT8WVX3P2P7`), triage of #243 probe failures.

## EARS spec

- When `agentbus setup` provisions an ephemeral or named agent on an
  encrypted workspace, the CLI shall retry the
  `POST /v1/agents/{name}/pubkey` publish across transient failures
  (documented server race between the newly-minted bound key becoming
  usable and the pubkey endpoint seeing it).
- If the publish ultimately fails after retries, the setup report shall
  carry a visibly loud marker (containing `!!!` and the phrase
  `PUBLISH FAILED`) and name an actionable recovery command
  (`agentbus keys rotate`).
- The retry helper shall be extractable and unit-testable, and its
  backoff shall grow across attempts so a refactor cannot flatten the
  wait to zero and turn the retry into a tight loop.
- Setup shall NOT fail on this — a half-wired project remains worse
  than one that had a rough publish — but the operator MUST see the
  problem in the setup output.

## Rationale

Backend's `probe_onboarding.py` was failing 3 of 9 remaining assertions
with cascades of "cannot seal: no public key" against ephemeral
`onboard-probe-*` agents. Root cause: setup silently swallowed the
sealing pubkey publish failure and added a soft `NOT REGISTERED`
report line that read as routine on a scanning eye.
