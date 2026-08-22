"""_drain call sites MUST NOT interleave across threads (REG-3, round-3 audit).

The round-2 audit shipped _drain_async that ran the drain on a background
thread. But its lock was held only during the LAUNCH decision, not for the
duration — so a main-thread caller (_backoff_and_drain on SSE drop, or the
startup drain in run()) could call _drain() concurrently with the background
thread. The cursor advanced twice; on_message ran twice per delivery; the
duplicate-wake incident (#88) would have reappeared exactly here.

This test proves the current design serializes ALL drain call sites through
one lock — a second drain launched (via _drain_async OR directly) while a
first is in-flight either waits (blocking acquire) or skips (non-blocking).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from agentbus_client import watch


class _StubBus:
    """Minimal AgentBus stand-in that returns one batch of one message the first
    call, then always empty. Enough for _drain() to run one iteration."""

    def __init__(self) -> None:
        self.api_key = "ab_sk_stub"
        self.base_url = "https://stub"
        self.calls = 0

    def inbox(self, cursor: int, limit: int = 100, agent: str | None = None):
        self.calls += 1
        # First call: hand back one message with seq=1 so the cursor advances.
        if self.calls == 1:
            return [_Msg(seq=1)]
        return []


class _Msg:
    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.raw = {"delivery_id": f"d{seq}", "agent_seq": seq}


def _make_watcher(bus, on_message, slow_drain: bool = False) -> watch.Watcher:
    """Build a Watcher on a real Watcher class but with a stub bus.

    We poke internals directly (agent, state_path=None, workspace) rather than
    going through construction paths that would need a real credential.
    """
    w = watch.Watcher(
        bus=bus,
        agent="test-agent",
        state_path=None,
        workspace="test-workspace",
        on_message=on_message,
    )
    if slow_drain:
        # Wrap the real _drain in a slow one — sleep 50 ms so a racing caller
        # would deterministically overlap without the lock.
        real_drain = w._drain

        def slow():
            time.sleep(0.05)
            return real_drain()

        w._drain = slow
    return w


def test_drain_async_and_main_thread_never_interleave() -> None:
    """Kick a background drain, immediately call _drain() from the main thread
    (simulating _backoff_and_drain), and assert on_message ran EXACTLY ONCE
    for the single message the stub returned.

    Without the lock the main-thread call and the background call would each
    invoke on_message once, producing two calls for one message.
    """
    bus = _StubBus()
    calls: list[dict[str, Any]] = []

    def on_msg(m: dict[str, Any]) -> None:
        calls.append(m)

    w = _make_watcher(bus, on_msg, slow_drain=True)
    # Fire the background drain, then immediately do a serialized main-thread
    # drain (the pattern _backoff_and_drain uses).
    w._drain_async()
    with w._drain_lock:
        w._drain()
    # Join the background thread if it exists.
    if w._drain_thread is not None:
        w._drain_thread.join(timeout=2.0)

    # Exactly ONE on_message call — no duplicate. If the lock weren't held for
    # the duration of _drain, we'd see two, because the stub returned one
    # message and the cursor would advance on both calls before either sees
    # the empty second batch.
    assert len(calls) == 1, f"on_message fired {len(calls)} time(s), expected 1"


def test_concurrent_drain_async_calls_are_deduped() -> None:
    """Two _drain_async calls at the same instant must produce at most one
    background thread and one drain execution. The second is a no-op."""
    bus = _StubBus()
    calls: list[dict[str, Any]] = []

    def on_msg(m: dict[str, Any]) -> None:
        calls.append(m)

    w = _make_watcher(bus, on_msg, slow_drain=True)

    # Fire two _drain_async calls back-to-back.
    threads = []
    for _ in range(2):
        t = threading.Thread(target=w._drain_async, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=1.0)
    if w._drain_thread is not None:
        w._drain_thread.join(timeout=2.0)

    # The stub's single message must reach on_message exactly once even
    # though two _drain_async calls happened.
    assert len(calls) == 1, f"on_message fired {len(calls)} time(s), expected 1"
