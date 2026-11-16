from __future__ import annotations

from typing import TYPE_CHECKING

from agentbus_client._omo import LIVE, STALE, UNREADABLE, omo_holder

from . import _common
from ._watch_runtime import _watch_pids

if TYPE_CHECKING:
    import argparse

EXIT_HELD_BY_OTHER_HARNESS = 9
EXIT_HELD = 4


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


def cmd_watch_check(args: argparse.Namespace) -> int:
    agent = _common.require_acting_agent(args)
    if not agent:
        return 2
    harness = getattr(args, "harness", None) or "claude"
    session = getattr(args, "session", None)
    held: list[str] = []
    if harness != "omo":
        holder = omo_holder(agent)
        if holder.live:
            held.append(f"omo session, pid {holder.pid} [{holder.path}]")
    for state_key, pid in _watch_pids(agent).items():
        if session and session in state_key:
            continue
        held.append(f"agentbus watch, pid {pid} [{state_key}]")
    if held:
        print(f"held: {agent} already has a watcher:")
        for line in held:
            print(f"  {line}")
        return EXIT_HELD
    print(f"free: no other watcher holds {agent}")
    return 0
