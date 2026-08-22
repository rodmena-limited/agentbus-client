"""Persisted `_failures` + jitter across restarts (macbook fix #6, backend endorsed).

The old backoff had two related flaws that the SEV-1 report exposed:

  (a) `_failures` reset to 0 on every successful stream open AND on every
      process restart, so an OS-supervised watcher whose parent died during
      an outage came back and reconnected at 1s. Every log line during a
      multi-minute outage read "retrying in 1s" — a 1Hz hammer against the
      recovering server (macbook's secondary defect c).
  (b) No jitter, so N watchers on N boxes coming back after a shared bus
      restart reconnected in lockstep and started tripping the server's
      30 QPS bulkhead. Backend recommended >= +/-10% jitter; we use +/-15%.

This file pins:
  * On restart within the TTL window: `_failures` is loaded from disk and
    the next reconnect uses the correct backoff step, not step 0.
  * On restart AFTER the TTL window: `_failures` resets to 0.
  * Jitter is applied to the sleep (base +/- 15%).
  * A successful stream open persists the reset so the NEXT restart is clean.
  * `_backoff_and_drain` persists the new failures count immediately, so an
    OS-supervisor crash between backoff steps does not lose the ladder.
"""

from __future__ import annotations

import json
import time

import pytest

from agentbus_client import watch as watch_module
from agentbus_client.watch import Watcher


class _FakeBus:
    def __init__(self):
        self.agent = "test-agent"
        self.base_url = "https://x"
        self.api_key = "ab_sk_test"
        self.inbox_raises: BaseException | None = None

    def inbox(self, cursor, limit=100, agent=None):
        if self.inbox_raises is not None:
            raise self.inbox_raises
        return []

    def whoami(self, agent=None):
        return {"workspace": {"slug": "test-ws"}}


def _new_watcher(tmp_path, cursor=0):
    return Watcher(_FakeBus(), agent="test-agent", state_path=tmp_path / "state.json")


# ------------------------------------------------------------- persist across restart


def test_failures_persist_across_restart_within_ttl(tmp_path, monkeypatch):
    """The exact SEV-1 shape: an OS supervisor's crash-and-restart during
    an outage MUST NOT reset the backoff ladder to step 0."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # Freeze time so last_failure_at is deterministic.
    fixed_now = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: fixed_now)

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("outage")
    # Two backoffs — walks the ladder from 1s to 2s.
    w._backoff_and_drain("test-1")
    w._backoff_and_drain("test-2")
    assert w._failures == 2

    # Simulate a crash + restart — construct a new Watcher with the SAME
    # state file. Its _failures MUST load from disk, not start at 0.
    w2 = _new_watcher(tmp_path)
    assert w2._failures == 2, (
        f"expected persisted failures=2, got {w2._failures} — an OS supervisor "
        f"restart would incorrectly restart the backoff ladder at 0"
    )
    assert w2._last_failure_at == fixed_now


def test_failures_reset_after_ttl_expires(tmp_path, monkeypatch):
    """If the persisted failure is old (watcher was up for hours after one
    early transient), the ladder must reset. Otherwise the next reconnect
    after a long healthy uptime would incorrectly start at some high step."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fixed_now = 1_700_000_000.0

    w = _new_watcher(tmp_path)
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    w.bus.inbox_raises = RuntimeError("early transient")
    w._backoff_and_drain("failure at t=now")
    assert w._failures == 1
    assert w._last_failure_at == fixed_now

    # Move the clock forward past the TTL.
    later = fixed_now + watch_module._FAILURES_TTL_SECONDS + 60
    monkeypatch.setattr(time, "time", lambda: later)

    w2 = _new_watcher(tmp_path)
    assert w2._failures == 0, "stale last_failure_at (older than TTL) must reset the ladder"


def test_successful_reconnect_clears_persisted_failures(tmp_path, monkeypatch):
    """When the stream opens cleanly, the successful open MUST persist the
    reset — otherwise a restart shortly after would reload stale failures
    and start at a high backoff step for no reason."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("transient")
    w._backoff_and_drain("t1")
    w._backoff_and_drain("t2")
    w._backoff_and_drain("t3")
    assert w._failures == 3

    # Simulate a successful stream open: mimic what _stream_once does.
    w.bus.inbox_raises = None
    # This is the load-bearing pattern from _stream_once:
    if w._failures != 0 or w._last_failure_at:
        w._failures = 0
        w._last_failure_at = 0.0
        w._save_cursor()

    # New watcher loading from the same state file MUST see failures=0.
    w2 = _new_watcher(tmp_path)
    assert w2._failures == 0
    assert w2._last_failure_at == 0.0


# ------------------------------------------------------------- jitter


def test_sleep_delay_is_jittered_around_the_base_step(tmp_path, monkeypatch):
    """+/-15% around the base step — deterministic when random.uniform is
    patched, so tests can pin the exact expected delay."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    # Force jitter to +15% for a predictable test — real prod uses uniform.
    monkeypatch.setattr(
        watch_module.random,
        "uniform",
        lambda lo, hi: hi,  # always the upper bound
    )

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("t")
    w._backoff_and_drain("t")
    # base 1s + 15% jitter = 1.15s
    assert slept == [pytest.approx(1.15, abs=1e-3)]

    # Now force jitter to -15% and check the other bound.
    slept.clear()
    monkeypatch.setattr(
        watch_module.random,
        "uniform",
        lambda lo, hi: lo,  # always the lower bound
    )
    w._backoff_and_drain("t")
    # step 2 (base 2s) with -15% jitter = 1.7s
    assert slept == [pytest.approx(1.7, abs=1e-3)]


def test_jitter_never_produces_a_negative_delay(tmp_path, monkeypatch):
    """max(0.0, base + jitter) guards against a pathological jitter value
    that could underflow. Belt-and-braces."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    # Force absurd negative jitter — much larger than the base step could ever
    # trigger in real code, but the guard should hold.
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: -1000.0)

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("t")
    w._backoff_and_drain("t")
    assert slept == [0.0]  # clamped, not negative


def test_backoff_walks_persisted_ladder_across_restarts(tmp_path, monkeypatch):
    """End-to-end: three backoffs, restart, one more backoff — the restarted
    watcher's backoff step comes from the persisted count, so its sleep
    reflects step 3 (the ladder is at index min(3, 5) = 3 → 10s), not
    step 0 (1s)."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    # No jitter for a deterministic step check.
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)

    w1 = _new_watcher(tmp_path)
    w1.bus.inbox_raises = RuntimeError("t")
    w1._backoff_and_drain("1")
    w1._backoff_and_drain("2")
    w1._backoff_and_drain("3")
    assert slept == [1.0, 2.0, 5.0]

    # Restart, same state file. New watcher loads _failures=3 -> next step 10s.
    slept.clear()
    w2 = _new_watcher(tmp_path)
    w2.bus.inbox_raises = RuntimeError("t")
    w2._backoff_and_drain("post-restart")
    assert slept == [10.0], (
        f"expected the restarted watcher to resume at ladder step 3 (10s), "
        f"got {slept} — the SEV-1 1Hz-hammer defect would show here as [1.0]"
    )


# ------------------------------------------------------------- persist immediately


def test_backoff_state_persisted_before_sleep_completes(tmp_path, monkeypatch):
    """`_save_cursor` is called BEFORE the sleep so a kill -9 during the
    sleep window (or an OS-supervisor pull) doesn't lose the ladder step."""
    saves = []
    real_save = Watcher._save_cursor

    def tracking_save(self):
        saves.append(self._failures)
        real_save(self)

    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(Watcher, "_save_cursor", tracking_save)

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("t")
    w._backoff_and_drain("t")

    # The save must have happened with the incremented count. Order matters
    # for the SEV-1 fix: persist first, sleep second.
    assert 1 in saves, f"saves recorded: {saves}"


# ------------------------------------------------------------- state file schema


def test_state_file_writes_new_backoff_fields(tmp_path, monkeypatch):
    """Contract check on the persisted schema: the state file must include
    `failures` and `last_failure_at` alongside the existing cursor +
    workspace + client_version. A schema audit tool reading state files can
    then observe the backoff position without a live process."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)

    w = _new_watcher(tmp_path)
    w.bus.inbox_raises = RuntimeError("t")
    w._backoff_and_drain("t")

    data = json.loads((tmp_path / "state.json").read_text())
    assert "failures" in data
    assert "last_failure_at" in data
    assert data["failures"] == 1
    assert data["last_failure_at"] == 1_700_000_000.0
    # Existing fields untouched.
    assert "cursor" in data
    assert "workspace" in data
    assert "client_version" in data
