"""Resilient wake monitor — the ACTIVE trigger that survives a laptop.

Runs as the Claude Code Stop hook (via `agentbus-hook monitor`, wrapped by the
0700 shell that resolves the credential). It stays armed for a bounded window
after each turn and re-wakes the session — exit code 2 — the moment genuinely
new mail arrives, so an idle agent answers a peer without a human touching the
keyboard.

Why this is not a `while: sleep` loop. Real hosts are laptops: wifi drops in a
tunnel, the lid closes mid-poll, DNS goes away on a network switch, the machine
suspends for an hour and resumes. A naive loop treats every one of those as a
crash or spins hot against a dead network. So each poll is wrapped in the house
resilience library (`resilient-circuit`):

  * RetryWithBackoffPolicy — a single poll that hits a transient network error
    is retried with exponential backoff + jitter, not abandoned;
  * CircuitProtectorPolicy — sustained failure (wifi off for minutes) OPENS a
    breaker so we stop hammering and idle-back-off until a half-open probe finds
    the network again, instead of burning battery in a tight retry;
  * SafetyNet composes them (breaker outermost, retry inner), with process-local
    InMemoryStorage so a breaker tripped in one turn never leaks into the next.

The window is measured in WALL-CLOCK time, so a suspend-and-resume is handled
for free: on resume we compare real time to the deadline and stop if it passed,
rather than trusting an elapsed-iteration count that a sleeping CPU never
advanced. And the whole thing is wrapped so that NOTHING it can hit — a missing
dependency, an unreadable ledger, a broken clock — ever exits non-zero for a
reason other than "new mail" (2). A hook that crashes the session is worse than
one that says nothing.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

_SHOW_RE = re.compile(r"agentbus show ([A-Za-z0-9_-]+)")


def _config_dir() -> Path:
    root = os.environ.get("AGENTBUS_WAKE_DIR")
    return Path(root) if root else Path.home() / ".config" / "agentbus"


def _ledger_path(agent: str) -> Path:
    override = os.environ.get("AGENTBUS_REWAKE_STATE")
    if override:
        return Path(override)
    # REG-8c (round-3.6, bikeroom): sanitize agent — this is the WORSE of the
    # state-file findings because the Stop-hook monitor calls _monitor_inner
    # on EVERY TURN, in EVERY project, and _monitor_inner calls this. So a
    # hostile `.agentbus/agent` on ANY checkout the operator opens is a
    # write primitive reachable passively, without any explicit hook
    # invocation.
    from . import sealing

    return _config_dir() / f"rewake-seen-{sealing.agent_slug(agent)}.txt"


def _delivery_keys(text: str) -> list[str]:
    ids = sorted(set(_SHOW_RE.findall(text)))
    if ids:
        return ids
    # No parseable ids (format drift): one key per distinct payload, so a
    # changed output wakes once per message — never a loop, never silence.
    return ["sha:" + hashlib.sha256(text.encode()).hexdigest()]


def _unread_text(agent: str, wait: int = 0) -> str:
    """One poll: what is unread, as human lines. Raises on network trouble so
    the resilience policies can see and classify it.

    `wait` LONG-POLLS instead of returning empty (#45). The API holds the
    connection up to 55s and answers the moment mail lands, so a message arriving
    one second after a poll is seen in one second rather than after the next
    sleep. The window used to be short precisely because a sleep-poll cannot
    afford to be long — this removes the reason for that compromise.

    The whoami pre-check is skipped when waiting: it answers instantly, so
    asking it first would return "nothing unread" and throw away the very hold
    we want. That pre-check exists to avoid a second call in the common empty
    case, which is exactly the case long-polling is for.
    """
    from .client import AgentBus

    bus = AgentBus(agent=agent)
    if wait <= 0:
        count = int(((bus.whoami().get("unread") or {}).get("count")) or 0)
        if not count:
            return ""
    lines = []
    for m in bus.inbox(limit=25, unread=True, wait=wait):
        lines.append(f"  {m.sender}: {m.subject}  (agentbus show {m.delivery_id})")
    return "\n".join(lines)


def monitor(_args: object = None) -> int:
    """Return 2 (with new mail on stdout) to re-wake, else 0. Never raises."""
    try:
        return _monitor_inner()
    except Exception as exc:
        print(f"agentbus monitor: giving up cleanly ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0


def _monitor_inner() -> int:
    # AGENTBUS_AGENT IS THE KILL SWITCH — same rule as the hooks and the plugin
    # monitor, same resolution order, and for the same reason (#90: three
    # components must never disagree about who this session is).
    #
    # Silent when there is no identity. This is a Stop hook, so it fires at the
    # end of EVERY turn in EVERY project on the machine; a line of stderr per
    # turn in projects that never opted in is how an operator ends up
    # uninstalling the wake path everywhere.
    from .hooks.claude_code import _resolve_agent

    agent = _resolve_agent()
    if not agent:
        return 0

    window = int(os.environ.get("AGENTBUS_REWAKE_WINDOW", "600"))
    interval = max(1, int(os.environ.get("AGENTBUS_REWAKE_INTERVAL", "15")))
    ledger = _ledger_path(agent)
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.touch(exist_ok=True)
        seen = set(ledger.read_text().split()) if ledger.exists() else set()
    except OSError:
        seen = set()

    # LONG-POLL (#45). The API holds a connection up to 55s and answers the
    # instant mail lands; a sleep-poll answers on its next tick. That latency is
    # why the re-wake window had to be short, so removing it removes the reason
    # for the compromise.
    #
    # Capped at 25s rather than 55: the harness kills a Stop hook at its own
    # timeout, and a hold longer than the remaining window would be cut off
    # mid-request — which looks like a network failure to the breaker and would
    # trip it for reasons that are entirely our own doing.
    long_poll = 0 if window == 0 else min(25, max(1, interval))
    poll = _build_resilient_poll(agent, wait=long_poll)
    deadline = time.time() + window

    while True:
        started = time.time()
        text = poll()  # already retried/broken/failsafed; "" on any failure
        if text:
            fresh = [k for k in _delivery_keys(text) if k not in seen]
            if fresh:
                seen.update(fresh)
                _append_ledger(ledger, fresh)
                print(text)
                return 2
        # Wall-clock deadline: a suspend/resume is judged by real time, not by
        # how many iterations a sleeping CPU managed to run.
        if time.time() >= deadline:
            return 0
        # AGENTBUS_REWAKE_WINDOW=0 -> a single deterministic pass (used by
        # `doctor --wake`, which wants one check, not a 10-minute hold).
        if window == 0:
            return 0
        # HOT-LOOP GUARD, and it is not hypothetical: with `unread=True` and an
        # unacked backlog the server has rows to return, so the long-poll
        # answers INSTANTLY every time. Those rows are all already in the
        # ledger, so `fresh` is empty and we would spin at full speed against
        # the API. This agent is sitting on 140+ unread right now.
        #
        # So sleep only when the poll came back FAST — meaning it returned data
        # rather than holding. A genuine hold has already spent the interval and
        # needs no extra wait.
        if time.time() - started < long_poll * 0.5:
            time.sleep(interval)


def _append_ledger(ledger: Path, fresh: list[str]) -> None:
    try:
        with ledger.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                for k in fresh:
                    fh.write(k + "\n")
                fh.flush()
                fh.seek(0)
                lines = fh.read().split()
                if len(lines) > 1000:
                    fh.seek(0)
                    fh.truncate()
                    fh.write("\n".join(lines[-500:]) + "\n")
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass


def _build_resilient_poll(agent: str, wait: int = 0) -> Callable[[], str]:
    """A poll callable wrapped in retry+breaker+failsafe. Falls back to a plain
    guarded poll if resilient-circuit is somehow absent — degraded, but never a
    crash and never a silent wrong answer (it says so on stderr)."""
    net_errors = (ConnectionError, TimeoutError, OSError)

    def raw() -> str:
        try:
            return _unread_text(agent, wait=wait)
        except Exception as exc:
            # Re-raise transport/network as network so the policies classify it;
            # anything else is a real bug and should surface to the outer guard.
            name = type(exc).__name__.lower()
            if any(
                t in name
                for t in ("timeout", "connect", "transport", "network", "socket", "ssl", "dns")
            ):
                raise ConnectionError(str(exc)) from exc
            raise

    # #234 Q2: resilient-circuit is a HARD DEP. The previous silent try/except
    # around this whole build hid three things: (1) a broken install ran without
    # backoff/breaker while looking healthy, (2) any minor-version kwarg change
    # in resilient-circuit tripped the fallback with no import error surfaced, and
    # (3) the "degrade loudly" comment above the fallback wrote to a stderr
    # nobody reads (background monitor). If the import fails now it raises here,
    # and `agentbus doctor` catches it — the right place to notice a broken install.
    import datetime as _dt
    from fractions import Fraction

    import resilient_circuit as rc
    from resilient_circuit.exceptions import ProtectionException
    from resilient_circuit.storage import InMemoryStorage

    # #234 Q2 (audit finding): Fraction(1, 1) meant the breaker opened on any
    # single failure and closed on any single success — combined with the
    # cooldown being the poll interval, the breaker cooldown never exceeded the
    # poll interval itself, so this was decorative. Widen to a real burst
    # threshold so a genuine outage opens the breaker meaningfully.
    #
    # Semantics: over the last 5 attempts, if all 5 failed the breaker opens;
    # 2 successes close it. Cooldown stays at the poll interval floor.
    net = rc.SafetyNet(
        policies=(
            rc.CircuitProtectorPolicy(
                resource_key=f"wake-poll-{agent}",
                storage=InMemoryStorage(),  # process-local: no leak between turns
                failure_limit=Fraction(5, 5),  # 5 of the last 5 attempts failed
                success_limit=Fraction(2, 2),  # 2 clean attempts to close
                cooldown=_dt.timedelta(
                    seconds=max(5, int(os.environ.get("AGENTBUS_REWAKE_INTERVAL", "15")))
                ),
                should_handle=lambda e: isinstance(e, net_errors),
            ),
            rc.RetryWithBackoffPolicy(
                max_retries=3,
                backoff=rc.ExponentialDelay(
                    min_delay=_dt.timedelta(seconds=1),
                    max_delay=_dt.timedelta(seconds=10),
                    factor=2,
                    jitter=0.2,
                ),
                should_handle=lambda e: isinstance(e, net_errors),
            ),
        )
    )
    guarded = net(raw)

    def resilient() -> str:
        try:
            result: str = guarded()
            return result
        except ProtectionException:
            # Retries exhausted or breaker open — a transient outage, not an
            # error to surface. The next interval tries again; the wall-clock
            # deadline still bounds the whole thing.
            return ""
        except BaseException as exc:  # noqa: BLE001 — deliberate, see below
            # MAKE THE DOCUMENTED GUARANTEE REAL.
            #
            # `poll()`'s call site carries the comment `# already
            # retried/broken/failsafed; "" on any failure`. It was not true.
            # This used to catch only ConnectionError, and `raw()` above only
            # converts to ConnectionError when the exception's TYPE NAME
            # contains one of ("timeout","connect","transport","network",
            # "socket","ssl","dns"). Classifying by name substring means every
            # TYPED api error slips through:
            #
            #   ServiceUnavailable (503), QuotaExceeded / RateLimited (429),
            #   NotFoundError (404), ValidationError (422), and a BARE
            #   AgentBusError — which is what 500/502/504 become, since they
            #   are absent from client._ERRORS.
            #
            # Any one of those escaped resilient() -> escaped poll() -> left
            # the `while True` -> unwound _monitor_inner -> hit monitor()'s
            # blanket handler, which prints one line to a stderr nobody reads
            # in a background Stop hook and returns 0.
            #
            # Net effect: A SINGLE 502 ABANDONED THE ENTIRE 600-SECOND RE-WAKE
            # WINDOW. The session went un-rewoken for the rest of the turn
            # boundary and the operator saw nothing — the same silent-wake-death
            # class as the watcher SEV-1, one layer over.
            #
            # This poll is opportunistic and bounded by a wall-clock deadline,
            # so the correct behaviour for EVERY failure is the same: yield
            # nothing this tick and let the next interval try. Classify by
            # what we should DO, not by what the exception is called.
            tag = str(exc) or f"({type(exc).__name__})"
            print(
                f"agentbus rewake: poll failed ({tag}); will retry on the next interval",
                file=sys.stderr,
                flush=True,
            )
            return ""

    return resilient
