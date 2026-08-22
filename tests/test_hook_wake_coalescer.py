"""Hook-wake coalescer (issuedb #9, SPECS/0009).

Peer proposal by agentbus-ui-c760a1; Farshid's originating idea. These
tests exercise the state machine, not the CLI wiring.
"""

from __future__ import annotations

import threading
import time

from agentbus_client._coalesce import Coalescer, is_envelope


def _msg(
    delivery_id: str = "01D",
    *,
    subject: str = "s",
    priority: str = "normal",
    thread_id: str = "01T",
    agent_seq: int = 1,
):
    return {
        "delivery_id": delivery_id,
        "message_id": f"m-{delivery_id}",
        "subject": subject,
        "sender_display": "peer",
        "sender_address": "peer@x",
        "thread_id": thread_id,
        "agent_seq": agent_seq,
        "priority": priority,
    }


# --------------------------------------------------------------- leading edge


def test_lone_message_fires_immediately_and_is_never_wrapped() -> None:
    """The wake for a single delivery must NOT be delayed by the window,
    and MUST arrive with its original per-message shape — installed hooks
    grep on subject / sender_display."""
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=2500, quiet_ms=800)

    t0 = time.monotonic()
    c.handle(_msg("01A", subject="alone"))
    elapsed = time.monotonic() - t0

    # Fired in the same tick — leading edge, no latency.
    assert len(seen) == 1
    assert elapsed < 0.05, f"leading-edge fire took {elapsed:.3f}s; must be immediate"
    # Not wrapped: the caller receives the ORIGINAL message.
    assert seen[0]["delivery_id"] == "01A"
    assert seen[0]["subject"] == "alone"
    assert not is_envelope(seen[0])

    # The window is open — flushing it produces no envelope (buffer empty).
    c.close()
    assert len(seen) == 1  # nothing added on close


# --------------------------------------------------------------- burst / trailing


def test_burst_of_five_produces_one_leading_wake_and_one_envelope() -> None:
    """The peer's burst-test shape: leading fires once, tail coalesces
    into ONE envelope containing the remaining 4."""
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=200, quiet_ms=50)

    for i in range(5):
        c.handle(_msg(f"01B{i}", subject=f"burst-{i}", agent_seq=i))
        time.sleep(0.01)  # 10 ms between arrivals — well under the 50 ms quiet

    # Wait for the trailing window to fire.
    time.sleep(0.3)

    # Exactly two sink calls: 1 leading + 1 envelope.
    assert len(seen) == 2, f"expected 2 sink calls, got {len(seen)}: {seen}"
    # The first is the raw first message.
    assert seen[0]["delivery_id"] == "01B0"
    assert not is_envelope(seen[0])
    # The second is a coalesced envelope with the remaining 4.
    envelope = seen[1]
    assert is_envelope(envelope)
    assert envelope["count"] == 4
    assert envelope["kind"] == "coalesced"
    assert [m["delivery_id"] for m in envelope["messages"]] == [
        "01B1",
        "01B2",
        "01B3",
        "01B4",
    ]


def test_envelope_projects_first_and_last_fields_for_placeholder_templates() -> None:
    """`notify_command` substitutes {subject}, {delivery_id}, etc. — the
    envelope must carry those top-level keys so templates do not
    KeyError. First message's fields for id/thread; last message's
    agent_seq (so the cursor placement is correct)."""
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=100, quiet_ms=30)

    c.handle(_msg("01F1", subject="first", thread_id="T1", agent_seq=10))
    time.sleep(0.005)
    c.handle(_msg("01F2", subject="second", thread_id="T2", agent_seq=11))
    time.sleep(0.005)
    c.handle(_msg("01F3", subject="third", thread_id="T3", agent_seq=12))
    time.sleep(0.15)

    envelope = seen[-1]
    assert is_envelope(envelope)
    assert envelope["delivery_id"] == "01F2"  # first message of the envelope
    assert envelope["thread_id"] == "T2"
    assert envelope["agent_seq"] == 12  # last agent_seq
    assert "coalesced" in envelope["subject"].lower()
    assert "second" in envelope["subject"]  # first envelope message's subject


# --------------------------------------------------------------- urgent bypass


def test_urgent_message_bypasses_the_window_and_flushes_pending() -> None:
    """Peer requirement: 'urgent priority messages must skip the window
    and fire immediately (leading-edge, then override the accumulation)'.
    A pending buffer is flushed FIRST to preserve arrival order, then the
    urgent message fires as a lone wake."""
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=5000, quiet_ms=2000)

    # Open a slow window with two accumulated messages.
    c.handle(_msg("01U0", subject="leading"))
    c.handle(_msg("01U1", subject="accum1"))
    c.handle(_msg("01U2", subject="accum2"))
    # No sleep — window is 5s wide, so those two are still buffered.
    assert len(seen) == 1  # only the leading

    # Now an urgent arrival.
    c.handle(_msg("01UX", subject="fire!", priority="urgent"))

    # Three more sink calls: the flushed envelope (2 msgs) and the urgent.
    assert len(seen) == 3
    envelope = seen[1]
    assert is_envelope(envelope)
    assert envelope["count"] == 2
    assert [m["delivery_id"] for m in envelope["messages"]] == ["01U1", "01U2"]
    urgent = seen[2]
    assert not is_envelope(urgent)
    assert urgent["delivery_id"] == "01UX"
    assert urgent["priority"] == "urgent"


def test_urgent_when_no_window_is_open_still_fires_immediately() -> None:
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=1000, quiet_ms=200)
    c.handle(_msg("01URG", priority="urgent"))
    assert len(seen) == 1
    assert seen[0]["delivery_id"] == "01URG"


# --------------------------------------------------------------- hard cap


def test_arrivals_faster_than_quiet_still_flush_before_hard_cap() -> None:
    """A stream that arrives every quiet_ms - X forever must still flush
    before the hard window elapses — the quiet timer cannot extend past
    the hard deadline."""
    seen: list[dict] = []
    # 250 ms hard, 100 ms quiet.
    c = Coalescer(seen.append, window_ms=250, quiet_ms=100)

    # Fire 6 arrivals 50 ms apart (each resets quiet under the hard cap).
    for i in range(6):
        c.handle(_msg(f"01H{i}"))
        time.sleep(0.05)

    # Total elapsed at last handle ≈ 250 ms. Wait a small extra beyond
    # the hard deadline to be sure it fired.
    time.sleep(0.2)

    assert len(seen) >= 2  # at least leading + one envelope
    # The tail must have flushed — buffer should not still be holding.
    total_delivered = sum(1 if not is_envelope(x) else x["count"] for x in seen)
    assert total_delivered == 6


# --------------------------------------------------------------- close / shutdown


def test_close_flushes_pending_buffer_synchronously() -> None:
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=5000, quiet_ms=2000)

    c.handle(_msg("01C0"))  # leading — fires
    c.handle(_msg("01C1"))  # buffered
    c.handle(_msg("01C2"))  # buffered
    assert len(seen) == 1

    c.close()

    assert len(seen) == 2
    envelope = seen[1]
    assert is_envelope(envelope)
    assert envelope["count"] == 2


def test_arrivals_after_close_deliver_directly_and_do_not_reopen_window() -> None:
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=5000, quiet_ms=2000)
    c.handle(_msg("01D0"))
    c.close()

    c.handle(_msg("01D1"))  # after close
    # Delivered directly, single-message shape.
    assert seen[-1]["delivery_id"] == "01D1"
    assert not is_envelope(seen[-1])


# --------------------------------------------------------------- sink safety


def test_a_crashing_sink_does_not_break_the_coalescer() -> None:
    calls = []

    def crashy(msg):
        calls.append(msg)
        raise RuntimeError("hook is broken")

    c = Coalescer(crashy, window_ms=100, quiet_ms=30)
    c.handle(_msg("01E0"))  # leading, sink raises — must not propagate
    c.handle(_msg("01E1"))  # buffered
    time.sleep(0.15)  # envelope flushes, sink raises again

    assert len(calls) == 2, "sink was called for both leading and envelope"


# --------------------------------------------------------------- concurrent


# --------------------------------------------------------------- handler compat


def test_notify_command_survives_a_coalesced_envelope(tmp_path) -> None:
    """The three built-in handlers (notify_command, append_file, print_line)
    are called with an envelope dict when count>=2. They must render
    something sensible — never KeyError, never crash — because they are
    the wake channel's last mile."""
    import subprocess

    from agentbus_client import watch as watch_module

    # Simulate an envelope going through notify_command with a template that
    # references every placeholder, including the new envelope_count.
    calls: list[str] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    import unittest.mock as mock

    handler = watch_module.notify_command(
        'echo "{envelope_count} messages from {sender}, first: {subject}"'
    )
    envelope = {
        "kind": "coalesced",
        "count": 3,
        "messages": [_msg("01Q1"), _msg("01Q2"), _msg("01Q3")],
        "subject": "3 coalesced messages (first: s)",
        "sender_display": "(coalesced)",
        "delivery_id": "01Q1",
        "message_id": "coalesced",
        "thread_id": "01T",
        "agent_seq": 3,
        "direction": "coalesced",
        "inbound_source": "",
    }
    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler(envelope)  # must not raise
    assert len(calls) == 1
    # envelope_count made it through the substitution.
    assert "3 messages" in calls[0]


def test_notify_command_still_works_for_a_lone_message(tmp_path) -> None:
    """Backward compat: envelope_count=1 for a single message."""
    import subprocess
    import unittest.mock as mock

    from agentbus_client import watch as watch_module

    calls = []
    handler = watch_module.notify_command('touch "solo-{envelope_count}"')

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0)

    with mock.patch.object(subprocess, "run", side_effect=fake_run):
        handler(_msg("01SOLO"))
    assert "solo-1" in calls[0]


def test_concurrent_handle_calls_are_serialized() -> None:
    """The drain thread and background timer thread both call in — count
    and ordering must be honest under contention."""
    seen: list[dict] = []
    c = Coalescer(seen.append, window_ms=200, quiet_ms=50)

    def spam(offset: int) -> None:
        for i in range(20):
            c.handle(_msg(f"01T{offset:02d}{i:02d}"))

    threads = [threading.Thread(target=spam, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.4)  # let the tail flush
    c.close()

    total_delivered = sum(1 if not is_envelope(x) else x["count"] for x in seen)
    assert total_delivered == 80, f"lost or duplicated messages: {total_delivered}"
    # And every id is unique.
    ids: list[str] = []
    for entry in seen:
        if is_envelope(entry):
            ids.extend(m["delivery_id"] for m in entry["messages"])
        else:
            ids.append(entry["delivery_id"])
    assert len(set(ids)) == 80
