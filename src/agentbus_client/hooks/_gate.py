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

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from . import _identity
from ._identity import _resolve_agent
from ._state import _gate_degraded_file, _scrub, clear_gate_degraded, record_gate_degraded


def _bus_reachable(base: str, timeout: float) -> tuple[bool, str]:
    """A bounded TCP connect to the bus — the cheapest possible "is the network there".

    urllib's single `timeout` covers connect AND read, so a dead network used to
    cost the full read budget per attempt, twice (peer review C5: 24s per tool
    call). A connect that fails in <= `timeout` answers the question the guard
    actually has — can we reach the bus at all — for a fraction of the cost.
    """
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        return False, f"no host in AGENTBUS_BASE_URL {base!r}"
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def pre_tool_use(_args: argparse.Namespace) -> int:
    """PreToolUse: ask AgentBus whether this tool call may run, BEFORE it does.

    THE ONLY ENFORCEMENT POINT THERE IS for an agent's own tool calls. AgentBus
    sits in no other part of that path — nothing server-side can stop a `Bash`
    call — so if this does not gate it, nothing does.

    NOT `permissionDecision: "ask"`. That escalates to an in-session permission
    prompt, and AgentBus exists for agents whose human is somewhere else
    entirely: on an unattended session nobody is at that prompt, so it stalls
    for the hook timeout and then fails. The same mistake as putting a modal in
    front of a socket injection — a human gate placed where no human is. The
    approval goes to Futex instead, by email, policy-routed, and survives the
    session dying.

    THE ONLY THING THAT BLOCKS IS A VERIFIED DENY. Operator directive
    2026-08-13 (#107): a dead credential must never hold a session hostage.

    The original design failed CLOSED on every branch — no credential,
    unreachable bus, non-200, unparseable, unforeseen all denied. That produced
    the worst failure in the product's life: a workspace reset revoked an
    agent's key, the hooks still resolved the identity from the project config,
    the guard could not reach its rules, and it denied EVERY tool call —
    including `echo` — across every session on the host. The operator had to
    physically delete identity files to recover their own machine. Revoking a
    key turned into taking the session hostage, and "recover the key" as the
    remedy asked the victim to pay for the platform's failure.

    So the trade is inverted, deliberately and with the operator's explicit
    instruction:

      * verified `deny` from the guard   -> BLOCK (a real policy decision)
      * verified `allow`                 -> allow
      * credential revoked / rejected    -> ALLOW, with a warning that this
                                             session is running UNVETTED
      * bus unreachable / non-200        -> ALLOW, with a warning
      * unparseable / unforeseen         -> ALLOW, with a warning

    The safety net DEGRADES; it never imprisons. An unvetted tool call is a
    real risk, but it is bounded — one call, once — while a session locked out
    of everything is unbounded. The warning is the load-bearing half: it must
    say the session is running WITHOUT approval checking, so nobody mistakes a
    degraded session for a protected one.

    Exit 0 always. A hook that crashes is a hook the harness may ignore, and
    the decision is carried in the JSON on stdout, not in the exit code.
    """
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")

    def decide(decision: str, reason: str) -> int:
        # `permissionDecision` is the field Claude Code acts on; the reason is
        # shown to the agent, so it has to say what to DO, not just "no".
        #
        # SCRUBBED HERE, at the single point every branch passes through, so a
        # future message that interpolates an exception cannot reintroduce the
        # leak. Scrubbing at each call site is how the first one survived.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision,
                        "permissionDecisionReason": _scrub(reason),
                    }
                }
            )
        )
        return 0

    # THE FAIL-OPEN BRANCH, and it was bypassable by the party being gated.
    #
    # It used to read "no credential in the environment -> allow". `runflow`
    # pointed out that the agent CONTROLS that environment: `unset
    # AGENTBUS_API_KEY` and the gate waves everything through. A control whose
    # off-switch is held by the thing it constrains is not a control.
    #
    # So the question is no longer "does this session have a credential" but
    # "IS THIS HOST CONFIGURED FOR AGENTBUS AT ALL". A host with key files in
    # ~/.config/agentbus/keys is a host that opted in, and a session there with
    # no credential is unexplained rather than innocent — so it DENIES.
    #
    # A host with no AgentBus configuration whatsoever never opted in, and
    # denying its every tool call would be an outage dressed as security. That
    # one still allows, and it is not bypassable by unsetting anything: you
    # cannot reach it from a configured host without deleting the operator's
    # key files, which is a louder act than this hook could hide anyway.
    api_key = os.environ.get("AGENTBUS_API_KEY")
    agent = _resolve_agent()
    if not api_key or not agent:
        # OPT-IN IS PER-PROJECT, NEVER PER-HOST. A session is AgentBus-gated only
        # when ITS OWN project config declares the identity, so an unrelated
        # session that merely inherited `AGENTBUS_AGENT` from the shell (or from
        # another app's env) is NOT gated. This is the 2026-08-10 incident: a
        # plugin auto-loaded for every opencode session on the host made a prism
        # session that never opted in get EVERY tool denied.
        #
        # The project's own declaraton is the honest opt-in: `.claude/
        # settings.local.json` is written by `agentbus setup`, is per-checkout,
        # and is the most specific signal a Claude session has. An env var alone
        # is not enough — it leaks. A hosted key file alone is not enough —
        # another project on the host may be wired. Only the project declaring
        # the identity makes THIS session an AgentBus session.
        # RESOLVED FROM THE REPO ROOT. Reading `.claude` relative to the cwd made
        # this gate FAIL OPEN in a subdirectory: a project that HAD opted in
        # looked un-opted-in to a hook firing from `sdk/`, so the deny branch
        # below was skipped and the session sailed through ungated. Wrong in the
        # unsafe direction, and invisible, because the symptom of a skipped gate
        # is that nothing happens.
        def _project_opted_in() -> bool:
            try:
                from pathlib import Path as _P

                local = (_identity._repo_root() or _P.cwd()) / ".claude" / "settings.local.json"
                if not local.is_file():
                    return False
                import json as _json

                data = _json.loads(local.read_text())
                return bool((data.get("env") or {}).get("AGENTBUS_AGENT"))
            except Exception:
                return False

        if _project_opted_in():
            # The project opted in but THIS session presents no credential.
            # Per #107 this DEGRADES rather than blocks: the action runs, but
            # the warning must be explicit that it is running WITHOUT approval
            # checking, so a degraded session is never mistaken for a protected
            # one. (Before #107 this was a deny — that is the hostage behaviour
            # that locked the operator out of their own machine.)
            # SEV-1-A: telemetry so a week of silent allowlisting is discoverable.
            with contextlib.suppress(Exception):
                record_gate_degraded(
                    agent or "unknown", "no_credential", "project opted-in, session has no key"
                )
            return decide(
                "allow",
                "this project is wired for AgentBus but this session has no "
                "credential, so the guard CANNOT check whether this action "
                "needs approval. The action runs UNVETTED. This is a degraded "
                "session, not a protected one. To restore gating, source the "
                "agent's key file (agentbus signin) and re-run.",
            )
        return decide("allow", "this project has not opted into AgentBus; ungated")

    base = os.environ.get("AGENTBUS_BASE_URL", "https://agentbus.rodmena.co.uk")

    # SEV-1-C (#234): CROSS-PROCESS FAST-FAIL CIRCUIT reusing the degraded state
    # file. When the bus is stuck (rolling deploy, network partition), every hook
    # invocation was independently paying the full timeout — a ten-tool-call turn
    # waited 4+ minutes even though the state was "bus down" the whole time. We
    # cannot share a bulkman across per-invocation Python subprocesses, but we can
    # share a state file: if the degraded record shows N recent failures within a
    # short cooldown, subsequent calls fail-fast (still degrade to allow, but at
    # 0ms) until a real verdict resets it. Turns a 5-minute wall clock into a
    # sub-second one, without weakening the deny path (a deny is still uncached).
    _FAST_FAIL_THRESHOLD = int(os.environ.get("AGENTBUS_GATE_FAST_FAIL_AFTER", "3"))
    _FAST_FAIL_COOLDOWN = float(os.environ.get("AGENTBUS_GATE_FAST_FAIL_COOLDOWN", "30"))
    try:
        state_path = _gate_degraded_file(agent)
        if state_path.exists():
            state = json.loads(state_path.read_text())
            count = int(state.get("count") or 0)
            last_at = state.get("last_at") or ""
            # A CONNECT failure opens the circuit at once (peer review C5): the
            # network is gone, and making the next ten tool calls each re-discover
            # that is what a user feels as "the client freezes on network drop".
            if last_at and (
                count >= _FAST_FAIL_THRESHOLD or state.get("reason") == "connect_failure"
            ):
                # REG-1 (round-3 audit): last_at is written with time.gmtime()
                # (UTC — see record_gate_degraded), so it MUST be parsed back as
                # UTC. time.mktime() interprets local; on BST that reads the
                # timestamp ~1h in the past and the cooldown never trips, on
                # US-Pacific ~8h in the future and it always trips. calendar.timegm
                # is the timezone-safe pair for gmtime.
                import calendar

                last_ts = calendar.timegm(time.strptime(last_at, "%Y-%m-%dT%H:%M:%SZ"))
                if (time.time() - last_ts) < _FAST_FAIL_COOLDOWN:
                    # Record this too — a fast-fail is still a degraded call.
                    with contextlib.suppress(Exception):
                        record_gate_degraded(agent, "fast_fail", f"circuit open (count={count})")
                    return decide(
                        "allow",
                        f"AgentBus gate is FAST-FAILING (circuit open, {count} recent "
                        f"failures; cooldown {int(_FAST_FAIL_COOLDOWN)}s). Action runs "
                        "UNVETTED — approval checking is OFF. Fix the bus or the "
                        "credential; a real verdict clears the circuit.",
                    )
    except Exception:
        pass

    # Budgets (peer review C5; previously 12s x 2 attempts = ~24s per tool call
    # on a dead network): a 1.5s TCP reachability check first, then a 4s read
    # budget for the verdict. A guard must be fast; a slow bus is a broken bus.
    _GATE_TIMEOUT = float(os.environ.get("AGENTBUS_GATE_TIMEOUT", "4"))
    _GATE_CONNECT_TIMEOUT = float(os.environ.get("AGENTBUS_GATE_CONNECT_TIMEOUT", "1.5"))
    reachable, why = _bus_reachable(base, _GATE_CONNECT_TIMEOUT)
    if not reachable:
        with contextlib.suppress(Exception):
            record_gate_degraded(agent, "connect_failure", why)
        return decide(
            "allow",
            f"AgentBus is unreachable ({why}), so this action runs UNVETTED — approval "
            f"checking is OFF until the bus is reachable again (circuit open for "
            f"{int(_FAST_FAIL_COOLDOWN)}s). Restore the network or the bus, then re-run "
            "any gated tool to confirm gating is back on.",
        )

    # ONE retry, on TRANSIENT failures only. david measured five 503s in an
    # evening, each recovered within seconds with /healthz green immediately
    # after — they are our own rolling reloads, not an outage.
    #
    # The gate changed what a blip costs. Before it, a 503 failed the send you
    # were making; with a `*` matcher it now stops whatever you were doing,
    # whether or not the bus was involved. A single retry absorbs every blip of
    # that shape.
    #
    # THIS DOES NOT WEAKEN FAIL-CLOSED, and the distinction is the whole point:
    # exhausting the retry still DENIES. A refusal is never retried, because a
    # refusal is an answer — only the absence of an answer is retried. Retrying
    # a 403 or a parsed `deny` would be the change that guts this control, so
    # the retry is scoped to the exception path and nothing else.
    body: dict[str, Any] | None = None
    last_exc: BaseException | None = None
    retired_detail: str | None = None
    for attempt in (0, 1):
        try:
            request = urllib.request.Request(
                f"{base.rstrip('/')}/v1/guard/check",
                data=json.dumps({"tool_name": tool_name, "tool_input": tool_input}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "X-AgentBus-Agent": agent,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=_GATE_TIMEOUT) as response:
                body = json.loads(response.read().decode())
            break
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # A retired agent is a DEFINITIVE answer, not an absence of one:
            # the bus is fine, the identity is gone, and the fix is on the
            # caller's side. Read the problem body so we can say exactly that
            # instead of "could not be checked", which sent the container-registry
            # builder session hunting a phantom bus outage (2026-08-11, #89).
            if exc.code == 410:
                try:
                    detail = json.loads(exc.read().decode()).get("detail") or ""
                    if "retired" in detail.lower():
                        retired_detail = detail
                except Exception:
                    pass
                break
            # 5xx is "we could not answer"; 4xx is an answer we must not retry.
            # Retrying a 401 would hammer the bus with a credential that will
            # never work, and turn one clear failure into two.
            if exc.code < 500 or attempt:
                break
            time.sleep(0.25)
        except Exception as exc:
            # Transport-class failure (timeout, reset, DNS): NO retry (peer review
            # C5) — the reachability check above already passed, so this is a
            # slow or dying bus and a second full budget buys nothing.
            last_exc = exc
            break

    if body is None:
        # NO ANSWER FROM THE GUARD = THE SESSION RUNS UNVETTED, IT IS NEVER
        # BLOCKED. Operator directive #107: a revoked key or unreachable bus
        # must degrade, not imprison. The action is allowed because the guard
        # could not produce a verdict — and the warning says so loudly, so a
        # degraded session is never mistaken for a protected one.
        if retired_detail:
            # SEV-1-A telemetry: agent-retired is a real, actionable state.
            with contextlib.suppress(Exception):
                record_gate_degraded(agent, "identity_retired", retired_detail)
            return decide(
                "allow",
                f"{retired_detail} The guard could not verify this action "
                "because the agent identity is retired, so it runs UNVETTED. "
                "Re-register the agent name (agentbus register) to restore "
                "gating.",
            )

        # SEV-1-A telemetry: the reason names the exception class so watch-status
        # can distinguish a burst of 401s (rotate a key) from a burst of 503s (bus
        # rolling deploy) without opening the file.
        with contextlib.suppress(Exception):
            reason_slug = type(last_exc).__name__.lower() if last_exc else "unknown"
            record_gate_degraded(agent, reason_slug, f"{last_exc}")
        return decide(
            "allow",
            f"AgentBus could not verify this action ({last_exc}), so it runs "
            "UNVETTED — approval checking is OFF for this call, not just for "
            "this action. If this session's actions need human approval, "
            "restore the credential (agentbus signin) and re-run.",
        )

    # A REAL verdict from the guard (allow or deny) clears the degraded record —
    # gating is provably back on. Only real answers clear it; the degraded paths
    # above never do.
    with contextlib.suppress(Exception):
        clear_gate_degraded(agent)
    if body.get("decision") == "allow":
        return decide("allow", str(body.get("reason") or "permitted"))
    return decide("deny", str(body.get("reason") or "this action requires human approval"))
