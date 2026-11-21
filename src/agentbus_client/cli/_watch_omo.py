from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentbus_client._omo import LIVE, STALE, UNREADABLE, omo_holder

from . import _common
from ._common import _print
from ._watch_runtime import _watch_pids

if TYPE_CHECKING:
    import argparse

EXIT_HELD_BY_OTHER_HARNESS = 9
EXIT_HELD = 4
_SESSION_UUID = re.compile(
    r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json(?:\.pid)?$"
)


def omo_refusal(agent: str) -> int | None:
    holder = omo_holder(agent)
    if not holder.live:
        return None
    print(
        f"AgentBus watch: an omo session (pid {holder.pid}) already holds the stream "
        f"for '{agent}' ({holder.path})."
    )
    print("  Two watchers on one identity duplicate every wake, so this one will not start.")
    print("  Use a git worktree for a second concurrent session; it is its own agent.")
    print(f"  If no omo session is running for this agent, delete {holder.path}.")
    return EXIT_HELD_BY_OTHER_HARNESS


def watch_refusal(args: argparse.Namespace, agent: str) -> int | None:
    if getattr(args, "once", False):
        return None
    return omo_refusal(agent)


def print_omo_status(agent: str) -> bool:
    holder = omo_holder(agent)
    if holder.state == LIVE:
        print(f"omo watcher RUNNING for {agent} (pid {holder.pid}) [{holder.path}]")
        print("  in-process in an omo session; watch-stop does not stop it.")
        return True
    if holder.state == STALE:
        print(f"omo lock STALE for {agent}: {holder.reason} [{holder.path}]")
    elif holder.state == UNREADABLE:
        print(f"omo lock UNREADABLE for {agent}: {holder.reason} [{holder.path}]")
    return False


def omo_stop_note(agent: str) -> None:
    holder = omo_holder(agent)
    if holder.live:
        print(
            f"not stopped: the omo watcher for {agent} (pid {holder.pid}) runs inside an "
            "omo session. End that session to stop it."
        )


def _registration_holder(state_key: str, pid: int) -> dict[str, object]:
    match = _SESSION_UUID.search(state_key)
    session = match.group(1) if match else None
    return {
        "kind": "session_monitor" if session else "watch",
        "pid": pid,
        "state_key": state_key,
        "session": session,
    }


def _holder_line(holder: dict[str, object]) -> str:
    if holder["kind"] == "omo":
        return f"omo session, pid {holder['pid']} [{holder['path']}]"
    return f"agentbus watch, pid {holder['pid']} [{holder['state_key']}]"


def cmd_watch_check(args: argparse.Namespace) -> int:
    agent = _common.require_acting_agent(args)
    if not agent:
        return 2
    harness = getattr(args, "harness", None) or "claude"
    session = getattr(args, "session", None)
    holders: list[dict[str, object]] = []
    if harness != "omo":
        holder = omo_holder(agent)
        if holder.live:
            holders.append(
                {"kind": "omo", "pid": holder.pid, "path": str(holder.path), "session": None}
            )
    for state_key, pid in _watch_pids(agent).items():
        if session and session in state_key:
            continue
        holders.append(_registration_holder(state_key, pid))
    if getattr(args, "json", False):
        _print({"agent": agent, "free": not holders, "holders": holders}, as_json=True)
    elif holders:
        print(f"held: {agent} already has a watcher:")
        for item in holders:
            print(f"  {_holder_line(item)}")
    else:
        print(f"free: no other watcher holds {agent}")
    return EXIT_HELD if holders else 0
