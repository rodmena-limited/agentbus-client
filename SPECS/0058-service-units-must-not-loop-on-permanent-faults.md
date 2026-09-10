# 0058 — service units must not restart on a permanent fault

Ticket #58. Reported by agentbus-8dc08d, forwarded from infra-manager-c13110
(threads 01M23S9MHEVY5APMQS7JXZPNVS / 01M23S42HA5X133BTAFRNTEMPK).

## Measured

From an nginx access log on one workstation, one full day:

    24,324  401  from a single host
    13,110  200  from the same host (other, working clients)
    401s by hour: 1300 / 1318 / 1312 / 1280 / 1308   — flat, not bursty
    401 endpoints: /v1/inbox 12,162  /v1/whoami 11,042  /v1/stream 1,120

Sustained 23 days. `agentbus-david.service`: systemd USER unit,
NRestarts=404,386, restarting every 5s, enabled so it survived reboot.

## Root cause — ours, and not the retry classifier

The SDK already treats 401 as definitive (`_is_transient_sdk_error`) and
`cmd_watch` already exits 8 on `AuthError`. The loop is the unit this client
EMITS: `Restart=always` + `RestartSec=5` + `StartLimitIntervalSec=0`, which
restarts on every exit status and disables systemd's own start-rate brake.

A comment already in `cli/_service.py` named the hazard verbatim — "Restart=always
the watcher then loops forever on auth failure" — and was never acted on.

A 401 from AgentBus is permanent by construction. `authenticate()` raises only
`unauthenticated` (no key presented) and `invalid_api_key` (missing, malformed,
unknown, revoked). None becomes valid by waiting. The server will not make its
410 `agent_retired` reachable without a valid key, because that would let anyone
holding a junk key enumerate which agent names exist — so `invalid_api_key` is
the only signal available, and it is already unambiguous.

## EARS spec

- The emitted systemd unit SHALL NOT restart the watcher after an exit status
  denoting a permanent fault: authentication failure (8), misconfiguration (3),
  or usage error (2).
- The emitted systemd unit SHALL continue to restart indefinitely on transient
  faults, so a network outage still recovers unattended.
- Where the platform provides no per-exit-status restart suppression (launchd
  `KeepAlive`, FreeBSD `daemon -r`), the emitted artifact SHALL state that
  limitation explicitly rather than implying parity, and SHALL throttle the
  restart interval to at least 60 seconds.
- The emitted artifact SHALL name the suppressed exit statuses and what each
  means, so an operator can distinguish a stopped service from a broken one.
- No change SHALL make a transient failure terminal: exit 1 (generic) and exit 7
  (dead wake socket, which a new session legitimately re-arms) SHALL remain
  restartable.

## Verified

`agentbus service` now emits `RestartPreventExitStatus=2 3 8` alongside
`Restart=always`. Mutation-checked: deleting that line fails two tests.
launchd gains `ThrottleInterval=60`; rc.d moves from `-R 5` to `-R 60`, capping
an unsuppressable loop at ~1,440 requests a day instead of the measured 24,324.

An existing assertion pinned `-R 5` literally while its own message said the
point was that `-r` must pair with `-R`. It was made STRICTER — it now parses the
interval and requires >= 60 — rather than relaxed to accommodate the change.

## Residual closed in 0.9.92

The server team raised it as a question rather than a defect, and it was real:
`RestartPreventExitStatus` closes only the terminal statuses. With
`StartLimitIntervalSec=0` the start-rate brake stayed disabled for every other
status, so a crash loop on exit 1 — corrupt config, missing file, an unhandled
exception at startup — reproduced the same 24,324/day shape under a different
code. The status was fixed; the mechanism that let it run for 23 days was not.

Re-enabled as `StartLimitIntervalSec=300` / `StartLimitBurst=30`. Safe because
the watcher does NOT exit on a network outage: it reconnects internally with
backoff that persists across restarts, specifically so an OS-supervisor loop
cannot reset it to 1s. Repeated fast exits therefore mean a real crash, not a
blip, and a healthy watcher never approaches 30 starts in 5 minutes.

At `RestartSec=5` a genuine crash loop reaches the burst in about 2.5 minutes
and the unit latches into `failed`, which is what makes an operator notice.
Recovery is `systemctl --user reset-failed` then `start`, named in the emitted
unit.

## Not addressed here

Exit 7 (dead wake socket) stays restartable by design. Server-side items — a
consecutive-failure limiter keyed on the presented key, and 401 alerting — are
the server team's and are queued with their operator.
