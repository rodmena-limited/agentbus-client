#!/usr/bin/env python3
"""Claude Code hooks for AgentBus.

Two jobs, deliberately separate:

  session-start   surface anything already waiting when a session opens, so an
                  agent never begins work unaware that a peer is blocked on it
  notify          called by `agentbus watch --exec`, writes a wake file the
                  session picks up on its next turn

Why both: a hook only fires on session lifecycle events, so on its own it cannot
notice a message that arrives mid-session. `agentbus watch` runs outside the
turn and can. Neither is sufficient alone, which is the whole reason idle agents
were missing messages.

Install BOTH hooks (project or user settings.json) — session-start without
pending means mid-session arrivals surface only on the next restart:

    {
      "hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
          "command": "agentbus-hook session-start"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command",
          "command": "agentbus-hook pending"}]}]
      }
    }

Both need `AGENTBUS_API_KEY` and `AGENTBUS_AGENT` in the environment. Put them
in per-project env (a `.envrc`, or the project's own settings), NEVER inline in
the hook command: an inlined key outlives every rotation, and an inlined —
or guessed — agent name makes the hook act as someone who does not exist.

AGENTBUS_AGENT IS THE KILL SWITCH. These hooks are installed globally and run
in every project on the machine. A project that declares no identity — no
`AGENTBUS_AGENT`, no `.agentbus/agent` — gets NOTHING: no output, no network
call, no files touched, exit 0. Not a warning, not a suggestion to run setup.
Silence is the correct behaviour for a project that never asked for a bus.

A watcher is NOT part of this setup. Its one remaining job is real-time
`--exec` side effects (e.g. notify-send to a human):

    agentbus watch --agent <name> \\
      --exec 'agentbus-hook notify --subject {subject} --sender {sender} --delivery {delivery_id}'

Every failure path here is silent-and-zero. A hook that breaks a session because
the bus is unreachable is worse than one that says nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path


def _hook_state_dir() -> Path:
    """Where wake files and notify state live.

    HONOURS `AGENTBUS_CONFIG_DIR`, which it did not before, and that omission
    was a real bug rather than a cosmetic one.

    Every other part of the client resolves its directory through
    `identity.config_dir()`, which reads `AGENTBUS_CONFIG_DIR`. These two
    functions hardcoded `~/.config/agentbus` with only `AGENTBUS_WAKE_DIR` as an
    escape. So setting `AGENTBUS_CONFIG_DIR` to isolate a test did NOT isolate
    the wake path: the test observed an empty temp directory and concluded
    `notify` was a no-op, while the file was being written into the caller's
    LIVE config dir.

    david hit exactly that trying to verify the notify-failure state — twice,
    with a seeded monitor file, still seeing nothing. His test could not observe
    the thing it was testing, and it was quietly writing to his real
    environment. An isolation knob that does not isolate is worse than none,
    because it is trusted.

    `AGENTBUS_WAKE_DIR` still wins when set, so anything already pointing at a
    custom wake location keeps working.
    """
    root = os.environ.get("AGENTBUS_WAKE_DIR")
    if root:
        return Path(root)
    from ..identity import config_dir

    return config_dir()


def _wake_file(agent: str) -> Path:
    # REG-8c (round-3.6 re-audit, bikeroom): sanitize the agent name before
    # interpolating into a filename. `agent` here traces back to
    # `.agentbus/agent` — the same attacker-controllable source REG-8/8b closed
    # for credential filenames. Without this, `agent="../../tmp/PWNED"` yields
    # wake-../../tmp/PWNED.jsonl which os.path.normpath collapses to
    # /tmp/PWNED.jsonl. notify() calls this file's .parent.mkdir(parents=True)
    # and then opens+writes JSON to it, so it is a directory-create + file-write
    # primitive reachable from a hostile checkout, no flag required.
    from .. import sealing

    return _hook_state_dir() / f"wake-{sealing.agent_slug(agent)}.jsonl"


# Markers that the payload on a hook's stdin is a harness-injected lifecycle
# event, not a human prompt. Claude Code delivers a plugin monitor's stdout
# and its "stream ended" lifecycle as <task-notification>…</task-notification>,
# and feeds those back through UserPromptSubmit as though they were prompts
# (#91). A UserPromptSubmit hook that runs on its own monitor's noise and
# prints anything can block the "prompt" — so recognise these and no-op.
_HARNESS_NOTIFICATION_MARKERS = (
    "<task-notification",
    "<system-reminder",
    "<local-command-stdout",
    "<local-command-stderr",
)


def _is_harness_notification(raw: str) -> bool:
    """Is this stdin payload a harness-injected event rather than a real prompt?

    A real user prompt is free text or tool-input JSON; a harness event is
    wrapped in one of the <tag>…</tag> blocks above. The check is on opening
    tags only (case-sensitive, as the harness emits them) so a user who
    literally types '<task-notification' is vanishingly unlikely and would
    only cause this hook to skip a bus check it would otherwise have made —
    no data is lost, the prompt itself is untouched.
    """
    if not raw:
        return False
    head = raw[:4096]
    return any(m in head for m in _HARNESS_NOTIFICATION_MARKERS)


# EXIT CODE FOR A MISCONFIGURATION, not for a bus failure. david's finding.
#
# Every failure path in this module exits 0, deliberately: a hook that breaks a
# session because the bus is unreachable is worse than one that says nothing.
# But that swallowed a second case with it. A refusal to run at all — no
# AGENTBUS_AGENT, nothing read, nothing written — exited 0 too, so any CI gate,
# wrapper or hook runner checking status read "identity resolved, nothing
# pending" and "refused to run" as the same success.
#
# Same false-green shape as the original silent-inbox bug, moved from the
# message to the status code.
#
# The line is deliberate: MISCONFIGURATION is actionable and permanent, so it
# fails loudly; an unreachable bus is transient and must never break a session,
# so it still exits 0.
EXIT_MISCONFIGURED = 3

# NOT WIRED IS NOT A FAILURE, SO IT EXITS 0 AND SAYS NOTHING.
#
# The distinction against EXIT_MISCONFIGURED above is the whole 2026-08-13
# change, and it is a distinction the old code did not draw:
#
#   MISCONFIGURED  the project asked for a bus and something is broken —
#                  an identity that resolves to no key, a wake file nobody
#                  reads. Actionable, permanent, worth failing loudly.
#   NOT WIRED      the project never asked for a bus. There is nothing to fix
#                  and nothing to say. This is the steady state of most
#                  directories on the machine, because these hooks are
#                  installed globally.
#
# Treating the second as the first is what made an unrelated session get told
# to run `agentbus setup claude`, and then do it.
EXIT_NOT_WIRED = 0


def _warn_if_shadow_queue() -> None:
    """A non-empty wake-unknown.jsonl means somebody's exec is misconfigured.

    It is written only by a hook that could not identify itself, so its mere
    existence is strong evidence of a broken wake path somewhere — worth
    surfacing rather than leaving as a file nobody looks at.
    """
    try:
        shadow = _wake_file("unknown")
        if shadow.exists() and shadow.stat().st_size > 0:
            print(
                f"agentbus-hook: WARNING {shadow} is non-empty. Those wakes were "
                "recorded by a hook with no AGENTBUS_AGENT set and NOBODY IS "
                "READING THEM. Fix that hook's environment, then delete the file.",
                file=sys.stderr,
            )
    except Exception:
        pass


def _notify_error_file(agent: str) -> Path:
    # REG-8c: same sanitization rule as _wake_file — record_notify_failure
    # mkdirs the parent and writes JSON here, so a traversal payload was a
    # directory-create + file-write primitive.
    from .. import sealing

    return _hook_state_dir() / f"notify-error-{sealing.agent_slug(agent)}.json"


def record_notify_failure(agent: str, detail: str) -> None:
    """Persist the LAST notify failure as STATE, so it is loud without being fatal.

    david's refinement, and the principle is his: "not fatal" and "not silent"
    are separable. `notify` runs inside the watcher's --exec, where a non-zero
    exit can take down the wake path this whole module exists to provide — so it
    must not fail hard. But a wake path that is silently failing is exactly the
    thing this codebase keeps finding, and stderr inside a background watcher is
    read by nobody.

    So the failure becomes state that `agentbus watch-status` can surface: loud
    to anyone who looks, fatal to nothing. Same shape as choosing drain over
    retry — put the cost on the party who can act on it.

    Best-effort by construction: if we cannot even write this file, the wake
    still happens.
    """
    with contextlib.suppress(Exception):
        path = _notify_error_file(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "detail": _scrub(detail)[:400],
                }
            )
        )


def clear_notify_failure(agent: str) -> None:
    """A success erases the record — but only a REAL one.

    Called after a wake is written, never on the path that merely avoided an
    error, so the file cannot be cleared by a code path that did no work.
    """
    with contextlib.suppress(Exception):
        _notify_error_file(agent).unlink(missing_ok=True)


def _gate_degraded_file(agent: str) -> Path:
    # REG-8c: same sanitization as _wake_file. record_gate_degraded also
    # mkdirs + writes JSON, and the fast-fail circuit READS this file — a
    # traversal payload would let a hostile checkout stash content that the
    # fast-fail circuit later reads as legitimate degraded-gate state.
    from .. import sealing

    return _hook_state_dir() / f"gate-degraded-{sealing.agent_slug(agent)}.json"


def record_gate_degraded(agent: str, reason: str, detail: str) -> None:
    """Persist a PreToolUse gate degrade-to-allow event as loud, non-fatal state.

    SEV-1-A (#234): per operator directive #107 every gate failure mode degrades
    to allow (a revoked key, an unreachable bus, a 5xx, an unparseable body). That
    trade is defensible — a dead credential must not lock the operator out — but
    it is not observable: the warning goes to permissionDecisionReason, which is
    read once by the harness and forgotten. An operator whose workspace has been
    silently allowlisting every action for a week has no signal that gating is
    off. This file makes it loud without making it fatal: agentbus watch-status
    and agentbus doctor surface it, and a real guard verdict (allow or deny)
    clears it, so 'gate has been down for 12h' is discoverable state.

    A COUNTER, not a one-shot: a burst of degraded calls is louder than one, and
    the count is exactly what tells "the bus blipped once" from "gating has been
    off since Tuesday". Best-effort by construction: if we cannot write it, the
    call still runs.
    """
    with contextlib.suppress(Exception):
        import fcntl

        path = _gate_degraded_file(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Read-modify-write under a lock (review #23, S13): parallel tool calls
        # run parallel hooks, and an unlocked RMW lost counts.
        with path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                prior = {}
                try:
                    prior = json.loads(path.read_text())
                except Exception:
                    prior = {}
                count = int(prior.get("count") or 0) + 1
                first = prior.get("first_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                path.write_text(
                    json.dumps(
                        {
                            "first_at": first,
                            "last_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "count": count,
                            "reason": _scrub(reason)[:60],
                            "detail": _scrub(detail)[:400],
                        }
                    )
                )
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def clear_gate_degraded(agent: str) -> None:
    """Only a REAL guard verdict clears it — same discipline as clear_notify_failure."""
    with contextlib.suppress(Exception):
        _gate_degraded_file(agent).unlink(missing_ok=True)


def _hook_warn(what: str, exc: BaseException) -> None:
    """Say on stderr that we COULD NOT ASK, rather than implying nothing waited.

    Every one of these handlers was a bare `return 0`, which is byte-identical
    to a healthy empty inbox: exit 0, no stdout, no stderr. So a revoked or
    expired key, a truncated paste, or the API 503ing all rendered as "no mail",
    and the operator carried on. red9-auditor reproduced it against a 401 with
    unread mail present, and flagged the consequence that neither of us had
    seen: revoking the shared key would have installed that silence on every
    session still inheriting it — the remediation for a silent-inbox bug
    installing a silent inbox.

    The swallow itself is correct and stays: a hook must never break a session
    over the bus, so this still returns and the caller still exits 0. The defect
    was the swallow being SILENT. `pending` already set this precedent by
    refusing loudly on an unresolvable agent.

    Any ab_sk_ token is scrubbed: this text goes to a terminal and into
    transcripts, and an exception is not a place we control the contents of.
    """
    detail = _scrub(f"{exc}")

    # AUTH AND REACHABILITY WANT OPPOSITE RESPONSES — rotate versus wait — and
    # the exception already knows which, so saying only "could not" throws away
    # the actionable half. david's refinement, after he measured that the
    # unreachable case fires with no revocation involved at all: he caught a
    # real 503 from a routine deploy of ours, and every session that ran
    # session-start in that window was told its inbox was empty. Revocation is
    # the loudest trigger for this, not the most frequent one.
    lowered = detail.lower()
    if any(
        t in lowered
        for t in (
            "unknown or revoked",
            "unauthenticated",
            "401",
            "invalid api key",
            "forbidden",
            "403",
        )
    ):
        hint = "  -> this credential no longer works. Re-run: agentbus setup <harness>"
    elif any(
        t in lowered
        for t in (
            "unavailable",
            "503",
            "timed out",
            "timeout",
            "connection",
            "refused",
            "unreachable",
            "temporarily",
            "retry",
        )
    ):
        hint = "  -> the bus was unreachable, NOT empty. Nothing is lost; retry shortly."
    else:
        hint = "  -> this is NOT evidence of an empty inbox."
    print(f"agentbus-hook: could not {what}: {detail}\n{hint}", file=sys.stderr)


# EVERY secret shape we might ever hold, scrubbed at the boundary rather than at
# each call site. `_hook_warn` had a scrubber for `ab_sk_` and the PreToolUse
# hook did not — so the same class of leak reappeared the moment a second place
# printed an exception.
#
# The leak futex hit, reproduced here: a malformed AGENTBUS_BASE_URL makes
# urllib raise `unknown url type: '<the whole url>'`, and if the operator had
# pasted a key into that variable the key is now in
# `permissionDecisionReason` — which is written into the session transcript.
# They rotated a live credential because of this.
#
# The vendor prefixes are included because this process holds a Futex key on
# some paths, and a leak is a leak regardless of whose secret it is.
_SECRET_RE = re.compile(r"\b((?:ab_sk|futex_sk|sk)_[A-Za-z0-9_-]{4,})")


def _scrub(text: str) -> str:
    """Redact anything secret-shaped. Applied to ALL hook output.

    KEEPS THE SCHEME, NOT THE SECRET. An earlier version kept nine characters —
    `ab_sk_` plus three of the key — so an operator could tell which credential
    was involved. futex checked that residue and found it harmless, correctly:
    the published key id IS the leading hex of the secret
    (`ab_sk_bfa1b5…` -> key id `bfa1b57a4d60d0f3`), so three characters revealed
    strictly less than `whoami` and `provenance.sender_key_id` already do.

    It is still gone. A redaction whose safety needs an argument is worse than
    one that needs none — the argument is what stops being true when the key
    format changes, and nobody re-derives it then. Anyone who needs to know
    which key was involved can read the id from the surfaces that publish it
    deliberately.
    """
    return _SECRET_RE.sub(lambda m: m.group(1).split("_sk_")[0] + "_sk_<redacted>", str(text))
