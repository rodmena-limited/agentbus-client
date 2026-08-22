"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

from . import _watch_runtime
from ._common import _accept_common_flags_after_subcommand, _cfg_dir
from ._watch_runtime import (
    _existing_logfile,
    _scan_watch_process,
    _scope_pids_by_state,
    _watch_pidfile,
    _watch_pids,
)


def _read_running_client_version(
    agent: str, state_key: str | None, for_pid: int | None = None
) -> str | None:
    """Read the `client_version` a running watcher persisted to its state file.

    Used by watch-status to spot a stale watcher: the state file is written
    by the WATCHER process, so its `client_version` is the version of the
    Python module that watcher imported at START — not the version of the
    CLI binary the operator just installed. If they differ, the upgrade
    did not restart the watcher.

    SHARES doctor's implementation rather than reimplementing it.
    macbook-admin-bd8e86 caught two successive misses here (thread
    01M08ZWE0XCTPJG1R0ZBXP8K7P, msgs 01M0916R4XW6K2NB248RYPR4DX and
    01M091QDY8KFYZSJPZGTA231ZG) and named the root pattern: "wherever two
    commands answer the same question, they should call ONE helper, not
    two similar ones."

    The specific bug their second report found: my copy globbed only
    `watch-*-<agent>.json`, which is the DEFAULT state-file name used when
    no --state is passed. The PLUGIN MONITOR — the production config for
    every Claude Code session — names its file `monitor-<agent>-*.json`.
    So the copy reported correctly for ad-hoc watchers and returned None
    for exactly the watchers that run in production. doctor's
    `_running_watcher_version` globs BOTH patterns and was right all
    along; this now calls it instead of paraphrasing it.

    `state_key` is preferred when the caller knows the exact state path —
    it is exact, needs no naming convention, and cannot drift again when
    someone invents a third state-file prefix (macbook's suggestion).
    """
    # 1. EXACT: the caller already identified the state file. Use it.
    if state_key and state_key != "(legacy)":
        candidate = _cfg_dir() / state_key.removesuffix(".pid")
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text())
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                # VERIFY THE STAMP BELONGS TO THE PROCESS WE ARE ASKING ABOUT.
                #
                # agentbus-ui-c760a1 spotted that `client_version` alone is
                # "last writer's version", not "this watcher's version": a
                # short-lived `agentbus watch --once` on a NEW cli stamps the
                # new version and exits while the long-running plugin monitor
                # carries on with OLD code. Comparing that stamp to the CLI
                # would report a match the instrument has not earned — the
                # exact failure class that produced this incident.
                stamped_pid = data.get("pid")
                if for_pid is not None and stamped_pid is not None and stamped_pid != for_pid:
                    return None  # renders as "cannot confirm", never a false match
                return data.get("client_version") or None
    # 2. FALLBACK: the ONE shared implementation, which globs both the
    #    `monitor-<agent>-*.json` (plugin) and `watch-*-<agent>.json`
    #    (default) naming schemes.
    from .. import onboarding as _onboarding

    return _onboarding._running_watcher_version(agent)


def cmd_watch_status(args: argparse.Namespace) -> int:
    agent = args.agent or os.environ.get("AGENTBUS_AGENT") or ""
    if not agent:
        print("no acting agent: pass --agent or set AGENTBUS_AGENT", file=sys.stderr)
        return 2
    # A SILENTLY FAILING WAKE PATH IS THE THING THIS COMMAND EXISTS TO FIND.
    #
    # `notify` runs inside the watcher's --exec and must never exit non-zero —
    # a hard failure there kills the wake path itself. So its failures are
    # recorded as STATE and surfaced here instead. david's principle: "not
    # fatal" and "not silent" are separable, and stderr inside a background
    # watcher is read by nobody.
    #
    # Printed BEFORE the RUNNING line, because a running watcher whose every
    # notify is failing is the case that most looks healthy.
    from ..hooks.claude_code import _gate_degraded_file, _hook_state_dir, _notify_error_file

    # THE FAILURE THE STATE FILE CANNOT REPORT IS THE MOST LIKELY ONE.
    #
    # notify records its failures into the state directory. If the failure IS
    # "cannot write to that directory", the record cannot be written either —
    # so the single most probable wake failure was the one case my own reporter
    # was blind to. A check that cannot go red in exactly the situation it
    # exists for, built hours after writing that rule down.
    #
    # Fixed by not asking the failing component to report itself: the condition
    # is observable HERE, at read time, without anything having been written.
    # THE EXIT CODE MUST REFLECT THE WAKE PATH, NOT JUST THE PROCESS.
    #
    # david flagged the rc as "a decision rather than an accident" and it was an
    # accident: the code depended only on whether a watcher was RUNNING. So a
    # host with a live watcher and an UNWRITABLE wake path returned 0 — a
    # wrapper or CI gate reading status saw success while every arrival was
    # being dropped.
    #
    # That is the same false-green this command was built to expose, one layer
    # out: the warning was on stdout and the status code disagreed with it.
    wake_broken = False
    state_dir = _hook_state_dir()
    if not state_dir.exists():
        wake_broken = True
        print(f"  WAKE PATH MISSING: {state_dir} does not exist — no arrival can be recorded.")
    elif not os.access(state_dir, os.W_OK):
        wake_broken = True
        print(
            f"  WAKE PATH UNWRITABLE: {state_dir} — arrivals CANNOT be recorded, "
            "and notify cannot even record that it failed."
        )

    err = _notify_error_file(agent)
    if err.exists():
        import json as _json

        with contextlib.suppress(Exception):
            data = _json.loads(err.read_text())
            print(f"  WAKE FAILING: last notify error at {data.get('at')}: {data.get('detail')}")
            print("    arrivals since then may not have been recorded.")

    # SEV-1-A (#234): the PreToolUse gate degrades to allow on any unreachable or
    # 5xx/401/parsing failure — deliberate (operator #107, a dead credential must
    # not lock the operator out) but silent. This surfaces the state file so a week
    # of allowlisted actions is discoverable rather than "gating just seemed off".
    gate_err = _gate_degraded_file(agent)
    if gate_err.exists():
        with contextlib.suppress(Exception):
            data = json.loads(gate_err.read_text())
            count = data.get("count", "?")
            print(
                f"  GATE DEGRADED: {count} action(s) allowed WITHOUT approval check "
                f"since {data.get('first_at')} (last at {data.get('last_at')}, "
                f"reason: {data.get('reason')})"
            )
            print(f"    detail: {data.get('detail')}")
            print(
                "    a REAL guard verdict clears this — restore the credential (agentbus "
                "signin) or fix the bus, then re-run any gated tool to confirm gating is back on."
            )
            wake_broken = True

    pids = _watch_pids(agent)
    want_state = getattr(args, "state", None)
    if want_state:
        pids = _scope_pids_by_state(pids, agent, want_state)
    if pids:
        # SEV-1 follow-up (macbook-admin-bd8e86): surface the running client
        # version of THIS watcher so stale-watcher-pretending-to-be-current
        # is visible at the first place someone would look. The version is
        # persisted per-run in the watcher's state file (`client_version`
        # field) and the CLI's own version is the alternative comparison
        # anchor.
        from .. import __version__ as _cli_ver

        for st, pid in pids.items():
            watcher_ver = _read_running_client_version(agent, st, for_pid=pid)
            ver_note = f" running={watcher_ver}" if watcher_ver else ""
            if watcher_ver and watcher_ver != _cli_ver:
                ver_note += f" (CLI is {_cli_ver}; RESTART TO PICK UP THE NEW BUILD)"
            print(
                f"watcher RUNNING for {agent} (pid {pid}){ver_note}"
                + (f" [{st}]" if st != "(legacy)" else " [legacy]")
            )
        # #204: report a log only if one EXISTS, and say why when it does not.
        # Only `watch --daemon` captures output; a foreground, plugin-managed or
        # rc.d watcher writes none, and naming a path in that case sends the
        # reader to grep a file that was never created.
        for st in pids:
            state_key = None if st == "(legacy)" else st
            found = _existing_logfile(agent, state_key)
            if found:
                path, is_ours = found
                if is_ours:
                    print(f"  log: {path}")
                else:
                    print(f"  log: {path}  (LEGACY SHARED PATH — may not be this watcher's)")
                    print(
                        "       written by whichever daemon last used the unkeyed name; "
                        "check its timestamps before trusting it as evidence."
                    )
            else:
                print(
                    f"  log: none for [{st}] — output is captured only by "
                    "`agentbus watch --daemon`;"
                )
                print(
                    "       this watcher was started another way (foreground, "
                    "plugin, rc.d), so there is nothing to read."
                )
    else:
        # #160: PIDFILE-ABSENT MUST NOT READ AS CHECKED-AND-DEAD. The pid-from-
        # pidfile check above is correct (verified on FreeBSD both directions by
        # its reporter) — but with no pidfile it had NOTHING to check and still
        # printed the same words as a confirmed-dead process. A live watcher
        # whose pidfile vanished (cause unknown; mechanism never established)
        # was reported NOT running, and everything downstream trusted it.
        # Absence of evidence, worded as evidence of absence. So: scan for a
        # matching live process (ps -axwwo — the #117 form; FreeBSD's -eo sees
        # only gettys), and say WHAT was checked either way.
        orphan = _scan_watch_process(agent)
        if orphan:
            print(
                f"watcher RUNNING for {agent} (pid {orphan}) — pidfile missing; "
                "restart the watcher to re-adopt it"
            )
            print(
                "  (found by process scan; the pidfile this tool normally reads is gone. "
                "watch-stop cannot manage it until restarted.)"
            )
        else:
            print(
                f"watcher NOT running for {agent}"
                + (f" matching --state {want_state!r}" if want_state else "")
            )
            print(
                "  checked: pidfiles in "
                f"{_watch_runtime._watch_runtime_dir(create=False)} and a full process scan — "
                "neither found it."
            )
        # #204: same rule on the not-running path. "last log: <path>" for a file
        # that never existed reads as "here is where the evidence is" when there
        # is no evidence at all.
        stale = _existing_logfile(agent, want_state)
        if stale:
            path, is_ours = stale
            suffix = "" if is_ours else "  (legacy shared path)"
            print(f"  last log: {path}{suffix}")
        else:
            print("  last log: none — no daemonised watcher has written one for this agent.")
    # 3 = the wake path itself is broken, and it OUTRANKS a running watcher:
    # a watcher whose every arrival is dropped is not a healthy wake path.
    # 1 = no watcher. 0 = watcher running and arrivals can be recorded.
    if wake_broken:
        return 3
    return 0 if pids else 1


def cmd_watch_stop(args: argparse.Namespace) -> int:
    import signal

    agent = args.agent or os.environ.get("AGENTBUS_AGENT") or ""
    # SCOPING, for the per-(agent,state) model: `--state NAME` stops exactly
    # the registrations whose pid-file state key is NAME (matched by SUFFIX so
    # `--state foobar.json` matches `{agent}-foobar.json.pid`). Without it, stop
    # every live registration for the agent (mailapi's artifact-level catch:
    # the stop surface had no way to name a slot after the state-keying landed).
    want_state = getattr(args, "state", None)
    pids = _watch_pids(agent)
    if want_state:
        pids = _scope_pids_by_state(pids, agent, want_state)
    if not pids:
        # FAIL LOUDLY: "no watcher matches this selector" is NOT "no watcher".
        # A silent no-match on a scoped stop means the operator believes the
        # watcher was stopped when nothing was (bob, 2026-08-10).
        if want_state:
            print(
                f"no running watcher for {agent or '<no agent>'} matching --state {want_state!r}",
                file=sys.stderr,
            )
            print(
                "  (the selector matched no registration — nothing was stopped.)", file=sys.stderr
            )
            return 2
        print(f"no running watcher for {agent or '<no agent>'}")
        return 1
    # Stop each selected registration via its OWN pid file so one slot being
    # stale never leaves another's registration behind.
    for st, pid in pids.items():
        os.kill(pid, signal.SIGTERM)
        _watch_pidfile(agent, None if st == "(legacy)" else st).unlink(missing_ok=True)
        print(
            f"stopped watcher for {agent} (pid {pid})"
            + (f" [{st}]" if st != "(legacy)" else " [legacy]")
        )
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("watch-status", help="is a watcher running for this agent?")
    p.add_argument(
        "--state", default=None, help="scope to one registration by state-file name (default: all)"
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_watch_status)

    p = sub.add_parser("watch-stop", help="stop the detached watcher for this agent")
    p.add_argument(
        "--state",
        default=None,
        help="stop exactly the registration with this state-file name "
        "(default: every live watcher for the agent)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_watch_stop)
