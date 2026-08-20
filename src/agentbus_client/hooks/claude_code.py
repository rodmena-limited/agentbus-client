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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Split into sibling modules (review #23, file-size cap); every name is re-exported
# here so `claude_code.<name>` keeps resolving. Tests patch helpers on the defining module.
from . import _gate, _identity, _inject, _session, _state, _turn
from ._gate import (
    _bus_reachable,
    pre_tool_use,
)
from ._identity import (
    _REPO_ROOT_CACHE,
    _adopt_credential_for,
    _agent_from_project_settings,
    _agent_from_worktree,
    _read_declared_agent,
    _repo_root,
    _resolve_agent,
    _worktree_identity_bleed,
)
from ._inject import (
    inject,
)
from ._session import (
    _greet_with_qr,
    _identity_claim_path,
    _identity_held_live,
    _is_self_send,
    _warn_if_env_overrides_this_checkout,
    _warn_if_identity_shared,
    session_start,
)
from ._state import (
    _HARNESS_NOTIFICATION_MARKERS,
    _SECRET_RE,
    EXIT_MISCONFIGURED,
    EXIT_NOT_WIRED,
    _gate_degraded_file,
    _hook_state_dir,
    _hook_warn,
    _is_harness_notification,
    _notify_error_file,
    _scrub,
    _wake_file,
    _warn_if_shadow_queue,
    clear_gate_degraded,
    clear_notify_failure,
    record_gate_degraded,
    record_notify_failure,
)
from ._turn import (
    _session_id_from_stdin,
    notify,
    pending,
    session_end,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbus-hook")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("session-start")
    p.set_defaults(func=session_start)

    p = sub.add_parser("inject")
    p.add_argument("--subject", default="(no subject)")
    p.add_argument("--sender", default="a peer")
    p.add_argument("--delivery", default="")
    p.add_argument("--seq", default="")
    p.add_argument("--direction", default="")
    # default=None, NOT "": absent means the monitor never told us, empty
    # means it told us the message is plain SMTP. See the envelope logic.
    p.add_argument("--inbound-source", default=None)
    # Persona lanes (SPECS/0021, SEV-2 fix). TWO distinct fields:
    #   --lane    = the SENDER's persona (backend #267 enrichment)
    #   --my-lane = the acting agent's OWN persona, used by the handoff
    #               reminder ("Your lane is: backend"). Passed by the
    #               --exec template's {my_lane} placeholder.
    # 0.9.34 used --lane for the reminder, so a frontend sender messaging
    # a backend receiver printed "Your lane is: frontend" — wrong. The
    # reminder must always reflect the RECEIVER's lane.
    p.add_argument("--lane", default=None)
    p.add_argument("--my-lane", default=None)
    p.set_defaults(func=inject)

    p = sub.add_parser("session-end")
    p.set_defaults(func=session_end)

    p = sub.add_parser("notify")
    p.add_argument("--subject", default="")
    p.add_argument("--sender", default="")
    p.add_argument("--delivery", default="")
    p.set_defaults(func=notify)

    p = sub.add_parser("pre-tool-use")
    p.set_defaults(func=pre_tool_use)

    p = sub.add_parser("pending")
    p.set_defaults(func=pending)

    # The resilient ACTIVE trigger — the Stop hook execs this. Kept in its own
    # module so the retry/breaker/failsafe machinery is not carried by the two
    # passive hooks that never need it.
    p = sub.add_parser("monitor")

    def _monitor(_a: argparse.Namespace) -> int:
        from ..rewake import monitor as _m

        rc: int = _m(_a)
        return rc

    p.set_defaults(func=_monitor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
