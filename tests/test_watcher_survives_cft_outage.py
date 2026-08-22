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
import time
from unittest.mock import patch

import pytest

from agentbus_client import client as client_module
from agentbus_client.client import AgentBusError, TransportError
from agentbus_client.watch import DeadWakeSocket, Watcher

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
        patch.object(client_module.resilience, "_sdk_bulkhead", lambda: _FakeBulkhead()),
        patch.object(client_module.resilience, "_sdk_safety_net", lambda: lambda f: f),
        pytest.raises(TransportError) as exc,
    ):
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
    # First backoff step is 1s with +/-15% jitter — range [0.85, 1.15].
    assert 0.85 <= slept[-1] <= 1.15, f"unexpected sleep with jitter: {slept[-1]}"
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
    # Exactly one sleep call for the base 1s step (jittered +/-15%).
    assert len(slept) == 1
    assert 0.85 <= slept[0] <= 1.15, f"unexpected jittered sleep: {slept}"


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
    at 'retrying in 1s' during a multi-minute outage — a 1Hz hammer.

    Pin the base ladder (jitter disabled by patching random.uniform to 0)."""
    from agentbus_client import watch as watch_module

    bus = _FakeBus()
    bus.inbox_raises = concurrent.futures.TimeoutError()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)

    w = _watcher(bus, tmp_path)
    for _ in range(6):
        w._backoff_and_drain("test")
    # First six failures walk (1, 2, 5, 10, 30, 60) with jitter disabled.
    assert slept == [1.0, 2.0, 5.0, 10.0, 30.0, 60.0]


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


def test_run_can_start_when_startup_drain_raises(tmp_path, monkeypatch):
    """The blackhole test that caught 0.9.24 (macbook-admin-bd8e86 in
    thread 01M08ZWE0XCTPJG1R0ZBXP8K7P): a watcher pointed at an
    unroutable base_url MUST be able to START. Before this fix the
    startup drain sat outside the reconnect envelope, so
    `Watcher.run()` raised out of run() and the process exited 3
    with the CFT-translated TransportError text — 'The reconnect
    loop treats this as retryable' when in fact it had never been
    reached."""
    bus = _FakeBus()
    bus.inbox_raises = TransportError("SDK call did not complete within 35.0s")

    w = _watcher(bus, tmp_path)

    # `run(once=True)` should NOT raise — startup drain deferred, loop
    # returns 0 because once=True prevents entering the stream loop.
    rc = w.run(once=True)
    assert rc == 0, "run(once=True) must not raise or exit-code when startup drain fails"


def test_run_startup_stamps_client_version_immediately(tmp_path, monkeypatch):
    """Macbook's instrument-lag observation: `doctor --wake` used to
    read `client_version` from the state file, but the state file was
    only rewritten when the cursor advanced. On a healthy watcher with
    a quiet inbox, the version field lagged arbitrarily — leading
    doctor to report a stale watcher that was actually current.

    Fix: watcher writes the state file at startup before doing anything
    that could block. Any subsequent doctor call reads a fresh version
    field even if no messages have arrived yet."""
    bus = _FakeBus()
    # Startup drain doesn't matter for THIS test — deferred handler catches.
    bus.inbox_raises = TransportError("net down")

    w = _watcher(bus, tmp_path)
    rc = w.run(once=True)
    assert rc == 0

    # State file MUST exist and have client_version — not require a
    # cursor advance.
    import json

    state_file = tmp_path / "state.json"
    assert state_file.exists(), "startup did not write the state file"
    data = json.loads(state_file.read_text())
    assert "client_version" in data
    assert data["client_version"], "client_version was empty in state file"


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


def test_cmd_watch_catches_escaped_auth_error(monkeypatch, tmp_path):
    """AuthError escaping Watcher.run() returns exit code 8 (terminal)."""
    import argparse

    from agentbus_client import cli as cli_module
    from agentbus_client import watch as watch_module
    from agentbus_client.client import AuthError

    args = argparse.Namespace(
        api_key="test-key",
        agent="test-agent",
        base_url="https://fake",
        cursor=None,
        state=str(tmp_path / "state.json"),
        workspace=None,
        exec=None,
        append=None,
        coalesce=None,
        once=True,
    )

    def _fake_run(self, once=False):
        raise AuthError("Key revoked", code="auth_failed", status=401)

    monkeypatch.setattr(watch_module.Watcher, "run", _fake_run)
    rc = cli_module.cmd_watch(args)
    assert rc == 8


def test_cmd_watch_catches_escaped_service_unavailable(monkeypatch, tmp_path):
    """ServiceUnavailable/AgentBusError escaping Watcher.run() returns exit code 3 (retryable)."""
    import argparse

    from agentbus_client import cli as cli_module
    from agentbus_client import watch as watch_module
    from agentbus_client.client import ServiceUnavailable

    args = argparse.Namespace(
        api_key="test-key",
        agent="test-agent",
        base_url="https://fake",
        cursor=None,
        state=str(tmp_path / "state.json"),
        workspace=None,
        exec=None,
        append=None,
        coalesce=None,
        once=True,
    )

    def _fake_run(self, once=False):
        raise ServiceUnavailable("Gateway down", status=503)

    monkeypatch.setattr(watch_module.Watcher, "run", _fake_run)
    rc = cli_module.cmd_watch(args)
    assert rc == 3


# ------------------------------------------------- Fix: revocation is TERMINAL
#
# SPECS/0020: AuthError (confirmed-revoked credential) exits 8 and is never
# retried. The cli.py handler above only fires if Watcher.run() actually lets
# AuthError ESCAPE — the generic `except Exception` in the reconnect loop used
# to swallow it into _backoff_and_drain and retry forever (hammering the bus
# with a key that will never work): the SSE revocation asymmetry the audit
# flagged.


def test_run_propagates_confirmed_revocation_from_stream(tmp_path, monkeypatch):
    """run() must re-raise AuthError from _stream_once, not fold it into the
    reconnect backoff. If it were swallowed, _backoff_and_drain would fire —
    which this test turns into a loud failure instead of an infinite loop."""
    from unittest.mock import patch

    from agentbus_client import watch as watch_module
    from agentbus_client.client import AuthError

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)

    def _revoked(self, once=False):
        raise AuthError("API key was revoked", code="revoked", status=401)

    def _must_not_backoff(self, reason, stream=None):
        raise AssertionError("revoked credential was folded into the backoff loop")

    with (
        patch.object(watch_module.Watcher, "_stream_once", _revoked),
        patch.object(watch_module.Watcher, "_backoff_and_drain", _must_not_backoff),
        pytest.raises(AuthError),
    ):
        w.run()


def test_run_propagates_revocation_from_startup_drain(tmp_path, monkeypatch):
    """A revoked key at STARTUP is terminal too: run(once=True) must raise
    AuthError, not defer the drain and return 0 (a revoked credential must
    never look like a successful `--once` check)."""
    from agentbus_client.client import AuthError

    bus = _FakeBus()
    bus.inbox_raises = AuthError("revoked", code="revoked", status=401)
    w = _watcher(bus, tmp_path)
    with pytest.raises(AuthError):
        w.run(once=True)


class _FakeStreamResponse:
    """Minimal httpx stream response: needs the context-manager protocol
    (`client.stream(...) as response`) and raise_for_status — nothing else is
    reached for these 4xx tests."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"stream error {self.status_code}",
                request=object(),
                response=self,
            )


class _FakeStreamClient:
    def __init__(self, status_code: int):
        self._status = status_code

    def __enter__(self) -> _FakeStreamClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def stream(self, method: str, url: str, headers=None) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._status)


def test_stream_once_confirmed_401_raises_auth_error(tmp_path, monkeypatch):
    """GET /v1/stream returning 401 with a REST-confirmed revocation raises
    AuthError (-> exit 8), not httpx.HTTPStatusError folded into backoff."""

    from agentbus_client import watch as watch_module

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)
    monkeypatch.setattr(watch_module.httpx, "Client", lambda timeout=None: _FakeStreamClient(401))
    monkeypatch.setattr(w, "_key_really_revoked", lambda: True)
    with pytest.raises(watch_module.AuthError):
        w._stream_once()


def test_stream_once_401_without_confirmation_stays_transient(tmp_path, monkeypatch):
    """GET /v1/stream returning 401 that is NOT a confirmed revocation (a
    server blip) re-raises the HTTPStatusError so the backoff loop reconnects —
    it must NOT exit 8 on the server's word alone."""
    import httpx

    from agentbus_client import watch as watch_module

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)
    monkeypatch.setattr(watch_module.httpx, "Client", lambda timeout=None: _FakeStreamClient(401))
    monkeypatch.setattr(w, "_key_really_revoked", lambda: False)
    with pytest.raises(httpx.HTTPStatusError):
        w._stream_once()
