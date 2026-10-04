from __future__ import annotations

import threading
import time
import weakref

import pytest

from agentbus_client.client import resilience
from agentbus_client.client.errors import AgentBusError, TransportError


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", None)
    monkeypatch.setattr(resilience, "_SDK_SAFETY_NET", None)
    monkeypatch.setattr(resilience, "_INFLIGHT", set())
    for name in (
        "AGENTBUS_SDK_MAX_CONCURRENT",
        "AGENTBUS_SDK_MAX_QUEUE",
        "AGENTBUS_SDK_CB_COOLDOWN",
        "AGENTBUS_SDK_MAX_RETRIES",
        "AGENTBUS_SDK_CB_FAILURE_LIMIT",
        "AGENTBUS_SDK_CB_SUCCESS_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)


def _wait_until(predicate, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _background(fn, results: list) -> threading.Thread:
    def _run() -> None:
        try:
            results.append(resilience._run_with_resilience(fn, timeout=10))
        except Exception as exc:
            results.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_bulkhead_defaults_and_breaker_off() -> None:
    bulkhead = resilience._sdk_bulkhead()
    assert bulkhead.config.name == "agentbus-sdk"
    assert bulkhead.config.max_concurrent_calls == 8
    assert bulkhead.config.max_queue_size == 100
    assert bulkhead.config.circuit_breaker_enabled is False
    assert bulkhead._executor._max_workers == 8
    assert bulkhead._executor._thread_name_prefix == "Bulkhead-agentbus-sdk"


def test_max_concurrent_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", "3")
    bulkhead = resilience._sdk_bulkhead()
    assert bulkhead.config.max_concurrent_calls == 3
    assert bulkhead._executor._max_workers == 3


def test_max_queue_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_QUEUE", "2")
    assert resilience._sdk_bulkhead().config.max_queue_size == 2


def test_singleton_is_reused() -> None:
    assert resilience._sdk_bulkhead() is resilience._sdk_bulkhead()
    assert resilience._sdk_safety_net() is resilience._sdk_safety_net()


def test_workers_are_daemon_threads() -> None:
    names: list[str] = []
    out = resilience._run_with_resilience(lambda: names.append(threading.current_thread().name))
    assert out is None
    assert names == ["Bulkhead-agentbus-sdk_0"]
    threads = list(resilience._sdk_bulkhead()._executor._threads)
    assert len(threads) == 1
    assert [t.daemon for t in threads] == [True]
    assert type(resilience._sdk_bulkhead()._executor).__name__ == "_DaemonExecutor"


def test_daemon_executor_runs_work_on_daemon_threads() -> None:
    executor = resilience._daemon_executor(2, "probe")
    try:
        seen = executor.submit(lambda: threading.current_thread().daemon).result(timeout=5)
        name = executor.submit(lambda: threading.current_thread().name).result(timeout=5)
    finally:
        executor.shutdown(wait=True)
    assert seen is True
    assert name.startswith("probe_")
    assert executor._max_workers == 2


def test_concurrency_cap_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", "2")
    release = threading.Event()
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def _blocker() -> str:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        release.wait(5)
        with lock:
            state["now"] -= 1
        return "done"

    results: list = []
    threads = [_background(_blocker, results) for _ in range(4)]
    assert _wait_until(lambda: state["now"] == 2)
    time.sleep(0.2)
    assert state["peak"] == 2
    assert len(resilience._sdk_bulkhead()._executor._threads) == 2
    release.set()
    for t in threads:
        t.join(5)
    assert results == ["done"] * 4
    assert state["peak"] == 2


def test_queue_full_rejects_then_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_CONCURRENT", "1")
    monkeypatch.setenv("AGENTBUS_SDK_MAX_QUEUE", "1")
    release = threading.Event()
    results: list = []
    threads = [_background(lambda: release.wait(5) and "done", results) for _ in range(2)]
    bulkhead = resilience._sdk_bulkhead()
    assert _wait_until(lambda: bulkhead._in_flight_count == 2)
    with pytest.raises(TransportError) as info:
        resilience._run_with_resilience(lambda: "never", timeout=5)
    assert "agentbus SDK bulkhead full" in str(info.value)
    assert len(resilience._INFLIGHT) == 2
    release.set()
    for t in threads:
        t.join(5)
    assert results == ["done", "done"]
    assert _wait_until(lambda: bulkhead._in_flight_count == 0)
    assert resilience._run_with_resilience(lambda: "after", timeout=5) == "after"
    assert len(resilience._INFLIGHT) == 0


def test_inflight_registry_is_empty_after_calls() -> None:
    assert resilience._run_with_resilience(lambda: 41 + 1) == 42
    assert len(resilience._INFLIGHT) == 0
    with pytest.raises(AgentBusError):
        resilience._run_with_resilience(_raise(AgentBusError("gone", status=404)))
    assert len(resilience._INFLIGHT) == 0


def test_inflight_registry_holds_the_cancel_flag_during_a_call() -> None:
    seen: list = []

    def _peek() -> str:
        seen.extend(resilience._INFLIGHT)
        return "x"

    assert resilience._run_with_resilience(_peek) == "x"
    assert len(seen) == 1
    assert isinstance(seen[0], threading.Event)
    assert seen[0].is_set() is False


def _raise(exc: BaseException):
    def _fn():
        raise exc

    return _fn


@pytest.mark.parametrize("status", [500, 502, 599])
def test_non_idempotent_5xx_is_attempted_once(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_MAX_RETRIES", "3")
    calls: list[int] = []

    def _fn() -> None:
        calls.append(1)
        raise AgentBusError("server exploded", status=status)

    with pytest.raises(AgentBusError) as info:
        resilience._run_with_resilience(_fn, timeout=10, idempotent=False)
    assert info.value.status == status
    assert type(info.value) is AgentBusError
    assert calls == [1]


def test_outer_deadline_returns_on_time_and_abandons() -> None:
    attempts: list[int] = []
    flags: list = []

    def _slow() -> str:
        attempts.append(1)
        flags.extend(resilience._INFLIGHT)
        time.sleep(1.0)
        raise TransportError("slow")

    started = time.monotonic()
    with pytest.raises(TransportError) as info:
        resilience._run_with_resilience(_slow, timeout=0.2)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert "did not complete within 0.2s" in str(info.value)
    assert len(flags) == 1
    assert flags[0].is_set() is True
    time.sleep(1.5)
    assert attempts == [1]


def test_cancellable_backoff_returns_zero_when_flagged() -> None:
    import datetime

    backoff = resilience._cancellable_backoff(
        min_delay=datetime.timedelta(seconds=3),
        max_delay=datetime.timedelta(seconds=8),
        factor=2,
        jitter=0.2,
    )
    assert backoff.jitter == 0.2
    assert backoff.factor == 2
    flag = threading.Event()
    flag.set()
    resilience._CURRENT_CANCEL.flag = flag
    try:
        started = time.monotonic()
        assert backoff.for_attempt(1) == 0.0
        assert time.monotonic() - started < 0.5
    finally:
        resilience._CURRENT_CANCEL.flag = None
    assert backoff.for_attempt(1) >= 2.0


def test_exit_hook_abandons_closes_and_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    flag = threading.Event()
    monkeypatch.setattr(resilience, "_INFLIGHT", {flag})

    class _Client:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.closed = 0

        def close(self) -> None:
            self.closed += 1
            if self.fail:
                raise RuntimeError("close failed")

    clients = [_Client(True), _Client(False)]
    registry: weakref.WeakSet = weakref.WeakSet(clients)
    monkeypatch.setattr(resilience, "_SYNC_CLIENTS", registry)

    class _Executor:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def shutdown(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    class _Bulkhead:
        def __init__(self) -> None:
            self._executor = _Executor()

    bulkhead = _Bulkhead()
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", bulkhead)
    resilience._abandon_inflight_on_exit()
    assert flag.is_set() is True
    assert [c.closed for c in clients] == [1, 1]
    assert bulkhead._executor.calls == [{"wait": False, "cancel_futures": True}]


def test_exit_hook_without_bulkhead_still_abandons(monkeypatch: pytest.MonkeyPatch) -> None:
    flag = threading.Event()
    monkeypatch.setattr(resilience, "_INFLIGHT", {flag})
    monkeypatch.setattr(resilience, "_SYNC_CLIENTS", weakref.WeakSet())
    resilience._abandon_inflight_on_exit()
    assert flag.is_set() is True


def test_exit_hook_survives_a_failing_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Executor:
        def shutdown(self, **_k: object) -> None:
            raise RuntimeError("boom")

    class _Bulkhead:
        _executor = _Executor()

    monkeypatch.setattr(resilience, "_SYNC_CLIENTS", weakref.WeakSet())
    monkeypatch.setattr(resilience, "_SDK_BULKHEAD", _Bulkhead())
    assert resilience._abandon_inflight_on_exit() is None
