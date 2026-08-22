"""Regression tests for the end-to-end audit sweep (0.9.29).

Four findings, each of the same shape the whole session has been about:
a correct implementation existing in one place and a divergent copy —
or a missing guard — somewhere else.

  REG-8d  onboarding._agent_key built keys/<agent>.env WITHOUT the
          traversal sanitizer its three siblings use. Reachable from a
          project-controlled .claude/settings.local.json, so a hostile
          checkout escalated an ordinary CLI verb to the OPERATOR key.

  F1      Watcher._drain_async held _drain_lock across a Thread.start()
          that can raise. If it did, the lock was stranded forever: the
          watcher stayed alive, answered nothing, and watch-status still
          reported RUNNING. Silent total wake-death.

  F2      Watcher._backoff_and_drain acquired that lock with an UNBOUNDED
          blocking wait, so the recovery path could stall for minutes on
          the failing path with no backoff and no log line.

  F3      rewake's poll classified failures by exception TYPE-NAME
          SUBSTRING, so every typed API error (503, 429, bare
          AgentBusError for 502/504) escaped and abandoned the whole
          600-second re-wake window on a single upstream blip.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

# ------------------------------------------------------- REG-8d traversal


def test_agent_key_cannot_traverse_to_operator_env(tmp_path, monkeypatch):
    """THE ESCALATION, reproduced then pinned.

    Before the fix, a hostile `.claude/settings.local.json` naming the
    agent `../operator` made `_agent_key` read the workspace OPERATOR
    credential — the one that can MINT a bound key for any agent."""
    from agentbus_client import onboarding

    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    (cfg / "operator.env").write_text("export AGENTBUS_API_KEY=ab_sk_OPERATOR_SECRET\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))

    got = onboarding._agent_key("../operator")

    assert got is None, (
        "traversal reached operator.env — a hostile checkout can escalate an "
        "ordinary CLI verb to the workspace operator credential"
    )


def test_agent_key_still_resolves_a_legitimate_bound_key(tmp_path, monkeypatch):
    """KNOWN-POSITIVE. Without it, a sanitizer that returned None for
    everything would satisfy the test above while silently breaking every
    real credential lookup — the fix would be an outage, not a guard."""
    from agentbus_client import onboarding

    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    (cfg / "keys" / "realagent.env").write_text("export AGENTBUS_API_KEY=ab_sk_LEGIT\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))

    assert onboarding._agent_key("realagent") == "ab_sk_LEGIT"


def test_agent_key_matches_its_siblings_sanitizer(tmp_path, monkeypatch):
    """The four call sites that resolve keys/<agent>.env must agree.
    REG-8b sanitized three and enumerated them in bound_env_filename's
    docstring; this one was left off that list."""
    from agentbus_client import onboarding, sealing

    cfg = tmp_path / "cfg"
    (cfg / "keys").mkdir(parents=True)
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))

    hostile = "../operator"
    expected_name = sealing.bound_env_filename(hostile)
    # The sanitizer must neutralise the traversal rather than pass it through.
    assert "/" not in expected_name
    assert ".." not in expected_name
    # And _agent_key must be looking for exactly that filename.
    (cfg / "keys" / expected_name).write_text("export AGENTBUS_API_KEY=ab_sk_SANITIZED\n")
    assert onboarding._agent_key(hostile) == "ab_sk_SANITIZED"


# ------------------------------------------------------- F1 stranded lock


class _Bus:
    agent = "a"
    base_url = "https://x"
    api_key = "k"

    def inbox(self, *a, **kw):
        return []


def test_drain_lock_is_released_when_the_thread_cannot_start(tmp_path):
    """F1. Thread.start() raises RuntimeError under thread/FD exhaustion.
    The lock was taken BEFORE it and released only inside the thread body,
    so a failed start stranded it forever — after which _drain_async was a
    permanent no-op and _backoff_and_drain blocked for the life of the
    process. Watcher alive, permanently deaf, watch-status says RUNNING."""
    from agentbus_client.watch import Watcher

    w = Watcher(_Bus(), agent="a", state_path=tmp_path / "s.json")

    with patch.object(threading, "Thread", side_effect=RuntimeError("can't start new thread")):
        w._drain_async()  # must not raise

    assert w._drain_lock.acquire(blocking=False), (
        "the drain lock was stranded by a failed Thread.start() — every "
        "subsequent drain and the entire reconnect path would block forever"
    )
    w._drain_lock.release()


def test_drain_lock_released_on_memory_error_too(tmp_path):
    """Same guard, the other plausible failure under pressure."""
    from agentbus_client.watch import Watcher

    w = Watcher(_Bus(), agent="a", state_path=tmp_path / "s.json")
    with patch.object(threading, "Thread", side_effect=MemoryError()):
        w._drain_async()
    assert w._drain_lock.acquire(blocking=False)
    w._drain_lock.release()


# ------------------------------------------------------- F2 bounded acquire


def test_backoff_does_not_block_forever_on_an_in_flight_drain(tmp_path, monkeypatch):
    """F2. The recovery path must never wait unboundedly on the failing
    path. With the lock held by a long drain, _backoff_and_drain must skip
    the opportunistic drain, say so, and still perform its backoff sleep."""
    from agentbus_client import watch as watch_module
    from agentbus_client.watch import Watcher

    monkeypatch.setattr(watch_module, "_DRAIN_LOCK_TIMEOUT_SECONDS", 0.1)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)

    w = Watcher(_Bus(), agent="a", state_path=tmp_path / "s.json")
    w._drain_lock.acquire()  # simulate a long in-flight background drain
    try:
        start = time.monotonic()
        w._backoff_and_drain("stream dropped (test)")
        elapsed = time.monotonic() - start
    finally:
        w._drain_lock.release()

    assert elapsed < 5, f"reconnect stalled {elapsed:.1f}s waiting on the drain lock"
    assert slept == [1.0], "the backoff sleep must still happen when the drain is skipped"


# ------------------------------------------------------- F3 rewake classifier


@pytest.mark.parametrize(
    "exc_factory,label",
    [
        (
            lambda: __import__("agentbus_client.client", fromlist=["AgentBusError"]).AgentBusError(
                "bad gateway", status=502
            ),
            "bare AgentBusError (502/504)",
        ),
        (
            lambda: __import__(
                "agentbus_client.client", fromlist=["ServiceUnavailable"]
            ).ServiceUnavailable("503"),
            "ServiceUnavailable",
        ),
        (
            lambda: __import__("agentbus_client.client", fromlist=["QuotaExceeded"]).QuotaExceeded(
                "429"
            ),
            "QuotaExceeded",
        ),
        (lambda: RuntimeError("something nobody predicted"), "unforeseen shape"),
    ],
)
def test_rewake_poll_survives_every_typed_api_error(exc_factory, label, monkeypatch):
    """F3. Each of these used to ESCAPE the poll and abandon the whole
    600-second re-wake window, because the classifier matched on exception
    TYPE-NAME SUBSTRING and none of these names contain 'timeout',
    'connect', 'transport', 'network', 'socket', 'ssl' or 'dns'.

    The call site documents `"" on any failure`. That guarantee is now real."""
    from agentbus_client import rewake

    monkeypatch.setattr(
        rewake, "_unread_text", lambda agent, wait=0: (_ for _ in ()).throw(exc_factory())
    )

    poll = rewake._build_resilient_poll("some-agent", wait=0)
    result = poll()  # must not raise

    assert result == "", f"{label} did not degrade to an empty poll result"


def test_rewake_poll_still_returns_real_text_on_success(monkeypatch):
    """KNOWN-POSITIVE for the guard above — a poll that returned "" for
    everything would satisfy every case in the parametrised test while
    silently disabling the re-waker entirely."""
    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "_unread_text", lambda agent, wait=0: "agentbus show 01ABC")
    poll = rewake._build_resilient_poll("some-agent", wait=0)
    assert poll() == "agentbus show 01ABC"


def test_rewake_classifies_503_as_transient():
    """The audit gap: during a rolling deploy, ServiceUnavailable (503) and
    bare AgentBusError (502/504) must be classified TRANSIENT so the retry
    policy backoffs and the circuit breaker counts them — not escape the
    SafetyNet into the fallback (which skipped both)."""
    from agentbus_client import rewake
    from agentbus_client.client import (
        AgentBusError,
        AuthError,
        NotFoundError,
        QuotaExceeded,
        ServiceUnavailable,
        TransportError,
    )

    is_t = rewake._is_transient_rewake_error
    # Transient — retried with backoff + counted by the breaker.
    assert is_t(ConnectionError("wifi dropped"))
    assert is_t(TimeoutError())
    assert is_t(OSError("dns failed"))
    assert is_t(ServiceUnavailable("deploy", status=503))
    assert is_t(AgentBusError("gateway", status=502))
    assert is_t(AgentBusError("gateway", status=504))
    assert is_t(TransportError("request never got an answer"))
    # Definitive — not transient, passes through for the outer guards.
    assert not is_t(AuthError("revoked", status=401))
    assert not is_t(NotFoundError("gone", status=404))
    assert not is_t(QuotaExceeded("429", status=429))
    assert not is_t(AgentBusError("bad", status=422))


# ------------------------------------------------------- F2b doctor ledger path


def test_doctor_reads_the_ledger_the_rewaker_actually_writes():
    """The doctor's 'production wake-ledger untouched' assertion used to
    build the path inline, diverging from rewake._ledger_path on THREE
    axes ($AGENTBUS_REWAKE_STATE, $AGENTBUS_WAKE_DIR vs
    $AGENTBUS_CONFIG_DIR, and the REG-8c agent sanitizer). Whenever any
    diverged it sampled a file the re-waker never writes, so the check
    could only ever go GREEN.

    Pinned by source inspection: doctor must CALL the function."""
    import inspect

    from agentbus_client import onboarding

    src = inspect.getsource(onboarding.doctor_wake)
    assert "_rewake._ledger_path(" in src, (
        "doctor rebuilt the ledger path instead of asking rewake for it — "
        "the isolation check can silently become one that cannot go red"
    )
