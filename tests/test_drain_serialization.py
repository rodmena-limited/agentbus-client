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

The double serves every message past the cursor it is given, as the server does,
and holds the first fetch open until a second fetch arrives or OVERLAP_WAIT runs
out. A second drain that reaches the server while the first is in flight therefore
gets the same message, and each drain hands it to on_message.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING, Any

from agentbus_client import watch

if TYPE_CHECKING:
    import pytest

OVERLAP_WAIT = 0.5
FIRST_FETCH_DEADLINE = 5.0
JOIN_DEADLINE = 5.0


class _Msg:
    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.raw = {"delivery_id": f"d{seq}", "agent_seq": seq}


class _CursorBus:
    """The server's inbox contract, plus a window in which two fetches can meet."""

    def __init__(self) -> None:
        self.api_key = "ab_sk_stub"
        self.base_url = "https://stub"
        self.messages = [_Msg(seq=1)]
        self.fetching = threading.Event()
        self.peak_fetches = 0
        self._active_fetches = 0
        self._count_lock = threading.Lock()
        self._second_fetch = threading.Barrier(2)

    def inbox(self, cursor: int, limit: int = 100, agent: str | None = None) -> list[Any]:
        batch = [m for m in self.messages if m.seq > cursor][:limit]
        with self._count_lock:
            self._active_fetches += 1
            self.peak_fetches = max(self.peak_fetches, self._active_fetches)
        self.fetching.set()
        with contextlib.suppress(threading.BrokenBarrierError):
            self._second_fetch.wait(timeout=OVERLAP_WAIT)
        with self._count_lock:
            self._active_fetches -= 1
        return batch


def _make_watcher(monkeypatch: pytest.MonkeyPatch, bus: Any) -> tuple[watch.Watcher, list[str]]:
    """A real Watcher on the double, with the reconnect backoff at zero.

    We poke internals directly (agent, state_path=None, workspace) rather than
    going through construction paths that would need a real credential.
    """
    monkeypatch.setattr(watch, "RECONNECT_BACKOFF", (0,))
    delivered: list[str] = []
    w = watch.Watcher(
        bus=bus,
        agent="test-agent",
        state_path=None,
        workspace="test-workspace",
        on_message=lambda m: delivered.append(m["delivery_id"]),
    )
    return w, delivered


def _start_background_drain(w: watch.Watcher, bus: _CursorBus) -> threading.Thread:
    w._drain_async()
    assert w._drain_thread is not None, "the background drain did not start"
    assert bus.fetching.wait(timeout=FIRST_FETCH_DEADLINE), "the drain never reached the server"
    return w._drain_thread


def _finish(thread: threading.Thread) -> None:
    thread.join(timeout=JOIN_DEADLINE)
    assert not thread.is_alive(), "the background drain did not finish"


def test_drain_async_and_main_thread_never_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconnect arrives while a background drain is inside the server call.
    The message reaches on_message exactly once and the two drains never overlap."""
    bus = _CursorBus()
    w, delivered = _make_watcher(monkeypatch, bus)

    background = _start_background_drain(w, bus)
    w._backoff_and_drain("stream dropped")
    _finish(background)

    assert delivered == ["d1"], f"on_message fired {len(delivered)} time(s), expected 1"
    assert bus.peak_fetches == 1


def test_the_startup_drain_waits_for_a_background_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _CursorBus()
    w, delivered = _make_watcher(monkeypatch, bus)

    background = _start_background_drain(w, bus)
    assert w._startup_drain() == 0
    _finish(background)

    assert delivered == ["d1"], f"on_message fired {len(delivered)} time(s), expected 1"
    assert bus.peak_fetches == 1


def test_concurrent_drain_async_calls_are_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A _drain_async call made while one is in flight is a no-op: it starts no
    thread and the first drain delivers the message alone."""
    bus = _CursorBus()
    w, delivered = _make_watcher(monkeypatch, bus)

    background = _start_background_drain(w, bus)
    w._drain_async()
    assert w._drain_thread is background, "a second drain thread started"
    _finish(background)

    assert delivered == ["d1"], f"on_message fired {len(delivered)} time(s), expected 1"
    assert bus.peak_fetches == 1


def test_mail_after_a_finished_drain_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock comes back: both call sites drain again once the first is done."""
    bus = _CursorBus()
    w, delivered = _make_watcher(monkeypatch, bus)

    _finish(_start_background_drain(w, bus))
    assert delivered == ["d1"]

    bus.messages.append(_Msg(seq=2))
    w._backoff_and_drain("stream dropped")
    assert delivered == ["d1", "d2"]

    bus.messages.append(_Msg(seq=3))
    w._drain_async()
    assert w._drain_thread is not None
    _finish(w._drain_thread)
    assert delivered == ["d1", "d2", "d3"]
