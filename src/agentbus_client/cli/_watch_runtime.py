"""The `agentbus` command line client."""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from ._common import _cfg_dir


def _watch_runtime_dir(create: bool = True) -> Path:
    """The watcher runtime dir. `create=False` tolerates a read-only parent.

    `watch-status` diagnosed the config dir as unwritable and then DIED trying to
    mkdir inside it, one line later — a diagnostic command failing on exactly the
    condition it had just reported. david caught it on 0.4.27, the release that
    added the diagnosis.

    It only bites when `watchers/` does not already exist: a fresh host, a fresh
    container, a first run. That is precisely the population that has never had a
    working wake path, and `watch-status` is the command you would tell them to
    run. They got a stack trace instead of the second half of the answer.

    A diagnostic must be the LAST thing that fails on a broken filesystem.
    """
    d = _cfg_dir() / "watchers"
    if create:
        with contextlib.suppress(OSError):
            d.mkdir(parents=True, exist_ok=True)
    return d


def _watch_pidfile(agent: str, state: str | None = None) -> Path:
    """One registration per (agent, state) — NEVER one per agent alone.

    A per-agent pid file has a capture bug (bob, 2026-08-10, reproduced both
    directions): a second watcher for the same agent overwrites the single
    `{agent}.pid` with its own pid, `watch-status` then reports the SECOND —
    possibly an --append recorder that cannot wake — as the live watcher (false
    positive), and when that second watcher exits it DELETES the file, so
    watch-status reports NOT running while the real wake path is alive (false
    negative). Both error directions from one bug, and it explains the
    unreproducible false negative david reported.

    Keying on the state file means each watcher owns its own slot and can only
    ever delete its own registration. The `state` key is the STATE FILE NAME
    (not the path), so the file stays small and the intent stays readable.
    """
    if state:
        return _watch_runtime_dir() / f"{agent}-{state}.pid"
    # Back-compat: a watcher with no explicit state uses the legacy single slot.
    return _watch_runtime_dir() / f"{agent}.pid"


def _watch_logfile(agent: str, state: str | None = None) -> Path:
    """Where a DAEMONISED watcher's output goes.

    #204: KEYED LIKE THE PIDFILE, for the same reason the pidfile is keyed.
    The pid file has been per-(agent, state) since #160; the log was not, so N
    watchers for one agent shared one advertised path while only the daemon one
    ever wrote it. On my own host the RUNNING watcher was the monitor-state one
    while the existing `<agent>.log` came from a different, earlier daemon run —
    so watch-status named a log that existed and was not that watcher's.

    Reported by bikeroom-freebsd-operato-dd8bca, whose box shows the other half:
    a state-keyed pidfile and NO log beside it, because their watcher was
    started by the plugin rather than by `watch --daemon`. Two hosts, opposite
    symptoms, one wrong key.
    """
    if state:
        return _watch_runtime_dir() / f"{agent}-{state}.log"
    return _watch_runtime_dir() / f"{agent}.log"


def _existing_logfile(agent: str, state: str | None = None) -> tuple[Path, bool] | None:
    """The log that ACTUALLY EXISTS for this watcher, or None.

    watch-status used to print `log: <path>` unconditionally. A path is not a
    log: a watcher started in the foreground, by the plugin, or under rc.d never
    creates one, so the line named a file nothing had written and a reader
    grepping it for a symptom got an empty result they could not distinguish
    from "the symptom did not occur". That is the shape #160 fixed ten lines
    below this, on the same function.

    Returns (path, is_this_watchers) or None.

    Checks the state-keyed name first, then the legacy shared one — a daemon
    started before this change is still writing to the old path, and reporting
    "no log" for a log that exists would be the same lie in the other
    direction. But the legacy path is SHARED, so a hit there is not evidence
    that the file belongs to the watcher being reported on: on my own host the
    running watcher is the monitor-state one while the legacy log came from an
    earlier, different daemon run. The caller must say so rather than let a
    reader assume attribution the filename cannot support — silently preferring
    the fallback would reintroduce the exact defect this change removes.
    """
    keyed = _watch_logfile(agent, state)
    if state and keyed.exists():
        return keyed, True
    legacy = _watch_logfile(agent)
    if legacy.exists():
        return legacy, state is None
    return None


def _scan_watch_process(agent: str) -> int | None:
    """A live `agentbus watch --agent <agent>` found by PROCESS SCAN (#160).

    The fallback for a vanished pidfile — never the primary (the pidfile
    carries the state key watch-stop needs). `ps -axwwo` per #117: FreeBSD's
    `-eo` without a controlling terminal lists only gettys, and BSD ps
    truncates args without -ww.
    """
    import subprocess as _sp

    try:
        out = _sp.run(
            ["ps", "-axwwo", "pid,args"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, _sp.SubprocessError):
        return None
    # SEV-3 (#234): match `--agent foo`, `--agent=foo`, and `--agent 'foo'`
    # (quoted) — an operator alias that uses `=` or quotes never matched the
    # bare space-separated form and this fallback returned None silently. The
    # pidfile is still the primary source of truth; this improves the fallback.
    needle_re = _agent_flag_re(agent)
    for line in out.splitlines():
        if "agentbus" in line and " watch " in f" {line} " and needle_re.search(line):
            with contextlib.suppress(ValueError, IndexError):
                return int(line.split(None, 1)[0])
    return None


def _agent_flag_re(agent: str) -> re.Pattern[str]:
    """`--agent foo`, `--agent=foo`, `--agent 'foo'` — the forms a watcher argv can carry."""
    return re.compile(rf"(?:^|\s)--agent(?:=|\s+)['\"]?{re.escape(agent)}['\"]?(?:\s|$)")


def _pid_cmdline(pid: int) -> str | None:
    """The command line of `pid`, or None if it cannot be read (gone, or foreign)."""
    with contextlib.suppress(OSError):
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    import subprocess as _sp

    try:
        out = _sp.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, _sp.SubprocessError):
        return None
    return out or None


def _pid_is_watcher(pid: int, agent: str) -> bool:
    """Is `pid` an agentbus watcher FOR THIS AGENT — not merely a live process?

    `os.kill(pid, 0)` proves a process exists, not which one (review #23,
    issuedb #28): pidfiles survive reboots, PIDs restart low, and a stale file
    made `watch-status` report `sleep 300` as RUNNING and `watch-stop` SIGTERM
    it. The command line is the identity check; a PID we cannot read is treated
    as stale, never as ours.
    """
    line = _pid_cmdline(pid)
    if not line:
        return False
    return (
        "agentbus" in line and " watch " in f" {line} " and bool(_agent_flag_re(agent).search(line))
    )


def _watch_pids(agent: str) -> dict[str, int]:
    """Every live watcher registration for this agent: {state_key: pid}.

    Enumerates ALL state-keyed pid files PLUS the legacy per-agent slot,
    additively — always. bob's residual (2026-08-10): consulting the legacy slot
    only when no state-keyed entry exists re-creates the original FALSE POSITIVE
    for upgraders. A pre-0.4.43 watcher (the real wake path) lives in the legacy
    {agent}.pid slot forever, and when anything starts a 0.4.43 watcher, that
    legacy waker must still be reported — not shadowed behind a newcomer. So
    BOTH are returned, and the legacy one is labeled `(legacy)` so a reader can
    tell which slot is which. Each entry is verified alive; a stale file is
    best-effort unlinked.
    """
    out: dict[str, int] = {}
    import glob as _glob

    for path in _glob.glob(str(_watch_runtime_dir(create=False) / f"{agent}-*.pid")):
        try:
            pid = int(Path(path).read_text().strip())
            os.kill(pid, 0)
            if not _pid_is_watcher(pid, agent):
                raise ProcessLookupError(f"pid {pid} is not an agentbus watcher for {agent}")
            out[Path(path).name] = pid
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                Path(path).unlink(missing_ok=True)
    # The legacy per-agent slot (no state key) is ALWAYS added when alive. It
    # cannot be shadowed by a state-keyed newcomer, and a state-keyed survivor
    # is never hidden behind it — both are reported, legacy labeled.
    try:
        pid = int(_watch_pidfile(agent).read_text().strip())
        os.kill(pid, 0)
        if not _pid_is_watcher(pid, agent):
            raise ProcessLookupError(f"pid {pid} is not an agentbus watcher for {agent}")
        out["(legacy)"] = pid
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            _watch_pidfile(agent).unlink(missing_ok=True)
    return out


def _state_key_for(want_state: str, agent: str | None = None) -> str:
    """Normalise a user-supplied `--state` into the state-file NAME to match.

    The slot on disk is `{agent}-{state}.pid`. The user passes `--state
    own-state.json` (any path) and must NOT have to know the internal `{agent}-`
    prefix or the trailing `.pid` (bob, 2026-08-10: the first implementation
    matched only the internal slot filename — the documented form silently did
    nothing). Strip the dirname and a trailing `.pid`. When the agent is known,
    ALSO strip an EXACT leading `{agent}-` so a pasted slot name
    (`bob-own-state.json`) resolves to the same state name as the file path
    (`own-state.json`).
    """
    name = Path(want_state).name
    name = name.removesuffix(".pid")
    if agent and name.startswith(f"{agent}-"):
        name = name[len(f"{agent}-") :]
    return name


def _slot_state(st: str, agent: str) -> str:
    """The state name a pid-slot filename encodes: `{agent}-{state}.pid`."""
    if st == "(legacy)":
        return "(legacy)"
    return st.removesuffix(".pid").removeprefix(f"{agent}-")


def _scope_pids_by_state(pids: dict[str, int], agent: str, want_state: str) -> dict[str, int]:
    """Filter `pids` to those matching a user-facing `--state` selector.

    Accepts, per bob's prescription (strip dirname, strip a trailing .pid,
    then match by the state name):
      * the state-file name         `own-state.json`
      * any path to it              `/abs/path/own-state.json`
      * the slot name               `bob-own-state.json` / `.json.pid`
    Every form normalises to the STATE name, and the SELECTED slot is the one
    whose encoded state equals it. The legacy slot `(legacy)` never matches —
    it has no state file.
    """
    want = _state_key_for(want_state, agent)  # path + .pid + {agent}- -> state name
    if not want:
        return {}
    out: dict[str, int] = {}
    for st, pid in pids.items():
        if st == "(legacy)":
            continue
        if _slot_state(st, agent) == want:
            out[st] = pid
    return out


def _watch_pid(agent: str, state: str | None = None) -> int | None:
    """The PID of a live watcher for this agent (state-scoped), or None.

    Checks the process is actually alive rather than trusting the file — a stale
    pidfile from a killed process is exactly the kind of evidence that looks like
    proof and is not.
    """
    try:
        pid = int(_watch_pidfile(agent, state).read_text().strip())
    except (OSError, ValueError):
        # OSError covers an unreadable or absent runtime dir as well as a missing
        # pidfile: "no watcher" is the right answer for all of them, and none of
        # them should stop the caller learning the rest.
        return None
    try:
        os.kill(pid, 0)
        if not _pid_is_watcher(pid, agent):
            raise ProcessLookupError(f"pid {pid} is not an agentbus watcher for {agent}")
    except (OSError, ProcessLookupError):
        # Stale pidfile from a dead (or REUSED, #28) pid. Best-effort: on a
        # read-only config dir we cannot clean it up, and failing to tidy must
        # not stop us answering the question that was asked.
        with contextlib.suppress(OSError):
            _watch_pidfile(agent, state).unlink(missing_ok=True)
        return None
    return pid
