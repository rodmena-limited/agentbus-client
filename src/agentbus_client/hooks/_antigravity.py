"""Antigravity's hook protocol: camelCase stdin, JSON stdout (#56).

Every Antigravity-specific wire detail lives here. `_inject.py` and `_gate.py`
stay Claude-shaped and untouched, because the two protocols agree on nothing
except that a hook is a process:

    Claude Code            Antigravity
    -----------            -----------
    re-wake = exit 2       re-wake = {"decision": "continue"}
    context = stdout text  context = {"injectSteps": [{"ephemeralMessage": ...}]}
    cwd     = the project  cwd     = the directory containing hooks.json
    AGENTBUS_AGENT in env  nothing in env at all

THE CWD TRAP, WHICH IS THE WHOLE REASON THIS MODULE EXISTS.

Antigravity's own docs say, verbatim: "The working directory is set to the
directory containing `hooks.json`." Our hooks.json is inside the plugin, so cwd
is `<repo>/.agents/plugins/agentbus/`. `hooks._identity._resolve_agent()` walks
UP from cwd looking for `.agentbus/agent`, and from inside the plugin directory
that walk still reaches the repo root — so a cwd-based implementation appears to
work here and would break the moment the plugin moves anywhere else (a global
install, a plugin registered from a shared path). The payload carries
`workspacePaths[0]`, which is the answer the harness actually knows, so we use
it and keep cwd only as a fallback.

PRECEDENCE IS UNCHANGED (#90). `$AGENTBUS_AGENT` still outranks everything: the
three components that resolve identity must never disagree about who this
session is, and a fourth rule here would be a fourth thing to keep in sync.

NOTHING HERE MAY EXIT NON-ZERO OR RAISE. These hooks run synchronously inside
the agent's loop; an exception would surface as a broken harness rather than a
quiet AgentBus. Every path prints one valid JSON object and returns 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ._identity import (
    _adopt_credential_for,
    _read_declared_agent,
    _resolve_agent,
)

# `Stop` requires a decision; the docs say any value other than "continue" lets
# the agent stop. We send an explicit "stop" rather than {} so the no-mail case
# states its intent instead of relying on the parser's tolerance for a missing
# required field.
_STOP = {"decision": "stop"}
_NOOP: dict[str, Any] = {}


def _emit(payload: dict[str, Any]) -> int:
    """One JSON object on stdout, always, and always exit 0."""
    try:
        sys.stdout.write(json.dumps(payload))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    return 0


def _payload() -> dict[str, Any]:
    """Read the hook payload from stdin. A bad payload is an empty one.

    Read ONCE, here, and passed down — `_turn.pending` reads stdin in two
    different places for Claude and that is a bug waiting for a second consumer.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _workspace(payload: dict[str, Any]) -> Path | None:
    """`workspacePaths[0]`, accepted only if it is an existing absolute dir.

    A relative or non-existent path is worse than none: it would silently
    resolve identity against the wrong tree. Absent is honest; wrong is not.
    """
    paths = payload.get("workspacePaths")
    if not isinstance(paths, list) or not paths:
        return None
    first = paths[0]
    if not isinstance(first, str) or not first:
        return None
    try:
        candidate = Path(first)
        if candidate.is_absolute() and candidate.is_dir():
            return candidate
    except (OSError, ValueError):
        return None
    return None


def _agent_for(payload: dict[str, Any]) -> str | None:
    """Who is this session? Env, then the payload's workspace, then cwd.

    THE OPT-IN CHECK IS NOT A FORMALITY. This plugin is machine-wide, so these
    hooks fire in every agy session on the box — and `.agentbus/agent` already
    exists in every checkout wired for Claude Code or opencode. Acting on that
    file alone would put a bus poll and a foreground Stop pause into projects
    that never asked for agy. So a workspace must have been through
    `agentbus setup agy` before we do anything for it.
    """
    declared = os.environ.get("AGENTBUS_AGENT")
    if declared:
        return declared
    workspace = _workspace(payload)
    if workspace is not None:
        from ..onboarding._paths import agy_is_wired

        if not agy_is_wired(workspace):
            return None
        found = _read_declared_agent(workspace)
        if found:
            return found
    # Fallback for a plugin that happens to sit inside the repo. Keeps this
    # module working if a future install moves hooks.json somewhere the payload
    # does not describe.
    return _resolve_agent()


def _with_credential(agent: str) -> bool:
    """Adopt the agent's bound key, unless the operator already exported one.

    Antigravity puts NOTHING in the hook's environment — no agent name and no
    key — so unlike the Claude lane there is no shell preamble doing `set -a`
    sourcing, and therefore none of that lane's shell-traversal surface. The
    sanitizing happens in `_adopt_credential_for`, which routes the agent name
    through `sealing.bound_env_filename`, so a hostile `.agentbus/agent` cannot
    reach `operator.env` by traversal.

    An explicitly exported key WINS: an operator who set one meant it.

    `_adopt_credential_for` is SILENT, BEST-EFFORT, and returns None — it works
    by mutating `os.environ`. So the effect is what gets verified here, not a
    return code: "no key file for this agent" is a real state (a worktree wired
    but never signed in), and treating the call itself as success would send us
    on to poll the bus with no credential and report the resulting silence as an
    empty inbox.
    """
    if os.environ.get("AGENTBUS_API_KEY"):
        return True
    try:
        _adopt_credential_for(agent)
    except Exception:
        return False
    return bool(os.environ.get("AGENTBUS_API_KEY"))


def _mail_summary(text: str) -> str:
    """Trim polled mail into something that reads as a system message."""
    cleaned = " ".join(text.split())
    if len(cleaned) > 900:
        cleaned = cleaned[:900] + " […]"
    return cleaned


def agy_stop(_args: object = None) -> int:
    """The ACTIVE wake lane: `Stop` -> {"decision": "continue"}.

    THE LEDGER IS NOT OPTIONAL HERE, AND THE FAILURE IS WORSE THAN CLAUDE'S.
    `{"decision": "continue"}` re-enters the agent loop, so a re-wake that is
    not claimed does not merely double a notification — it spins the model
    against the same unread message forever, burning quota with no human in the
    room. `poll_for_fresh_mail` claims through the same flock'd ledger the
    Claude re-waker uses, so one delivery wakes a session exactly once no matter
    which harness observed it first.

    The window is short because agy hooks BLOCK the loop (see AGY_WAKE_WINDOW_SEC).
    This is a turn-boundary catch, not an idle hold: always-attached
    reachability comes from `agentbus service`, exactly as it does elsewhere.
    """
    payload = _payload()
    agent = _agent_for(payload)
    if not agent:
        return _emit(_STOP)
    if not _with_credential(agent):
        return _emit(_STOP)

    from ..onboarding._paths import AGY_WAKE_WINDOW_SEC

    window = int(os.environ.get("AGENTBUS_REWAKE_WINDOW", str(AGY_WAKE_WINDOW_SEC)))
    interval = max(1, int(os.environ.get("AGENTBUS_REWAKE_INTERVAL", "5")))
    try:
        from ..rewake import poll_for_fresh_mail

        text = poll_for_fresh_mail(agent, window=window, interval=interval)
    except Exception:
        # A bus failure must never hold the loop or look like a harness fault.
        return _emit(_STOP)
    if not text:
        return _emit(_STOP)
    return _emit(
        {
            "decision": "continue",
            "reason": (
                "AgentBus mail arrived while you were finishing. Read it before "
                "you stop:\n\n" + _mail_summary(text)
            ),
        }
    )


def agy_preinvocation(_args: object = None) -> int:
    """The PASSIVE catch-up lane: `PreInvocation` -> injectSteps.

    `ephemeralMessage`, NEVER `userMessage`. A userMessage reads as something a
    human typed, which is the #91 class of bug: the agent answers a prompt
    nobody wrote. An ephemeral system note says where it came from.

    Claims through the SAME ledger as `agy_stop`, and that sharing is required
    rather than tidy: PreInvocation fires before every model call INCLUDING the
    one that `Stop` just forced, so without a shared claim the message that
    caused the wake would be injected again on arrival.
    """
    payload = _payload()
    agent = _agent_for(payload)
    if not agent:
        return _emit(_NOOP)
    if not _with_credential(agent):
        return _emit(_NOOP)
    try:
        from ..rewake import poll_for_fresh_mail

        # window=0 -> one deterministic pass. This runs before EVERY model call;
        # holding here would put the whole window on the front of every turn.
        text = poll_for_fresh_mail(agent, window=0, interval=1)
    except Exception:
        return _emit(_NOOP)
    if not text:
        return _emit(_NOOP)
    return _emit(
        {
            "injectSteps": [
                {"ephemeralMessage": ("AgentBus: new mail for you.\n\n" + _mail_summary(text))}
            ]
        }
    )


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire the Antigravity hooks into `agentbus-hook`."""
    p = sub.add_parser("agy-stop", help="Antigravity Stop hook (active re-wake)")
    p.set_defaults(func=agy_stop)

    p = sub.add_parser("agy-preinvocation", help="Antigravity PreInvocation hook (catch-up)")
    p.set_defaults(func=agy_preinvocation)
