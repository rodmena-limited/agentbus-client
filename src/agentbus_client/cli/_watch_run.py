"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from ..client import AgentBusError, AuthError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _cfg_dir
from ._watch_runtime import _watch_logfile, _watch_pid, _watch_pidfile


def cmd_watch(args: argparse.Namespace) -> int:
    """Hold a stream open and act on every arriving message.

    This is the piece no server-side feature can replace: the bus pushes fine,
    but a session that is not running cannot be woken by anything the server
    does. Run this alongside a session and it will notice.
    """
    from pathlib import Path

    from .._coalesce import Coalescer
    from ..watch import Watcher, append_file, notify_command, print_line

    bus = _common._bus(args)
    agent = args.agent or bus.agent
    if not agent:
        print("no acting agent: pass --agent or set AGENTBUS_AGENT", file=sys.stderr)
        return 2

    # --exec and --append COMPOSE. They were an if/elif, so passing both silently
    # dropped the append: the messages were processed, the command fired, the
    # cursor advanced, and the JSONL file was simply never created. An empty
    # append file is indistinguishable from a quiet inbox, so nothing surfaced.
    #
    # Reported by `david` with an isolated-state reproduction and a control run
    # (same command, --exec removed -> 3 lines written). Made to compose rather
    # than rejected as mutually exclusive because /llms.txt actively recommends
    # both forms, so the combination a reader is steered into must work.
    handlers: list[Any] = []
    if args.exec:
        handlers.append(notify_command(args.exec))
    if args.append:
        handlers.append(append_file(Path(args.append)))
    if not handlers:
        handlers.append(print_line)

    def fanout(message: dict[str, Any]) -> None:
        # Each side-effect is isolated: a failing --exec must not swallow the
        # --append audit trail, which is often the only record of what arrived.
        for one in handlers:
            try:
                one(message)
            except Exception as exc:
                print(f"agentbus watch: handler failed: {exc}", file=sys.stderr)

    # MY-LANE INJECTION (SPECS/0021, SEV-2 fix). The acting agent's OWN
    # persona is injected as `my_lane` into a SHALLOW COPY of every message
    # and envelope — DISTINCT from the server's `lane` field, which is the
    # SENDER's persona (backend #267 enrichment).
    #
    # SEV-2 lesson: 0.9.34 stamped the acting agent's persona onto `lane`,
    # clobbering the sender's. Same field name, two meanings → the receiver's
    # persona overwrote the sender's on every wake. The two concepts must
    # have two names: `lane` = who SENT this, `my_lane` = who I am. This
    # wrapper only ever sets `my_lane` and never touches `lane`.
    #
    # One `my_lane` per envelope, never per message — the coalescer already
    # ensures one wake per burst. Absent when the acting agent has no
    # persona (the majority case), so existing hooks are byte-identical.
    def with_my_lane(message: dict[str, Any]) -> None:
        if my_lane:
            message = {**message, "my_lane": my_lane}
        fanout(message)

    # Coalescer (issuedb #9, SPECS/0009): burst arrivals collapse into a
    # single envelope wake. Lone messages still fire immediately with the
    # unchanged per-message shape, so installed UserPromptSubmit hooks that
    # grep the current fields keep working with no schema-version bump.
    coalescer: Coalescer | None = None
    handler: Callable[[dict[str, Any]], None]
    if getattr(args, "no_coalesce", False):
        handler = with_my_lane
    else:
        coalescer = Coalescer(
            with_my_lane,
            window_ms=int(getattr(args, "coalesce_window", 2500)),
            quiet_ms=int(getattr(args, "coalesce_quiet", 800)),
        )
        handler = coalescer.handle

    # The cursor state MUST be scoped by workspace as well as agent. Cursors are
    # per-delivery sequences inside one workspace and mean nothing in another, but
    # the same agent NAME routinely exists in several — `agentbus-dev` lives in
    # both `test` and `rodmena`. Keying this file on the name alone made a watcher
    # started in a second workspace resume from a foreign cursor and skip
    # everything below it, which is indistinguishable from an empty inbox.
    # Measured, not theorised: with a real message waiting, cursor 0 returned 1
    # and the leaked cursor 26 returned 0.
    # DETACH, so the watcher outlives the session that started it.
    #
    # The wake channel is an outbound SSE stream — correct for agents behind a
    # strict inbound firewall, which is most of them, and the same model a
    # browser or a mobile app uses. Webhooks are not an option for those boxes
    # and never were.
    #
    # What a browser gets for free is the LONG-LIVED PROCESS. `agentbus watch`
    # was a foreground child of the terminal, so it died with the session, in
    # silence, and the agent went dark while still looking fine from the server.
    # That happened twice in one afternoon to the same peer.
    # THE STATE KEY MUST BE RESOLVED BEFORE THE DAEMON BRANCH. The pid file is
    # keyed on it (bob's capture bug: a per-agent pid file lets a second watcher
    # clobber the survivor's registration), so the daemon path needs the same
    # derived state name the foreground path uses below.
    #
    # `workspace` is resolved HERE, once, because BOTH the state key and the
    # state path need it. It used to be referenced as `args.workspace` — a
    # namespace attribute that does not exist — so `agentbus watch` (or
    # `watch --once`) WITHOUT `--state` crashed on the very first line of the
    # daemon branch. The plugin monitor always passes `--state`, which is the
    # only reason this latent crash never reached a user until the wake-socket
    # probe ran it bare.
    workspace: str | None = None
    # SEV-1 (macbook-admin-bd8e86 thread 01M08ZBXDD8PQ9J70MM4VDBZR0): this
    # bus.whoami() runs at startup, and its purpose is a STATE-FILE LABEL —
    # cosmetic. On a network outage the client's fix at
    # _run_with_resilience translates every failure shape (including
    # concurrent.futures.TimeoutError) into TransportError, so `Exception`
    # here is the honest catch: nothing about labelling the state file may
    # prevent the watcher from launching and entering its backoff loop.
    # The old hand-written tuple omitted httpx.HTTPError entirely and
    # missed CFT on Python 3.10, so the watcher could crash on restart
    # during exactly the outage its reconnect loop existed to survive.
    try:
        # whoami returns workspace as an OBJECT: {"id": ..., "slug": ...}.
        # my_lane = the ACTING AGENT's own persona, extracted from the SAME
        # call that resolves the workspace label (zero additional network).
        # DISTINCT from the server's `lane` field (sender's persona) — the
        # SEV-2 fix, see the with_my_lane comment above.
        _who = bus.whoami() or {}
        workspace = (_who.get("workspace") or {}).get("slug") or None
        my_lane = (_who.get("agent") or {}).get("persona") or None
    except Exception:  # startup label lookup MUST NOT block launch
        workspace = None
        my_lane = None

    if args.state:
        state_key = Path(args.state).name
    else:
        state_key = f"watch-{workspace or 'unknown'}-{agent}.json"

    if getattr(args, "daemon", False):
        existing = _watch_pid(agent, state_key)
        if existing:
            print(f"watcher already running for {agent} (pid {existing})", file=sys.stderr)
            return 0
        # #204: keyed by the SAME state_key as the pidfile above, so two
        # watchers for one agent no longer share (and interleave into) one log,
        # and watch-status can name the log belonging to the watcher it is
        # reporting on rather than whichever daemon last wrote the shared path.
        log_path = _watch_logfile(agent, state_key)
        argv = [sys.executable, "-m", "agentbus_client.cli", "watch", "--agent", agent]
        for flag in ("exec", "append", "state"):
            value = getattr(args, flag, None)
            if value:
                argv += [f"--{flag}", str(value)]
        # Forward coalescer knobs so a daemonised watcher matches the
        # caller's tuning (issuedb #9).
        for flag, dest in (
            ("coalesce-window", "coalesce_window"),
            ("coalesce-quiet", "coalesce_quiet"),
        ):
            value = getattr(args, dest, None)
            if value is not None:
                argv += [f"--{flag}", str(value)]
        if getattr(args, "no_coalesce", False):
            argv += ["--no-coalesce"]
        if getattr(args, "persona", None):
            argv += ["--persona", str(args.persona)]
        with open(log_path, "ab", buffering=0) as log:
            proc = subprocess.Popen(
                argv,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own session+process group: survives SIGHUP
            )
        _watch_pidfile(agent, state_key).write_text(str(proc.pid))
        print(f"watcher started for {agent} (pid {proc.pid}), detached from this session")
        print(f"  log:    {log_path}")
        print(f"  status: agentbus watch-status --agent {agent}")
        print(f"  stop:   agentbus watch-stop --agent {agent}")
        return 0

    # Record our own pid so watch-status can answer honestly even when started
    # in the foreground. Keyed on the SAME state name resolved above, so a
    # foreground watcher and a daemon watcher for one agent with different state
    # files do not clobber each other's registration (bob's capture bug).
    with contextlib.suppress(OSError):
        _watch_pidfile(agent, state_key).write_text(str(os.getpid()))

    if args.state:
        state = Path(args.state)
    else:
        state = _cfg_dir() / f"watch-{workspace or 'unknown'}-{agent}.json"

        # A pre-existing `watch-<agent>.json` is NOT adopted. It predates this
        # change and carries no workspace, so there is no way to tell whether its
        # cursor belongs to this workspace or another one. The two failure modes
        # are not symmetric: adopting a foreign cursor SKIPS mail silently, while
        # declining replays an inbox once and is merely noisy. This system's whole
        # premise is that a missed message is the expensive failure, so it replays.
        legacy = _cfg_dir() / f"watch-{agent}.json"
        if legacy.exists() and not state.exists():
            print(
                f"agentbus watch: ignoring pre-workspace state {legacy.name} — it "
                f"does not record which workspace its cursor belongs to, so this "
                f"run replays {workspace} from the start once, then checkpoints "
                f"to {state.name}. Delete the old file when every watcher has "
                f"been restarted.",
                file=sys.stderr,
            )
    # SEV-1 follow-up (macbook-admin-bd8e86 suggestion): print client_version
    # in the startup banner so a stale watcher is self-evident in the log
    # instead of requiring an operator to think to run `agentbus --version`
    # against the exact interpreter the watcher runs out of. Same reason the
    # state file records client_version — surfaces make the failure mode
    # discoverable at the FIRST place someone would look.
    from .. import __version__ as _client_ver

    print(
        f"agentbus watch {_client_ver}: {agent} on {bus.base_url} (state: {state})",
        file=sys.stderr,
    )
    # ONLY --exec CAN START A TURN. --append files it, the default prints it to a
    # terminal that, for an unattended agent, nobody is attached to. Declaring this
    # is what lets the server stop reporting `wake_channel: true` for a recorder —
    # the state david sat in for two days while every dashboard showed him green.
    #
    # A DEAD SESSION SOCKET IS NOT A TRANSIENT ERROR, IT IS THE WAKE CHANNEL
    # BEING GONE (2026-08-11: a watcher from a dead session kept streaming and
    # injected an operator's email into a socket that no longer existed). Surface
    # it with the dedicated code and a reason on stderr — a supervisor, the
    # plugin monitor's retry loop, and `watch-status` can then tell "the wake
    # target died" from "the stream dropped" and act accordingly (stop the
    # zombie, let a fresh session re-arm).
    # SIGTERM (watch-stop, session-end, the plugin monitor) used to kill the
    # process outright, so the coalescer's buffered envelope was lost while the
    # cursor had already been persisted past it (review #23, S3). Raise a
    # BaseException instead so every `finally` below runs; exit 143 keeps the
    # conventional code the monitor script already recognises.
    import signal as _signal
    import threading as _threading

    from ..watch import EXIT_DEAD_WAKE_SOCKET, WatchTerminated
    from ..watch import DeadWakeSocket as _DeadWakeSocket

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        raise WatchTerminated()

    previous_handler: Any = None
    if _threading.current_thread() is _threading.main_thread():
        with contextlib.suppress(ValueError, OSError):
            previous_handler = _signal.signal(_signal.SIGTERM, _on_sigterm)
    pidfile = _watch_pidfile(agent, state_key)

    try:
        try:
            Watcher(
                bus,
                agent,
                on_message=handler,
                cursor=args.cursor,
                state_path=state,
                workspace=workspace,
                wake_capable=bool(args.exec),
            ).run(once=args.once)
        except _DeadWakeSocket as exc:
            print(f"agentbus watch: {exc}", file=sys.stderr)
            print("  A fresh session's monitor will re-arm the wake path.", file=sys.stderr)
            return EXIT_DEAD_WAKE_SOCKET
        except AuthError as exc:
            # SPECS/0020: a revoked key is TERMINAL — retrying will hammer
            # the bus with a credential that will never work. Exit 8 so the
            # monitor script's dedicated auth-failure branch handles it
            # (stop + operator message, no retry). Without this catch, the
            # exception reaches main()'s global AuthError handler which
            # also returns 8, but catching it HERE means cmd_watch controls
            # its own exit codes and a refactor to main() cannot silently
            # change what the monitor sees.
            print(
                f"agentbus watch: credential rejected ({exc.code}: {exc.detail})", file=sys.stderr
            )
            return 8
        except AgentBusError as exc:
            # SPECS/0020: ANY other bus error that escapes the Watcher's own
            # reconnect loop (e.g. a ServiceUnavailable or TransportError
            # during construction, or a future code path that doesn't enter
            # the while-True loop) MUST stay retryable. Exit 3 is the
            # generic "bus error" code the monitor script counts against its
            # startup budget and retries with backoff.
            #
            # Without this catch, main()'s global handlers assign
            # ServiceUnavailable → 5, QuotaExceeded → 4, generic → 3. The
            # monitor script only recognises 3 as retryable; 4 and 5 fall
            # through to the catch-all and consume an attempt with no
            # diagnostic. Normalising them to 3 here is both simpler and
            # more honest: every transient bus failure is retryable.
            tag = str(exc) or f"({type(exc).__name__})"
            print(f"agentbus watch: bus error ({tag}); monitor should retry", file=sys.stderr)
            return 3
        except WatchTerminated:
            print(
                "agentbus watch: SIGTERM received; flushing pending wakes and stopping",
                file=sys.stderr,
            )
            return 143
    finally:
        # Flush any buffered coalesced envelope so a graceful shutdown never
        # eats a wake. Runs on every exit path — normal, DeadWakeSocket,
        # SIGTERM, or an unexpected raise higher up.
        if coalescer is not None:
            coalescer.close()
        # Remove OUR pidfile (only if it still names us) so a reused PID can
        # never be mistaken for a live watcher later (issuedb #28).
        with contextlib.suppress(OSError, ValueError):
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink(missing_ok=True)
        if previous_handler is not None:
            with contextlib.suppress(ValueError, OSError):
                _signal.signal(_signal.SIGTERM, previous_handler)
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("watch", help="stay connected and act on arriving messages")
    p.add_argument(
        "--exec",
        default=None,
        help="shell command per message; {subject} {sender} {delivery_id} "
        "{message_id} {thread_id} {agent_seq} are substituted and shell-quoted",
    )
    p.add_argument("--append", default=None, help="append JSON lines to this file")
    p.add_argument("--state", default=None, help="cursor checkpoint file")
    p.add_argument("--cursor", type=int, default=0, help="start from this cursor")
    p.add_argument("--once", action="store_true", help="drain and exit; do not stream")
    p.add_argument(
        "--daemon",
        action="store_true",
        help="detach and keep running after this session ends "
        "(the wake channel is outbound SSE, so this works behind "
        "a strict inbound firewall)",
    )
    # Coalescer flags (issuedb #9, SPECS/0009). Bursts of arrivals — up
    # to a hard 2500 ms window, or until 800 ms of silence — collapse
    # into a single envelope wake carrying every buffered message.
    # A lone delivery still fires immediately (leading edge). urgent
    # priority always bypasses.
    p.add_argument(
        "--coalesce-window",
        type=int,
        default=2500,
        metavar="MS",
        dest="coalesce_window",
        help="max milliseconds the trailing envelope can accumulate (default 2500). "
        "Cap on how long the tail of a burst can hold; overrides quiet.",
    )
    p.add_argument(
        "--coalesce-quiet",
        type=int,
        default=800,
        metavar="MS",
        dest="coalesce_quiet",
        help="close the envelope after this many ms of silence (default 800). "
        "Bounded above by --coalesce-window.",
    )
    p.add_argument(
        "--no-coalesce",
        action="store_true",
        dest="no_coalesce",
        help="disable envelope coalescing entirely; fire the wake hook once per message. "
        "Only useful if a downstream hook is not envelope-aware.",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_watch)
