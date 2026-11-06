"""#94 item 3: a queued call cancelled on timeout left its flag in `_INFLIGHT`.

`_wrapped` drops the flag in its `finally`, and a future cancelled while it is
queued never runs `_wrapped`. The tests use the real bulkhead with one worker.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.client import TransportError, resilience

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _one_worker_and_one_queue_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_INFLIGHT", set())
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_QUEUE", "1")
    monkeypatch.delenv("AGENTBUS_SDK_RESILIENCE", raising=False)
    monkeypatch.delenv("AGENTBUS_SDK_MAX_RETRIES", raising=False)


class _Worker:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.results: list[Any] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _hold(self) -> str:
        self.started.set()
        self.release.wait(10)
        return "held"

    def _run(self) -> None:
        self.results.append(resilience._run_with_resilience(self._hold, timeout=10))

    def start(self) -> None:
        self._thread.start()
        assert self.started.wait(5)

    def finish(self) -> None:
        self.release.set()
        self._thread.join(5)
        assert self.results == ["held"]


def _settled(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_a_queued_call_that_times_out_leaves_only_the_running_flag() -> None:
    worker = _Worker()
    worker.start()
    assert len(resilience._INFLIGHT) == 1
    ran: list[int] = []
    with pytest.raises(TransportError, match=r"did not complete within 0\.05s"):
        resilience._run_with_resilience(lambda: ran.append(1), timeout=0.05)
    assert len(resilience._INFLIGHT) == 1
    worker.finish()
    assert _settled(lambda: len(resilience._INFLIGHT) == 0)
    assert ran == []


def test_repeated_timeouts_do_not_pile_up() -> None:
    worker = _Worker()
    worker.start()
    for _ in range(25):
        with pytest.raises(TransportError):
            resilience._run_with_resilience(lambda: "never", timeout=0.01)
    assert len(resilience._INFLIGHT) == 1
    worker.finish()
    assert _settled(lambda: len(resilience._INFLIGHT) == 0)


def test_the_queue_has_room_again_after_the_timeout() -> None:
    """A cap that only blocks is a deadlock. The slot the cancelled call held must
    come back, and later calls must run."""
    worker = _Worker()
    worker.start()
    bulkhead = resilience._sdk_bulkhead()
    with pytest.raises(TransportError):
        resilience._run_with_resilience(lambda: "never", timeout=0.05)
    assert _settled(lambda: bulkhead._in_flight_count == 1)
    worker.finish()
    assert _settled(lambda: bulkhead._in_flight_count == 0)
    assert resilience._run_with_resilience(lambda: "after", timeout=5) == "after"
    assert len(resilience._INFLIGHT) == 0


def test_a_running_call_keeps_its_flag_until_it_ends() -> None:
    """The other direction: the flag of a call that is still running is what lets its
    next attempt see it was abandoned, so it must stay until the call ends."""
    release = threading.Event()
    flags: list[threading.Event] = []

    def _running() -> str:
        flags.extend(resilience._INFLIGHT)
        release.wait(10)
        return "late"

    with pytest.raises(TransportError):
        resilience._run_with_resilience(_running, timeout=0.05)
    assert len(flags) == 1
    assert set(flags) == resilience._INFLIGHT
    assert flags[0].is_set()
    release.set()
    assert _settled(lambda: len(resilience._INFLIGHT) == 0)


def test_a_call_that_finishes_in_time_leaves_no_flag() -> None:
    assert resilience._run_with_resilience(lambda: "done", timeout=5) == "done"
    assert len(resilience._INFLIGHT) == 0
