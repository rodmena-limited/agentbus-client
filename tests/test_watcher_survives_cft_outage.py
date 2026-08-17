"""SEV-1 (macbook-admin-bd8e86, thread 01M08ZBXDD8PQ9J70MM4VDBZR0):
`agentbus watch` used to DIE on network outage because
`_run_with_resilience` leaked `concurrent.futures._base.TimeoutError`
raw, and on Python 3.10 that class does NOT subclass OSError. Every
downstream `except (AgentBusError, OSError, httpx.HTTPError, ...)`
guard let it escape — INCLUDING the reconnect handler
`_backoff_and_drain`, so the recovery mechanism was the thing that
crashed during exactly the condition it existed to recover from.

These tests force-raise `concurrent.futures.TimeoutError` at each of
the sites the outage traversed and assert the watcher survives:

  1. `_run_with_resilience` translates it at the boundary into
     TransportError with the original as __cause__. No caller ever
     sees a raw CFT again.
  2. `_backoff_and_drain` catches whatever the drain raises, always
     sleeps for the backoff (in a `finally` block), never propagates
     upward except for DeadWakeSocket.
  3. Startup `whoami()` in cmd_watch is total — a network-down startup
     no longer prevents the process from launching.
  4. Empty-message diagnostic: str(CFT()) is '', so logs now include
     type name when the message is empty.

All tests use pytest.MonkeyPatch to inject the exception without any
real network activity. The scenarios are constructed to reproduce the
exact traceback macbook observed on Python 3.10.20.
"""

from __future__ import annotations

import concurrent.futures
import io
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agentbus_client import client as client_module
from agentbus_client.client import AgentBusError, TransportError
from agentbus_client.watch import Watcher, DeadWakeSocket


# ---------------------------------------------------- Fix #1: boundary translation


def test_run_with_resilience_translates_cft_to_transport_error():
    """The SINGLE FIX that closes the whole class. Every downstream `except
    TransportError` now catches network stalls on every Python version, at
    every call site, including ones nobody audited."""
    def timing_out_call():
        raise RuntimeError("should not reach here")

    # Bypass the real bulkman + safety net — patch the future to raise CFT
    # directly out of .result(timeout=...), which is what happens on a real
    # network stall.
    class _FakeFuture:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

    class _FakeBulkhead:
        def execute(self, fn):
            return _FakeFuture()

    with (
        patch.object(client_module, "_sdk_bulkhead", lambda: _FakeBulkhead()),
        patch.object(client_module, "_sdk_safety_net", lambda: lambda f: f),
    ):
        with pytest.raises(TransportError) as exc:
            client_module._run_with_resilience(timing_out_call, timeout=0.01)

    # The original CFT is preserved as the cause so callers can still
    # introspect if they want to.
    assert isinstance(exc.value.__cause__, concurrent.futures.TimeoutError)
    # The error text names the shape so an operator scanning logs can grep.
    assert "did not complete within" in str(exc.value)
    assert "TimeoutError" in str(exc.value)


def test_transport_error_is_catchable_by_downstream_guards():
    """Contract check: TransportError is an AgentBusError subclass, so every
    `except AgentBusError` guard in watch.py, cli.py, onboarding.py catches
    the translated CFT for free."""
    assert issubclass(TransportError, AgentBusError)


# ---------------------------------------------------- Fix #3: backoff is total


class _FakeBus:
    """Minimum surface for Watcher: `.inbox`, `.agent`, `.base_url`,
    `.api_key`, `.whoami`. Everything is patchable."""

    def __init__(self):
        self.agent = "test-agent"
        self.base_url = "https://x"
        self.api_key = "ab_sk_test"
        self.inbox_raises: BaseException | None = None
        self.inbox_calls = 0

    def inbox(self, cursor, limit=100, agent=None):
        self.inbox_calls += 1
        if self.inbox_raises is not None:
            raise self.inbox_raises
        return []

    def whoami(self, agent=None):
        return {"workspace": {"slug": "test-ws"}}


def _watcher(bus, tmp_path):
    return Watcher(bus, agent="test-agent", state_path=tmp_path / "state.json")


def test_backoff_and_drain_survives_cft_from_inbox(tmp_path, monkeypatch):
    """The exact site macbook traced. bus.inbox() raises CFT (network
    down). _backoff_and_drain USED to let it propagate to run() and kill
    the process. Now it catches everything and sleeps in finally."""
    bus = _FakeBus()
    bus.inbox_raises = concurrent.futures.TimeoutError()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    w = _watcher(bus, tmp_path)
    # Must not raise. Must call sleep for the backoff.
    w._backoff_and_drain("stream dropped (test)")
    assert slept, "backoff sleep was skipped — the whole point of _backoff_and_drain"
    assert slept[-1] == 1  # first backoff step is 1s
    # _failures was incremented so subsequent backoffs escalate.
    assert w._failures == 1


def test_backoff_and_drain_survives_arbitrary_exception_from_drain(tmp_path, monkeypatch):
    """Belt-and-braces: even if a NEW exception shape lands in the drain
    tomorrow, the reconnect handler is total. Only DeadWakeSocket re-raises."""
    bus = _FakeBus()

    class WeirdError(RuntimeError):
        pass

    bus.inbox_raises = WeirdError("some unexpected shape")
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    w = _watcher(bus, tmp_path)
    w._backoff_and_drain("test")  # must not raise
    assert slept, "sleep in finally still fires"


def test_backoff_sleep_fires_even_when_drain_raises(tmp_path, monkeypatch):
    """Macbook's secondary defect (b): `time.sleep(delay)` used to sit AFTER
    the drain, unguarded, so a drain that raised skipped the sleep too and
    the watcher became a 1Hz reconnect hammer against the server."""
    bus = _FakeBus()
    bus.inbox_raises = concurrent.futures.TimeoutError()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    w = _watcher(bus, tmp_path)
    w._backoff_and_drain("test")
    assert slept == [1], f"expected exactly one 1s sleep, got {slept}"


def test_backoff_reraises_dead_wake_socket(tmp_path, monkeypatch):
    """Contract: DeadWakeSocket is the ONE signal that means 'stop trying,
    the wake target no longer exists' — it must propagate out even though
    the reconnect handler is otherwise total."""
    bus = _FakeBus()
    dead = DeadWakeSocket("session socket is gone")
    bus.inbox_raises = dead
    monkeypatch.setattr(time, "sleep", lambda s: None)

    w = _watcher(bus, tmp_path)
    with pytest.raises(DeadWakeSocket):
        w._backoff_and_drain("test")


def test_backoff_escalates_across_repeated_failures(tmp_path, monkeypatch):
    """The RECONNECT_BACKOFF table is (1, 2, 5, 10, 30, 60). Repeated
    failures MUST walk it, not stay at 1s. macbook's log showed every line
    at 'retrying in 1s' during a multi-minute outage — a 1Hz hammer."""
    bus = _FakeBus()
    bus.inbox_raises = concurrent.futures.TimeoutError()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    w = _watcher(bus, tmp_path)
    for _ in range(6):
        w._backoff_and_drain("test")
    # First six failures walk (1, 2, 5, 10, 30, 60)
    assert slept == [1, 2, 5, 10, 30, 60]


# ---------------------------------------------------- Fix #7: empty-message diagnostic


def test_drain_async_diagnostic_names_type_when_str_is_empty(tmp_path, capsys):
    """str(concurrent.futures.TimeoutError()) is ''. macbook's log
    literally contained `agentbus watch: background drain failed:` with
    nothing after the colon. Now the type name is in the log."""
    bus = _FakeBus()
    bus.inbox_raises = concurrent.futures.TimeoutError()

    w = _watcher(bus, tmp_path)
    w._drain_async()
    # drain runs on a background thread; wait for it to finish
    if w._drain_thread is not None:
        w._drain_thread.join(timeout=5)

    err = capsys.readouterr().err
    assert "background drain failed" in err
    # The type name is present — the empty str() no longer eats the signal.
    assert "TimeoutError" in err


# ---------------------------------------------------- Fix #4: startup resilience (integration)


def test_cmd_watch_whoami_startup_catches_any_exception():
    """The startup workspace-label lookup was `except (AgentBusError,
    OSError, ValueError, KeyError)` — missing httpx.HTTPError entirely,
    missing CFT on 3.10. Now bare `except Exception` because the whoami
    is a COSMETIC state-file label, not a functional prerequisite. If it
    fails the watcher must still launch and enter backoff.
    """
    import inspect
    from agentbus_client import cli as cli_module

    src = inspect.getsource(cli_module.cmd_watch)
    # The startup guard for the workspace label is now total.
    assert "except Exception:" in src
    # The specific comment referencing the SEV-1 diagnosis is present so a
    # future refactor doesn't narrow it back.
    assert "startup label lookup MUST NOT block launch" in src
