"""Backend caveat A: the SSE backlog is capped at 100 deliveries per
reconnect (thread 01M08ZABM8B3N2VB1TV7R7J2ED).

  "A watcher out for long enough to miss >100 deliveries will need to
   reconnect AGAIN with the latest Last-Event-ID to pull the next 100,
   or fall through to GET /v1/inbox catch-up."

The claim to verify — NOT assume — is that the client already handles
this via the HTTP drain path, which pages `while True` until it gets an
empty batch. If that is true, no client change is needed for the cap. If
it is false, a watcher returning from a long outage silently loses
everything past the first 100.

That distinction is exactly the kind this session kept getting wrong by
reading code and concluding rather than exercising it, so these tests
drive real page boundaries through `_drain`.
"""

from __future__ import annotations

from agentbus_client.watch import Watcher


class _Delivery:
    """Minimal stand-in for client.Delivery — _drain uses .seq and .raw."""

    def __init__(self, seq: int):
        self.seq = seq
        self.raw = {"delivery_id": f"d{seq}", "agent_seq": seq, "subject": f"msg-{seq}"}


class _PagingBus:
    """Serves `total` deliveries in pages of `page_size`, exactly as the
    real inbox endpoint does — each call returns everything after the
    cursor, capped at the limit."""

    def __init__(self, total: int, page_size: int = 100):
        self.total = total
        self.page_size = page_size
        self.agent = "test-agent"
        self.base_url = "https://x"
        self.api_key = "k"
        self.calls: list[int] = []

    def inbox(self, cursor, limit=100, agent=None):
        self.calls.append(cursor)
        remaining = [s for s in range(1, self.total + 1) if s > cursor]
        return [_Delivery(s) for s in remaining[: min(limit, self.page_size)]]


def _watcher(bus, tmp_path, seen):
    return Watcher(
        bus,
        agent="test-agent",
        state_path=tmp_path / "state.json",
        on_message=lambda m: seen.append(m["agent_seq"]),
    )


def test_backlog_of_exactly_100_drains_completely(tmp_path):
    """The boundary case. A backlog of exactly the cap must not stop at
    the cap — the next call returning empty is what ends the loop."""
    seen: list[int] = []
    bus = _PagingBus(total=100)
    w = _watcher(bus, tmp_path, seen)

    delivered = w._drain()

    assert delivered == 100
    assert seen == list(range(1, 101))
    assert w.cursor == 100


def test_backlog_of_250_drains_past_the_cap(tmp_path):
    """Backend caveat A, exercised. 250 deliveries with a 100-per-page
    cap must all surface — the drain pages three times (100, 100, 50)
    then a fourth empty call terminates it."""
    seen: list[int] = []
    bus = _PagingBus(total=250)
    w = _watcher(bus, tmp_path, seen)

    delivered = w._drain()

    assert delivered == 250, (
        f"only {delivered} of 250 deliveries surfaced — a watcher returning "
        "from a long outage would silently lose everything past the cap"
    )
    assert seen == list(range(1, 251))
    assert w.cursor == 250
    # Paged from the right cursors: 0, then 100, then 200, then 250 (empty).
    assert bus.calls == [0, 100, 200, 250]


def test_drain_resumes_from_a_persisted_cursor_past_the_cap(tmp_path):
    """A watcher that crashed mid-backlog resumes where it stopped rather
    than replaying from 0 (which would re-fire every wake hook) or
    skipping ahead (which would lose mail)."""
    seen: list[int] = []
    bus = _PagingBus(total=250)
    w = _watcher(bus, tmp_path, seen)
    w.cursor = 120  # crashed partway through the second page

    delivered = w._drain()

    assert delivered == 130  # 121..250
    assert seen == list(range(121, 251))
    assert bus.calls[0] == 120, "did not resume from the persisted cursor"


def test_empty_backlog_makes_exactly_one_call(tmp_path):
    """The common case must not cost extra round trips."""
    seen: list[int] = []
    bus = _PagingBus(total=0)
    w = _watcher(bus, tmp_path, seen)

    assert w._drain() == 0
    assert bus.calls == [0]
    assert seen == []


def test_a_failing_handler_does_not_stop_the_backlog_drain(tmp_path):
    """One bad wake hook must not strand the remaining 249 messages.
    The handler exception is logged per-message and the drain continues —
    otherwise a single malformed delivery would block catch-up forever."""
    delivered_to_handler: list[int] = []

    def flaky(msg):
        delivered_to_handler.append(msg["agent_seq"])
        if msg["agent_seq"] == 42:
            raise RuntimeError("this one hook is broken")

    bus = _PagingBus(total=250)
    w = Watcher(
        bus,
        agent="test-agent",
        state_path=tmp_path / "state.json",
        on_message=flaky,
    )

    delivered = w._drain()

    assert delivered == 250
    assert len(delivered_to_handler) == 250
    assert w.cursor == 250
