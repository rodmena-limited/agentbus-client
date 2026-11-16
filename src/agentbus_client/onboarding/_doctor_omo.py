from __future__ import annotations

from agentbus_client._omo import STALE, UNREADABLE, omo_extension_dir, omo_holder

from ._omo_extension import EXTENSION_COMMIT, MISSING, PINNED, extension_state
from ._paths import _say


def _extension_line() -> str:
    directory = omo_extension_dir()
    state, differing = extension_state(directory)
    short = EXTENSION_COMMIT[:7]
    if state == PINNED:
        return f"[ok] omo extension: {directory} matches {short}"
    if state == MISSING:
        return f"[!!] omo extension: none at {directory}"
    return f"[!!] omo extension: {directory} differs from {short} ({', '.join(differing)})"


def doctor_wake_omo(name: str) -> int | None:
    holder = omo_holder(name)
    installed = (omo_extension_dir() / "index.ts").exists()
    if not holder.live:
        if installed or holder.state in (STALE, UNREADABLE):
            detail = holder.reason or "no lock file"
            _say(f"  omo: no omo session holds the stream for {name} ({holder.state}: {detail})")
            _say(f"  {_extension_line()}")
        return None
    _say("  harness: omo")
    _say(f"  [ok] omo watcher: pid {holder.pid} holds {holder.path}")
    _say(f"  {_extension_line()}")
    _say(f"wake chain: an omo session holds the stream for {name} in-process.")
    _say("  This check did not test that stream or deliver a message. To prove it, leave")
    _say(f"  the session idle and run: agentbus send {name} -s 'wake test' -b 'Reply: PONG'")
    return 0
